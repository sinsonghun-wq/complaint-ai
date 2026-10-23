"""Extend the existing three-source RAG lookup with published long-source spans."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import time
from collections import deque
from datetime import date

import httpx
from psycopg.errors import QueryCanceled

from ..db import connection
from ..logger import logger
from ..settings import ACTIVE_EMBEDDING_MODEL, LLAMA_SERVER_URL, LLM_BACKEND, LLM_NUM_CTX
from .rag_splitting import TOKEN_LIMIT, document_structure, token_count

ROW_LIMIT = 3
CONTEXT_LIMIT = min(8192, max(512, LLM_NUM_CTX // 3))
CONTEXT_CHAR_LIMIT = 5000
INCOMPLETE_NOTICE_CHARS = 160
EXTERNAL_SOURCE_LABEL = '[명시적으로 참조된 다른 법령의 근거]\n'
RETRIEVAL_SECONDS = 600
MAX_REFERENCE_SOURCES = 16
MAX_REFERENCE_CLAUSES = 96


class ReferenceLimitError(ValueError):
    pass


def fetch_all(sql, params=()):
    try:
        with connection(connect_timeout=3) as db:
            db.execute("SELECT set_config('statement_timeout','5000',true)")
            return db.execute(sql, params).fetchall()
    except QueryCanceled as error:
        raise ReferenceLimitError('참고 자료 조회가 제한 시간을 초과했습니다.') from error


def fetch_one(sql, params=()):
    rows = fetch_all(sql, params)
    return rows[0] if rows else None


class ReferenceTraversal:
    """One request's bounded graph and caches; never shared across users."""
    def __init__(self):
        self.deadline = time.monotonic() + RETRIEVAL_SECONDS
        self.cancelled = threading.Event()
        self.sources = {}
        self.references = {}

    def check(self):
        if self.cancelled.is_set() or time.monotonic() >= self.deadline:
            raise ReferenceLimitError('참조 근거 검색 시간을 초과하여 추가 조회를 중단했습니다.')

    def source(self, parent_id):
        self.check()
        identifier = str(parent_id)
        if identifier not in self.sources:
            if len(self.sources) >= MAX_REFERENCE_SOURCES:
                raise ReferenceLimitError('연결된 법령이 많아 한 번의 답변에서 적용 근거를 모두 확인할 수 없습니다.')
            source = load_source(parent_id)
            if source and hashlib.sha256(source['content'].encode('utf-8')).hexdigest() != source['content_hash']:
                raise ReferenceLimitError('원문 변경으로 조각과의 일치 여부를 확인할 수 없습니다.')
            self.sources[identifier] = source
        self.check()
        return self.sources[identifier]

    def reference(self, law, as_of):
        self.check()
        cache_key = (law, as_of)
        if cache_key not in self.references:
            if len(self.references) >= 32:
                raise ReferenceLimitError('연결된 법령의 조회 한도를 초과했습니다.')
            rows = fetch_all('''SELECT p.id AS parent_id,p.title,COALESCE(s.metadata,'{}'::jsonb) AS metadata
                FROM department_documents p LEFT JOIN department_documents_chunk_state s ON s.parent_id=p.id
                WHERE md5(p.title)=md5(%s) AND p.title=%s AND p.deleted_at IS NULL
                AND ((s.status='ready' AND s.embedding_model=%s) OR (s.parent_id IS NULL AND p.embedding IS NOT NULL))''',
                (law, law, ACTIVE_EMBEDDING_MODEL))
            eligible, warning = choose_versions(rows, as_of)
            self.references[cache_key] = [] if warning else [r for r in rows if str(r['parent_id']) in eligible]
        self.check()
        return self.references[cache_key]


def legacy_lookup(vector):
    # Keep the original short-only path and output unchanged.
    return fetch_all('''SELECT SUBSTRING(content, 1, 5000) AS content FROM department_documents
        WHERE deleted_at IS NULL ORDER BY embedding <=> %s::vector LIMIT 3''', (str(vector),))


