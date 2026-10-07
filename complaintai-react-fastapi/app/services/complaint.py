import asyncio
import json
from typing import Any
import psycopg

from ..db import connection
from ..ai import CATEGORIES, analyze, embedding, fallback, fingerprint
from ..dependencies import owner_id, vector_literal

classification_slots = asyncio.Semaphore(1)
classification_tasks: set[asyncio.Task] = set()

def insert_complaint(conn: psycopg.Connection, record: dict[str, Any], source_file: str | None, source_row: int | None, stored_owner: str | None, vector: list[float] | None = None) -> bool:
    metadata = json.dumps({key: record.get(key) for key in ["key_points", "urgency", "needs_review", "review_reason", "reason", "keywords"]}, ensure_ascii=False)
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO complaints (title,content,summary,category,source_file,source_row,content_fingerprint,processing_mode,llm_model,embedding_model,prompt_version,analysis_metadata,embedding,owner_user_id)
            SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s
            WHERE NOT EXISTS (SELECT 1 FROM complaints WHERE content_fingerprint=%s AND deleted_at IS NULL AND complaint_status<>'취소' AND owner_user_id IS NOT DISTINCT FROM %s) RETURNING id""",
            (record["title"], record["content"], record["summary"], record["category"], source_file, source_row, fingerprint(record["content"]), record.get("processing_mode", "fallback"), record.get("model"), "Qwen/Qwen3-Embedding-4B" if vector else None, record.get("prompt_version"), metadata, vector_literal(vector), stored_owner, fingerprint(record["content"]), stored_owner))
        return cur.fetchone() is not None

async def save_records(records: list[dict[str, Any]], actor: dict[str, Any], source_file: str | None = None) -> tuple[int, int]:
    saved = skipped = 0
    with connection() as conn:
        for item in records:
            record = await analyze(str(item.get("title", "")), str(item.get("content", ""))) if item.get("use_ai") else fallback(str(item.get("title", "")), str(item.get("content", "")))
            if item.get("category") in CATEGORIES and not item.get("use_ai"):
                record["category"] = item["category"]
            if not record["content"]:
                continue
            was_saved = insert_complaint(conn, record, item.get("source_file") or source_file, item.get("source_row"), owner_id(actor), await embedding(record))
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
            result = conn.execute("""UPDATE complaints SET summary=%s,category=%s,analysis_state='completed',
                processing_mode=%s,llm_model=%s,prompt_version=%s,analysis_metadata=%s::jsonb
                WHERE id=%s AND analysis_revision=%s AND analysis_state='processing'
                AND deleted_at IS NULL AND complaint_status='접수' RETURNING id""",
                (record['summary'], record['category'], record.get('processing_mode', 'fallback'), record.get('model'), record.get('prompt_version'), metadata, complaint_id, revision)).fetchone()
        if not result:
            return
    try:
        vector = await embedding(record)
    except Exception:
        vector = None
    if vector:
        with connection() as conn:
            conn.execute("""UPDATE complaints SET embedding=%s::vector,embedding_model='Qwen/Qwen3-Embedding-4B'
                WHERE id=%s AND analysis_revision=%s AND analysis_state='completed'
                AND deleted_at IS NULL AND complaint_status<>'취소'""", (vector_literal(vector), complaint_id, revision))