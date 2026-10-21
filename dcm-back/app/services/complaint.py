import asyncio
import json
from typing import Any
import psycopg
import logging

from ..db import connection
from ..ai import ACTIVE_EMBEDDING_MODEL, CATEGORIES, analyze, embeddingForSelect, fallback, fingerprint
from ..dependencies import owner_id, vector_literal
from ..dedup import identity, item_identity, find_duplicate, lock_identities
from ..settings import EMBEDDING_DIMENSIONS
from ..departments import resolve_department

classification_slots = asyncio.Semaphore(1)
classification_tasks: set[asyncio.Task] = set()


class ImportVectorPolicy:
    """Optional file index: do not repeatedly compute vectors we cannot store.

    This is local to one import batch/job; the embedding function, model and
    independent RAG index are deliberately untouched.
    """
    def __init__(self, embedder=None):
        self.embedder = embedder or embeddingForSelect
        self.warning = None

    async def vector(self, record):
        if self.warning:
            record["_embedding_warning"] = self.warning
            return None
        vector = await self.embedder(record["content"])
        if vector and len(vector) != EMBEDDING_DIMENSIONS:
            self.warning = f"임베딩 {len(vector)}차원과 민원 DB {EMBEDDING_DIMENSIONS}차원이 달라 벡터 저장을 보류했습니다."
            record["_embedding_warning"] = self.warning
            return None
        return vector


def insert_complaint(conn: psycopg.Connection, record: dict[str, Any], source_file: str | None, source_row: int | None, stored_owner: str | None, vector: list[float] | None = None) -> bool:
    profile = record.get('_raw_identity') or identity(record['content'])
    lock_identities(conn, [profile], stored_owner)
    if find_duplicate(conn, profile, stored_owner):
        return False
    details = {key: record.get(key) for key in ["key_points", "urgency", "needs_review", "review_reason", "reason", "keywords", "confidence", "rule_decision", "llm_attempted"]}
    if record.get("_embedding_warning"):
        details["embedding_warning"] = record["_embedding_warning"]
    # Import success must not depend on the optional search index. Never pad or
    # truncate vectors, or change the configured embedding model / RAG index.
    if vector and len(vector) != EMBEDDING_DIMENSIONS:
        details["embedding_warning"] = f"임베딩 {len(vector)}차원과 민원 DB {EMBEDDING_DIMENSIONS}차원이 달라 벡터 저장을 보류했습니다."
        logging.getLogger(__name__).debug(details["embedding_warning"])
        vector = None
    metadata = json.dumps(details, ensure_ascii=False)
    department = resolve_department(conn, record["category"])
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO complaints (title,content,summary,category,source_file,source_row,content_fingerprint,processing_mode,llm_model,embedding_model,prompt_version,analysis_metadata,embedding,owner_user_id,
            raw_content_fingerprint,dedup_compact_fingerprint,dedup_profile,department_id)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,%s,%s,%s::jsonb,%s)
            ON CONFLICT DO NOTHING RETURNING id""",
            (record["title"], record["content"], record["summary"], record["category"], source_file, source_row, fingerprint(record["content"]), record.get("processing_mode", "fallback"), record.get("model"), ACTIVE_EMBEDDING_MODEL if vector else None, record.get("prompt_version"), metadata, vector_literal(vector), stored_owner,
             profile['raw'], profile['compact'], json.dumps(profile), department["id"] if department else None))
        return cur.fetchone() is not None

async def save_records(records: list[dict[str, Any]], actor: dict[str, Any], source_file: str | None = None) -> tuple[int, int]:
    saved = skipped = 0
    vectors = ImportVectorPolicy()
    profiles = [item_identity(item) for item in records]
    with connection() as conn:
        lock_identities(conn, profiles, owner_id(actor))
        for item, profile in zip(records, profiles):
            # Exact duplicates need neither another LLM call nor another vector.
            existing = find_duplicate(conn, profile, owner_id(actor))
            if existing:
                skipped += 1
                continue
            if item.get("source_file") or source_file:
                from .imports import analyze_import_record
                record = await analyze_import_record(item)
            else:
                record = await analyze(str(item.get("title", "")), str(item.get("content", ""))) if item.get("use_ai") else fallback(str(item.get("title", "")), str(item.get("content", "")))
            if item.get("category") in CATEGORIES and not item.get("use_ai") and record.get("processing_mode") != "llm":
                record["category"] = item["category"]
            if not record["content"]:
                continue
            record['_raw_identity'] = profile
            vector = await vectors.vector(record)
            was_saved = insert_complaint(conn, record, item.get("source_file") or source_file, item.get("source_row"), owner_id(actor), vector)
            saved += int(was_saved); skipped += int(not was_saved)
        conn.commit()
    return saved, skipped

async def classify_submission(complaint_id: int, revision: int) -> None:
    async with classification_slots:
        with connection() as conn:
            row = conn.execute("""UPDATE complaints SET analysis_state='processing'
                WHERE id=%s AND analysis_revision=%s AND analysis_state='pending'
                AND deleted_at IS NULL AND complaint_status='접수' RETURNING title,content""", (complaint_id, revision)).fetchone()
        if not row:
            return
        try:
            record = await analyze(row['title'], row['content'])
        except Exception:
            record = fallback(row['title'], row['content'])
        metadata = json.dumps({key: record.get(key) for key in ['key_points', 'urgency', 'needs_review', 'review_reason', 'reason', 'keywords']}, ensure_ascii=False)
        with connection() as conn:
            department = resolve_department(conn, record['category'])
            result = conn.execute("""UPDATE complaints SET summary=%s,category=%s,department_id=%s,analysis_state='completed',
                processing_mode=%s,llm_model=%s,prompt_version=%s,analysis_metadata=%s::jsonb
                WHERE id=%s AND analysis_revision=%s AND analysis_state='processing'
                AND deleted_at IS NULL AND complaint_status='접수' RETURNING id""",
                (record['summary'], record['category'], department['id'] if department else None, record.get('processing_mode', 'fallback'), record.get('model'), record.get('prompt_version'), metadata, complaint_id, revision)).fetchone()
        if not result:
            return
    try:
        vector = await embeddingForSelect(record["content"])
    except Exception:
        vector = None
    if vector and len(vector) == EMBEDDING_DIMENSIONS:
        with connection() as conn:
            conn.execute("""UPDATE complaints SET embedding=%s::vector,embedding_model=%s
                WHERE id=%s AND analysis_revision=%s AND analysis_state='completed'
                AND deleted_at IS NULL AND complaint_status<>'취소'""", (vector_literal(vector), ACTIVE_EMBEDDING_MODEL, complaint_id, revision))