def lookup(vector, as_of=None):
    if not fetch_one('SELECT parent_id FROM department_documents_chunk_state LIMIT 1'):
        return None
    short = fetch_all('''SELECT p.id AS parent_id,p.title,SUBSTRING(p.content,1,5000) AS content,
            p.embedding <=> %s::vector AS distance,'short' AS kind
        FROM department_documents p WHERE p.deleted_at IS NULL AND p.embedding IS NOT NULL
        AND NOT EXISTS(SELECT 1 FROM department_documents_chunk_state s WHERE s.parent_id=p.id)
        ORDER BY p.embedding <=> %s::vector LIMIT 3''', (str(vector), str(vector)))
    catalog = fetch_all('''SELECT s.parent_id,p.title,s.metadata FROM department_documents_chunk_state s
        JOIN department_documents p ON p.id=s.parent_id WHERE s.status='ready' AND s.completed_chunks=s.total_chunks
        AND s.total_chunks>0 AND p.deleted_at IS NULL AND s.embedding_model=%s''', (ACTIVE_EMBEDDING_MODEL,))
    groups = {}
    for row in catalog:
        identifier = row['metadata'].get('law_id')
        groups.setdefault(('id',identifier) if identifier else ('title',row['title']), []).append(row)
    excluded = []; ambiguous = {}
    for rows in groups.values():
        eligible, warning = choose_versions(rows, as_of or date.today())
        excluded.extend(str(row['parent_id']) for row in rows if str(row['parent_id']) not in eligible)
        if warning:
            for row in rows: ambiguous[str(row['parent_id'])] = f"{row['title']}: {warning}"
    excluded_ids = set(excluded)
    eligible_ids = [str(row['parent_id']) for row in catalog if str(row['parent_id']) not in excluded_ids]
    # Apply publication/version eligibility BEFORE top-k, while allowing the
    # vector index to run before joins. Cross-column state equality otherwise
    # made PostgreSQL sort every chunk despite the HNSW index.
    chunks = fetch_all('''WITH hits AS MATERIALIZED (
            SELECT c.parent_id,c.chunk_index,c.start_offset,c.end_offset,c.embedding <=> %s::vector AS distance
            FROM department_documents_chunks c WHERE c.embedding IS NOT NULL AND c.parent_id=ANY(%s::uuid[])
            ORDER BY c.embedding <=> %s::vector LIMIT 24)
        SELECT hits.*,p.title,s.metadata,'long' AS kind FROM hits
        JOIN department_documents_chunk_state s ON s.parent_id=hits.parent_id JOIN department_documents p ON p.id=hits.parent_id
        WHERE s.status='ready' AND s.completed_chunks=s.total_chunks AND s.total_chunks>0
          AND s.embedding_model=%s AND p.deleted_at IS NULL ORDER BY hits.distance''',
        (str(vector), eligible_ids, str(vector), ACTIVE_EMBEDDING_MODEL))
    warnings = []
    if ambiguous:
        closest = fetch_one('''SELECT c.parent_id,c.embedding <=> %s::vector AS distance FROM department_documents_chunks c
            JOIN department_documents_chunk_state s ON s.parent_id=c.parent_id JOIN department_documents p ON p.id=c.parent_id
            WHERE c.parent_id=ANY(%s::uuid[]) AND c.embedding IS NOT NULL AND s.status='ready' AND p.deleted_at IS NULL
            ORDER BY c.embedding <=> %s::vector LIMIT 1''', (str(vector), list(ambiguous), str(vector)))
        distances = [row['distance'] for row in short + chunks]
        if closest and (not distances or closest['distance'] <= min(distances)):
            warnings.append(ambiguous[str(closest['parent_id'])])
    return short, chunks, warnings


def effective_date(value):
    match = re.search(r'(\d{4})\D{1,3}(\d{1,2})\D{1,3}(\d{1,2})', str(value or ''))
    if not match: return None
    try: return date(*map(int, match.groups()))
    except ValueError: return None


def choose_versions(rows, as_of):
    dated = [(effective_date(row['metadata'].get('effective_date')), str(row['parent_id'])) for row in rows]
    if len(rows) == 1:
        day, identifier = dated[0]
        return ({identifier} if not day or day <= as_of else set()), None
    if any(day is None for day, _ in dated):
        versions = {str(row['metadata'].get('law_version') or '') for row in rows}
        if len(versions) > 1 or '' in versions:
            return set(), '동일 법령의 여러 자료가 있으나 시행일·개정본을 구분할 수 없습니다.'
        return {identifier for day, identifier in dated if day is None or day <= as_of}, None
    valid = [day for day, _ in dated if day <= as_of]
    if not valid: return set(), None
    latest = max(valid)
    owners = [identifier for day, identifier in dated if day == latest]
    if len(owners) != 1: return set(), '동일 시행일의 개정 자료가 여러 개여서 근거를 확정할 수 없습니다.'
    return set(owners), None


