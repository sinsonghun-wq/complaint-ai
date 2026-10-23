"""Deferred long sources, per-chunk checkpoints, and atomic search publication."""
import asyncio
import hashlib
import json
import uuid

from ..db import connection, fetch_one
from ..dependencies import vector_literal
from ..settings import ACTIVE_EMBEDDING_MODEL
from .rag_splitting import SPLITTER_VERSION, document_structure, split_document


def digest(content):
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def defer_document(db, job, item):
    """Called under the existing import job + dedup locks, in its row savepoint."""
    parent = db.execute('SELECT id,created_by,title FROM department_documents WHERE md5(content)=md5(%s) AND content=%s LIMIT 1',
                        (item['content'], item['content'])).fetchone()
    if parent:
        if parent['title'] != item['title']: return False
        state = db.execute('SELECT * FROM department_documents_chunk_state WHERE parent_id=%s FOR UPDATE', (parent['id'],)).fetchone()
        if state and (state['status'] == 'ready' or str(state['job_id']) == str(job['id'])):
            return False
        if str(parent['created_by']) != str(job['created_by']):
            return False
        if state and db.execute("SELECT 1 FROM rag_import_jobs WHERE id=%s AND status IN ('queued','processing')", (state['job_id'],)).fetchone():
            return False
        parent_id = parent['id']
    else:
        parent_id = uuid.uuid4()
        db.execute('''INSERT INTO department_documents(id,document_id,department_id,title,original_name,version,storage_path,content,created_by)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (parent_id, job['document_id'], job['department_id'], item['title'], job['source_file'], item['source_row']-1,
             job['storage_path'], item['content'], job['created_by']))
    db.execute('''INSERT INTO department_documents_chunk_state(parent_id,job_id,source_row,content_hash,token_count,embedding_model,splitter_version,metadata)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT(parent_id) DO UPDATE SET job_id=EXCLUDED.job_id,source_row=EXCLUDED.source_row,
            status='deferred',last_error=NULL,updated_at=NOW()''',
        (parent_id, job['id'], item['source_row'], digest(item['content']), item['token_count'], job['embedding_model'],
         SPLITTER_VERSION, json.dumps(item.get('metadata', {}), ensure_ascii=False)))
    return True


def active(db, job_id):
    return db.execute("SELECT id FROM rag_import_jobs WHERE id=%s AND status='processing' FOR UPDATE", (job_id,)).fetchone()


def yield_to_short_jobs(job_id):
    with connection() as db:
        if not active(db, job_id): return True
        short = db.execute("SELECT 1 FROM rag_import_jobs WHERE status='queued' AND stage<>'chunking' AND id<>%s LIMIT 1", (job_id,)).fetchone()
        if short:
            db.execute("UPDATE rag_import_jobs SET status='queued',stage='chunking',worker_id=NULL,heartbeat_at=NOW() WHERE id=%s", (job_id,))
            return True
    return False


def plan(job, source):
    content = source['content']
    if digest(content) != source['content_hash']:
        raise ValueError('원문이 변경되어 저장된 조각과 일치하지 않습니다. 원문을 덮어쓰지 않고 처리를 중단했습니다.')
    if source['splitter_version'] != SPLITTER_VERSION or source['embedding_model'] != job['embedding_model'] or job['embedding_model'] != ACTIVE_EMBEDDING_MODEL:
        raise ValueError('조각 생성 방식 또는 임베딩 모델이 달라 안전하게 이어서 처리할 수 없습니다.')
    chunks = list(split_document(content)) if not source['total_chunks'] else None
    structure = document_structure(content) if chunks is not None else source['structure']
    with connection() as db:
        if not active(db, job['id']): return False
        db.execute("SELECT parent_id FROM department_documents_chunk_state WHERE parent_id=%s FOR UPDATE", (source['parent_id'],))
        if chunks is not None:
            for index, chunk in enumerate(chunks):
                db.execute('''INSERT INTO department_documents_chunks(id,parent_id,chunk_index,start_offset,end_offset,content,token_count)
                    VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(parent_id,chunk_index) DO NOTHING''',
                    (uuid.uuid4(), source['parent_id'], index, chunk['start_offset'], chunk['end_offset'], chunk['content'], chunk['token_count']))
            db.execute('''UPDATE department_documents_chunk_state SET structure=%s::jsonb,total_chunks=%s WHERE parent_id=%s''',
                       (json.dumps(structure, ensure_ascii=False), len(chunks), source['parent_id']))
        db.execute("UPDATE department_documents_chunk_state SET status='processing',last_error=NULL,updated_at=NOW() WHERE parent_id=%s", (source['parent_id'],))
        db.execute('''UPDATE rag_import_jobs SET stage='chunking',active_source_row=%s,
            active_chunks_total=(SELECT total_chunks FROM department_documents_chunk_state WHERE parent_id=%s),
            active_chunks_completed=(SELECT count(*) FROM department_documents_chunks WHERE parent_id=%s AND embedding IS NOT NULL),heartbeat_at=NOW() WHERE id=%s''',
            (source['source_row'], source['parent_id'], source['parent_id'], job['id']))
    return True


def save_chunk(job, source, chunk, vector):
    with connection() as db:
        if not active(db, job['id']): return False
        db.execute('UPDATE department_documents_chunks SET embedding=%s::vector WHERE id=%s AND embedding IS NULL', (vector_literal(vector), chunk['id']))
        db.execute('''UPDATE department_documents_chunk_state SET completed_chunks=(SELECT count(*) FROM department_documents_chunks
            WHERE parent_id=%s AND embedding IS NOT NULL),updated_at=NOW() WHERE parent_id=%s''', (source['parent_id'], source['parent_id']))
        db.execute('''UPDATE rag_import_jobs SET active_chunks_completed=(SELECT completed_chunks FROM department_documents_chunk_state WHERE parent_id=%s),
            heartbeat_at=NOW() WHERE id=%s''', (source['parent_id'], job['id']))
    return True


def finish_source(job, source, error=None):
    with connection() as db:
        if not active(db, job['id']): return False
        current = db.execute('''SELECT s.*,p.content FROM department_documents_chunk_state s
            JOIN department_documents p ON p.id=s.parent_id WHERE s.parent_id=%s FOR UPDATE OF s''', (source['parent_id'],)).fetchone()
        if current['status'] in ('ready','failed'): return True
        if not error:
            if digest(current['content']) != current['content_hash']:
                error = '임베딩 도중 원문이 변경되었습니다. 조각 검색에 게시하지 않았습니다.'
            else:
                pieces = db.execute('''SELECT content,start_offset,end_offset,(embedding IS NOT NULL) AS embedded
                    FROM department_documents_chunks WHERE parent_id=%s ORDER BY chunk_index''', (source['parent_id'],)).fetchall()
                position = 0
                valid = len(pieces) == current['total_chunks'] and bool(pieces)
                for piece in pieces:
                    valid = valid and piece['embedded'] and piece['start_offset'] == position and piece['content'] == current['content'][piece['start_offset']:piece['end_offset']]
                    position = piece['end_offset']
                if not valid or position != len(current['content']):
                    raise ValueError('전체 원문에 대응하는 모든 조각 임베딩을 완료하기 전에는 검색에 게시할 수 없습니다.')
        db.execute('''UPDATE department_documents_chunk_state SET status=%s,last_error=%s,updated_at=NOW() WHERE parent_id=%s''',
                   ('failed' if error else 'ready', error, source['parent_id']))
        if error:
            db.execute("INSERT INTO rag_import_failures(job_id,source_row,reason,phase) VALUES(%s,%s,%s,'chunk')", (job['id'], source['source_row'], str(error)[:2000]))
        db.execute('''UPDATE rag_import_jobs SET long_processed_rows=long_processed_rows+1,
            completed_rows=checkpoint_rows-deferred_rows+long_processed_rows+1,saved_rows=saved_rows+%s,failed_rows=failed_rows+%s,
            active_source_row=NULL,active_chunks_total=0,active_chunks_completed=0,heartbeat_at=NOW() WHERE id=%s''',
            (0 if error else 1, 1 if error else 0, job['id']))
    return True


async def process_deferred(job, embed):
    with connection() as db:
        parents = db.execute('''SELECT parent_id FROM department_documents_chunk_state
            WHERE job_id=%s AND status IN ('deferred','processing') ORDER BY source_row''', (job['id'],)).fetchall()
    for parent in parents:
        if await asyncio.to_thread(yield_to_short_jobs, job['id']): return False
        source = await asyncio.to_thread(fetch_one, '''SELECT s.*,p.content FROM department_documents_chunk_state s
            JOIN department_documents p ON p.id=s.parent_id WHERE s.parent_id=%s''', (parent['parent_id'],))
        if not source: continue
        try:
            if not await asyncio.to_thread(plan, job, source): return False
            while True:
                if await asyncio.to_thread(yield_to_short_jobs, job['id']): return False
                chunk = await asyncio.to_thread(fetch_one, '''SELECT id,content FROM department_documents_chunks
                    WHERE parent_id=%s AND embedding IS NULL ORDER BY chunk_index LIMIT 1''', (source['parent_id'],))
                if not chunk: break
                vector = await embed(job['id'], chunk['content'])
                if vector is None: return False
                if not await asyncio.to_thread(save_chunk, job, source, chunk, vector): return False
            if not await asyncio.to_thread(finish_source, job, source): return False
        except Exception as error:
            if not await asyncio.to_thread(finish_source, job, source, f'긴 원문의 조각 임베딩을 완료하지 못했습니다. {str(error)[:1500]}'):
                return False
    return True
