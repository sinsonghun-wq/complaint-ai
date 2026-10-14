"""Versioned, atomic import of extracted and reviewed official originals."""
from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .db import connection
from .rag import vector_literal
from .rag_models import embed_texts, embedding_identity
from .rag_schema import ensure_rag_schema

ROOT = Path(__file__).resolve().parents[2]


def split_section(text: str, limit: int = 1600) -> list[str]:
    # Retain whole FAQ answers whenever possible; long answers split on paragraph boundaries.
    if len(text) <= limit:
        return [text]
    result, current = [], ''
    for paragraph in text.splitlines():
        if not paragraph.strip():
            continue
        if current and len(current) + len(paragraph) > limit:
            result.append(current)
            current = ''
        while len(paragraph) > limit:
            if current:
                result.append(current); current = ''
            result.append(paragraph[:limit]); paragraph = paragraph[limit:]
        current += ('\n' if current else '') + paragraph
    if current:
        result.append(current)
    return result


def confined_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Manifest file path escapes the source directory')
    return path


async def import_manifest(manifest_path: Path) -> dict:
    ensure_rag_schema()
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    root = manifest_path.parent
    identity = await embedding_identity()
    run_id = str(uuid.uuid4())
    with connection() as conn:
        conn.execute("INSERT INTO rag_import_runs(id,manifest_path,status,details) VALUES(%s,%s,'processing',%s::jsonb)", (run_id, str(manifest_path), json.dumps({'embedding': identity})))
    document_count, chunk_count, skipped = 0, 0, 0
    try:
        catalog = list(csv.DictReader((ROOT / 'docs/ai-expected-answer/sources.csv').open(encoding='utf-8-sig')))
        sources_by_id = {row['source_id']: row for row in catalog}
        with connection() as conn:
            for source in catalog:
                conn.execute('INSERT INTO rag_sources(source_id,metadata) VALUES(%s,%s::jsonb) ON CONFLICT(source_id) DO UPDATE SET metadata=EXCLUDED.metadata,updated_at=NOW()', (source['source_id'], json.dumps(source, ensure_ascii=False)))
        for doc in manifest['documents']:
            if doc['review_status'] != 'verified':
                skipped += 1; continue
            if doc['source_id'] not in sources_by_id or not doc['official_url'].startswith('https://'):
                raise ValueError('Unknown source or invalid official URL')
            if urlsplit(doc['official_url']).hostname != urlsplit(sources_by_id[doc['source_id']]['url']).hostname:
                raise ValueError('Document host differs from the official source catalog')
            original_path = confined_path(root, doc['original_path'])
            if hashlib.sha256(original_path.read_bytes()).hexdigest() != doc['original_hash']:
                raise ValueError('Original document hash mismatch')
            sections = json.loads(confined_path(root, doc['sections_path']).read_text(encoding='utf-8'))
            serialized = json.dumps(sections, ensure_ascii=False, sort_keys=True)
            content_hash = hashlib.sha256(serialized.encode()).hexdigest()
            chunks = []
            for section in sections:
                for part, text in enumerate(split_section(section['text']), 1):
                    chunks.append({'heading': section['heading'], 'content': text, 'applicability': section.get('applicability', {}), 'locator': section['locator'] + (f' · 문단 {part}' if len(section['text']) > 1600 else '')})
            if not chunks:
                raise ValueError('No original answer text')
            with connection() as conn:
                existing = conn.execute('''SELECT d.id,d.active,(SELECT COUNT(*) FROM rag_chunks c WHERE c.document_id=d.id AND c.embedding_profile=%s) AS chunks
                    FROM rag_documents d WHERE d.source_id=%s AND d.official_url=%s AND d.content_hash=%s''',
                    (identity['profile'], doc['source_id'], doc['official_url'], content_hash)).fetchone()
            if existing and existing['chunks'] == len(chunks) and existing['active']:
                with connection() as conn:
                    conn.execute('UPDATE rag_documents SET metadata=%s::jsonb,original_path=%s,retrieved_at=%s WHERE id=%s', (json.dumps(doc, ensure_ascii=False), str(original_path), doc['retrieved_at'], existing['id']))
                skipped += 1; continue
            vectors = []
            for start in range(0, len(chunks), 4):
                batch = chunks[start:start + 4]
                vectors.extend(await embed_texts([doc['title'] + '\n' + item['heading'] + '\n' + item['content'] for item in batch]))
            document_id = str(existing['id']) if existing else str(uuid.uuid4())
            # Publish a whole source version only after every embedding is ready.
            with connection() as conn:
                conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('rag-import:' + doc['source_id'],))
                conn.execute('UPDATE rag_documents SET active=FALSE WHERE source_id=%s AND official_url=%s', (doc['source_id'], doc['official_url']))
                published = conn.execute('''INSERT INTO rag_documents(id,source_id,title,official_url,publisher,kind,version,jurisdiction,effective_from,effective_to,retrieved_at,content_hash,original_path,review_status,active)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'verified',TRUE)
                    ON CONFLICT(source_id,official_url,content_hash) DO UPDATE SET active=TRUE,retrieved_at=EXCLUDED.retrieved_at RETURNING id''',
                    (document_id, doc['source_id'], doc['title'], doc['official_url'], doc['publisher'], doc['kind'], doc['version'], doc['jurisdiction'], doc.get('effective_from'), doc.get('effective_to'), doc['retrieved_at'], content_hash, str(original_path))).fetchone()
                document_id = str(published['id'])
                conn.execute('UPDATE rag_documents SET metadata=%s::jsonb WHERE id=%s', (json.dumps(doc, ensure_ascii=False), document_id))
                conn.execute('DELETE FROM rag_chunks WHERE document_id=%s AND embedding_profile=%s', (document_id, identity['profile']))
                for ordinal, (chunk, vector) in enumerate(zip(chunks, vectors)):
                    conn.execute('''INSERT INTO rag_chunks(document_id,ordinal,heading,content,departments,type_ids,locator,embedding_profile,embedding,applicability)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s::jsonb)''',
                        (document_id, ordinal, chunk['heading'], chunk['content'], doc['departments'], doc['type_ids'], chunk['locator'], identity['profile'], vector_literal(vector), json.dumps(chunk['applicability'])))
            document_count += 1; chunk_count += len(chunks)
            print(f"{doc['source_id']}: {len(chunks)} chunks published", flush=True)
        result = {'run_id': run_id, 'documents': document_count, 'chunks': chunk_count, 'skipped': skipped, 'embedding': identity}
        with connection() as conn:
            conn.execute("UPDATE rag_import_runs SET status='completed',document_count=%s,chunk_count=%s,details=%s::jsonb,finished_at=NOW() WHERE id=%s", (document_count, chunk_count, json.dumps(result), run_id))
        return result
    except Exception as exc:
        with connection() as conn:
            conn.execute("UPDATE rag_import_runs SET status='failed',document_count=%s,chunk_count=%s,details=%s::jsonb,finished_at=NOW() WHERE id=%s", (document_count, chunk_count, json.dumps({'error': str(exc)}), run_id))
        raise