def version_eligible(candidate, as_of):
    metadata = candidate.get('metadata') or {}
    rows = fetch_all('''SELECT p.id AS parent_id,COALESCE(s.metadata,'{}'::jsonb) AS metadata
        FROM department_documents p LEFT JOIN department_documents_chunk_state s ON s.parent_id=p.id
        WHERE p.deleted_at IS NULL AND ((s.status='ready' AND s.embedding_model=%s) OR (s.parent_id IS NULL AND p.embedding IS NOT NULL))
          AND (CASE WHEN %s<>'' THEN s.metadata->>'law_id'=%s ELSE p.title=%s END)''',
        (ACTIVE_EMBEDDING_MODEL, metadata.get('law_id',''), metadata.get('law_id',''), candidate['title']))
    eligible, warning = choose_versions(rows, as_of)
    return str(candidate['parent_id']) in eligible, warning


def load_source(parent_id):
    source = fetch_one('''SELECT p.id,p.title,p.content,s.content_hash,s.structure,COALESCE(s.metadata,'{}'::jsonb) AS metadata
        FROM department_documents p LEFT JOIN department_documents_chunk_state s ON s.parent_id=p.id
        WHERE p.id=%s AND p.deleted_at IS NULL AND ((s.status='ready' AND s.embedding_model=%s)
            OR (s.parent_id IS NULL AND p.embedding IS NOT NULL))''', (parent_id, ACTIVE_EMBEDDING_MODEL))
    if source and source['content_hash'] is None:
        # A long source can explicitly cite an ordinary short source. Its storage
        # and normal vector lookup remain unchanged; expand only this citation.
        if token_count(source['content']) > TOKEN_LIMIT: return None
        source['structure'] = document_structure(source['content'])
        source['content_hash'] = hashlib.sha256(source['content'].encode('utf-8')).hexdigest()
    elif source:
        # Repair derived reference parsing on read, without changing originals,
        # persisted chunks, their vectors, or import/resume checkpoints.
        source['structure'] = document_structure(source['content'])
    return source


def source_header(document):
    meta = document['metadata']
    return f"[원문 ID: {document['id']} | 법령·자료명: {document['title']} | 시행일: {meta.get('effective_date') or '미제공'} | 개정 정보: {meta.get('law_version') or '미제공'}]"


def clause_content(document, number):
    unit = document['structure'][number]
    return f"[{unit['heading']}]\n{document['content'][unit['start']:unit['end']]}"


def incomplete_content(warnings, max_chars, *, source=None, parent_id=None):
    """Bound notices only; legal clauses are never cut to fit the budget."""
    detail = ' '.join(dict.fromkeys(warnings))
    prefix = '근거 확인 미완료: '
    suffix = '\n필수 조항·예외·참조를 모두 확인하지 못했으므로 적용 여부를 단정하지 마세요.'
    header = source_header(source) if source else f'[원문 ID: {parent_id}]' if parent_id else ''
    content = (header+'\n' if header else '') + prefix + detail + suffix
    if len(content) <= max_chars:
        return content
    available = max_chars-len(prefix)-len(suffix)
    if available >= 0:
        return prefix+detail[:available]+suffix
    return '근거 확인 미완료. 적용 여부를 단정하지 마세요.'[:max(0,max_chars)]


def source_context(parent_id, seeds, *, _context=None, max_chars=None):
    max_chars = CONTEXT_CHAR_LIMIT if max_chars is None else max(0,max_chars)
    context = _context or ReferenceTraversal()
    source = None
    warnings = []
    selected = {}
    pending = deque()
    visited = {}
    indexes = {}
    evidence_chars = 0

    def index_for(document):
        identifier = str(document['id'])
        if identifier not in indexes:
            by_key = {}; reverse = {}; definitions = []
            for number, unit in enumerate(document['structure']):
                by_key.setdefault(unit['key'], []).append(number)
                if unit['exception']:
                    for ref in unit['references']: reverse.setdefault(ref, []).append(number)
                if re.search(r'\((?:정의|적용\s*범위|적용\s*제외)\)', unit['heading']): definitions.append(number)
            indexes[identifier] = (by_key, reverse, definitions)
        return indexes[identifier]

    def enqueue(document, number, depth, required=True):
        nonlocal evidence_chars
        identifier = str(document['id'])
        identity = (identifier, number)
        if identity in visited and (visited[identity] or not required): return
        context.check()
        if identity not in visited and len(visited) >= MAX_REFERENCE_CLAUSES:
            raise ReferenceLimitError('연결된 조항이 많아 한 번의 답변에서 전체 적용 조건을 확인할 수 없습니다.')
        if identity not in visited:
            added = len(clause_content(document, number))
            if identifier in selected:
                added += 2
            else:
                added += len(source_header(document))+1
                if identifier != str(parent_id): added += len(EXTERNAL_SOURCE_LABEL)
                if selected: added += 2
            # Check before scheduling a clause's outgoing references. This limits
            # traversal work, not just the final text after all queries finish.
            if evidence_chars+added > max_chars:
                raise ReferenceLimitError(f'답변 근거 한도를 초과했습니다({max_chars:,}자). 필수 조항·예외·참조를 모두 전달할 수 없습니다.')
            evidence_chars += added
        visited[identity] = required
        selected.setdefault(identifier, set()).add(number)
        pending.append((identifier, number, depth, required))

    def seed_context(document, numbers, depth):
        units = document['structure']
        for number in numbers:
            enqueue(document, number, depth)
            for adjacent in (number-1, number+1):
                if 0 <= adjacent < len(units) and units[adjacent]['exception']: enqueue(document, adjacent, depth)

    try:
        source = context.source(parent_id)
        if not source: return None
        initial = [i for i,u in enumerate(source['structure']) if any(u['start'] < s['end_offset'] and u['end'] > s['start_offset'] for s in seeds)]
        seed_context(source, initial, 0)
        while pending:
            context.check()
            identifier, number, depth, required = pending.popleft()
            document = context.source(identifier)
            units = document['structure']; unit = units[number]
            by_key, reverse, definitions = index_for(document)
            body = document['content'][unit['start']:unit['end']]
            # Definitions/scope are whole-clause supporting context. Their
            # unrelated branches must not recursively crawl the legal corpus.
            # A direct reference to the same clause promotes it to required.
            if not required: continue
            # Chapter-specific definitions apply when their defined term occurs
            # in the selected rule; do not drag every chapter's laws into it.
            for definition in definitions:
                item = units[definition]
                if '정의' not in item['heading']:
                    if item['start'] < unit['start']: enqueue(document, definition, depth, False)
                else:
                    terms = re.findall(r'["“]([^"”]{1,50})["”]', document['content'][item['start']:item['end']])
                    if any(term in body for term in terms): enqueue(document, definition, depth, False)
            for backlink in reverse.get(unit['key'], []): enqueue(document, backlink, depth)
            for reference in unit['references']:
                matches = by_key.get(reference, [])
                if not matches: warnings.append(f"{document['title']}: 참조한 {reference}의 원문이 이 자료에 없습니다.")
                elif len(matches) != 1: warnings.append(f"{document['title']}: {reference}가 중복되어 참조 대상을 확정할 수 없습니다.")
                else: enqueue(document, matches[0], depth)
            for reference in unit.get('external', []):
                if depth >= 3:
                    warnings.append(f"외부 법령 ‘{reference['law']}’의 연결 근거가 깊어 모두 확인하지 못했습니다."); continue
                as_of = effective_date(document['metadata'].get('effective_date')) or date.today()
                related = context.reference(reference['law'], as_of)
                if len(related) != 1:
                    warnings.append(f"외부 법령 ‘{reference['law']}’의 {reference['key']}에 해당하는 개정본·원문을 확정하지 못했습니다."); continue
                target = context.source(related[0]['parent_id'])
                matches = index_for(target)[0].get(reference['key'], []) if target else []
                if len(matches) != 1:
                    warnings.append(f"외부 법령 ‘{reference['law']}’의 {reference['key']}를 찾지 못했습니다."); continue
                seed_context(target, matches, depth+1)
    except ReferenceLimitError as error:
        warnings.append(str(error))
    metadata = source['metadata'] if source else {}
    title = source['title'] if source else '자료'

    if warnings:
        content = incomplete_content(warnings,max_chars,source=source,parent_id=parent_id)
    else:
        parts = []
        for identifier, numbers in selected.items():
            document = context.sources[identifier]
            text = source_header(document) + '\n' + '\n\n'.join(clause_content(document,i) for i in sorted(numbers))
            if identifier != str(parent_id): text = EXTERNAL_SOURCE_LABEL + text
            parts.append(text)
        content = '\n\n'.join(parts)
    return dict(content=content,parent_id=str(parent_id),title=title,metadata=metadata,
                context_complete=not warnings,clauses=[source['structure'][i]['key'] for i in sorted(selected.get(str(parent_id),set()))] if source else [],warnings=warnings)

async def chat_tokens(content):
    if LLM_BACKEND == 'llama_server':
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                result = await client.post(LLAMA_SERVER_URL+'/tokenize', json={'content':content,'add_special':False})
                result.raise_for_status()
                return len(result.json()['tokens'])
        except (httpx.HTTPError, ValueError, KeyError, TypeError): pass
    # Byte fallback deliberately overestimates ordinary byte-token vocabularies.
    return len(content.encode('utf-8'))


async def _retrieve(vector, query, context):
    as_of = effective_date(query) or date.today()
    candidates = await asyncio.to_thread(lookup, vector, as_of)
    if candidates is None: return await asyncio.to_thread(legacy_lookup, vector)
    short, chunks, warnings = candidates
    if not chunks:
        results = [{'content':row['content']} for row in short]
        if warnings: results.append({'content':incomplete_content(warnings,CONTEXT_CHAR_LIMIT), 'context_complete':False})
        return results
    ranked = sorted(short + chunks, key=lambda row:row['distance'])
    selected = []; seen = set()
    for candidate in ranked:
        parent = str(candidate['parent_id'])
        if parent in seen: continue
        seen.add(parent); selected.append(candidate)
        if len(selected) == ROW_LIMIT: break
    results = []; used = 0; long_chars = 0
    long_remaining = sum(row['kind'] != 'short' for row in selected) + bool(warnings)
    for candidate in selected:
        if candidate['kind'] == 'short':
            record = {'content':candidate['content']}
        else:
            long_remaining -= 1
            # Keep room for an explicit incomplete notice for every remaining
            # long source. All long evidence and notices share 5,000 characters.
            max_chars = max(0,CONTEXT_CHAR_LIMIT-long_chars-INCOMPLETE_NOTICE_CHARS*long_remaining)
            seeds = [row for row in chunks if str(row['parent_id']) == str(candidate['parent_id'])][:1]
            record = await asyncio.to_thread(source_context, candidate['parent_id'], seeds, _context=context, max_chars=max_chars)
            if not record: continue
        count = await chat_tokens(record['content'])
        if used + count > CONTEXT_LIMIT:
            if candidate['kind'] == 'short':
                # Original short text stays unchanged; prioritize it over optional long evidence.
                results.append(record); used += count; continue
            record = {'content':incomplete_content(['관련 조항과 예외·참조를 모두 전달하면 답변 근거 한도를 초과합니다.'],max_chars,parent_id=candidate['parent_id']),
                      'parent_id':str(candidate['parent_id']), 'context_complete':False, 'warnings':['required_context_exceeds_budget']}
            count = await chat_tokens(record['content'])
        results.append(record); used += count
        if candidate['kind'] != 'short': long_chars += len(record['content'])
    if warnings: results.append({'content':incomplete_content(warnings,CONTEXT_CHAR_LIMIT-long_chars), 'context_complete':False})
    return results


async def retrieve(vector, query=''):
    context = ReferenceTraversal()
    started = time.monotonic()
    try:
        async with asyncio.timeout(RETRIEVAL_SECONDS):
            return await _retrieve(vector, query, context)
    except (TimeoutError, ReferenceLimitError) as error:
        logger.warning('[RAG] retrieval deadline/limit: %s', str(error))
        return [{'content':'참고 자료 검색이 제한 시간을 초과했습니다. 확인되지 않은 근거로 답변을 단정하지 말고 다시 시도하도록 안내하세요.', 'context_complete':False}]
    finally:
        context.cancelled.set()
        logger.info('[RAG] retrieval finished seconds=%.2f sources=%s law_lookups=%s', time.monotonic()-started,len(context.sources),len(context.references))
