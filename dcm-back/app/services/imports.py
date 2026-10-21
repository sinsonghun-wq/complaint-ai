"""CSV processing independent of the web application and its lifespan.

Progress is published every small group; inserts and their checkpoint are
committed together every 500 source rows. A retry only resumes durable work.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from ..ai import analyze, embeddingForSelect, fallback
from ..db import connection
from ..settings import (
    CSV_LLM_CONFIDENCE_THRESHOLD, CSV_RULE_OTHER_MIN_CONTENT_CHARS,
    IMPORT_PROGRESS_ROWS, LLM_IMPORT_ENABLED, LLM_TIMEOUT_MS, MAX_BATCH_SIZE,
)
from .complaint import ImportVectorPolicy, insert_complaint
from .parser import csv_row_to_record, csv_rows
from .csv_reader import CsvRowError
from ..dedup import identity, lock_identities
from .file_rules import file_fallback

LOGGER = logging.getLogger("complaintai.import-worker")
_llm_unavailable_until = 0.0


def should_use_import_llm(record: dict, rule: dict) -> bool:
    if not LLM_IMPORT_ENABLED or not str(record.get("content") or "").strip():
        return False
    if (rule.get("rule_decision") or {}).get("requires_llm"):
        return True
    if len(rule.get("content") or "") < 20:
        return False
    if rule.get("category") == "기타" and len(record["content"]) >= CSV_RULE_OTHER_MIN_CONTENT_CHARS:
        return False
    return float(rule.get("confidence") or 0) < CSV_LLM_CONFIDENCE_THRESHOLD


async def analyze_import_record(record: dict) -> dict:
    global _llm_unavailable_until
    rule = file_fallback(record["title"], record["content"])
    result = rule
    attempted = False
    if should_use_import_llm(record, rule) and time.monotonic() >= _llm_unavailable_until:
        attempted = True
        try:
            candidate = await asyncio.wait_for(analyze(record["title"], record["content"]), timeout=min(60, LLM_TIMEOUT_MS / 1000))
            if candidate.get("processing_mode") == "llm":
                result = candidate
            else:
                result = {**rule, "reason": candidate.get("reason") or rule["reason"]}
                _llm_unavailable_until = time.monotonic() + 60
        except Exception as error:
            _llm_unavailable_until = time.monotonic() + 60
            result = {**rule, "reason": f"일괄 분석 LLM 응답을 받지 못해 규칙 결과를 유지했습니다. ({type(error).__name__})"}
    return {**result, "rule_decision": rule["rule_decision"],
            "llm_attempted": attempted,
            "source_row": record.get("source_row"), '_raw_identity': identity(record['content'])}


def update_progress(job_id: str, completed: int | None = None) -> None:
    with connection() as conn:
        conn.execute("""UPDATE import_jobs SET heartbeat_at=NOW(),completed_rows=COALESCE(%s,completed_rows)
            WHERE id=%s AND status='processing'""", (completed, job_id))


async def _heartbeat(job_id: str) -> None:
    while True:
        await asyncio.sleep(10)
        await asyncio.to_thread(update_progress, job_id)


async def _process(job: dict[str, Any]) -> None:
    job_id = str(job["id"])
    path = Path(job["storage_path"])
    if not path.is_file():
        raise FileNotFoundError("원본 CSV 파일을 찾지 못했습니다.")
    completed = int(job.get("checkpoint_rows") or 0)
    saved = int(job.get("saved_rows") or 0)
    skipped = int(job.get("skipped_rows") or 0)
    failed = int(job.get("failed_rows") or 0)
    batch: list[tuple[int, dict, dict | None, str | None]] = []
    vectors = ImportVectorPolicy(embeddingForSelect)

    def flush() -> None:
        nonlocal saved, skipped, failed
        if not batch:
            return
        batch_saved = batch_skipped = batch_failed = 0
        with connection() as conn:
            conn.execute("SELECT 1")  # Outer transaction; row transactions below are savepoints.
            lock_identities(conn, [record['_raw_identity'] for _, _, record, _ in batch if record is not None], job.get('owner_user_id'))
            for number, raw, record, error in batch:
                if record is not None:
                    try:
                        # A bad row must not abort the other 499 records.
                        with conn.transaction():
                            inserted = insert_complaint(conn, record, job["source_file"], number, job.get("owner_user_id"), record.pop("_embedding", None))
                        batch_saved += int(inserted)
                        batch_skipped += int(not inserted)
                    except Exception as exc:
                        error = f"저장 실패: {exc}"
                if error:
                    batch_failed += 1
                    conn.execute("INSERT INTO import_failures(job_id,source_row,raw_data,reason) VALUES(%s,%s,%s::jsonb,%s)",
                                 (job_id, number, json.dumps(raw, ensure_ascii=False), error[:2000]))
            conn.execute("""UPDATE import_jobs SET checkpoint_rows=%s,completed_rows=%s,
                saved_rows=%s,skipped_rows=%s,failed_rows=%s,heartbeat_at=NOW()
                WHERE id=%s AND status='processing'""",
                (completed, completed, saved + batch_saved, skipped + batch_skipped, failed + batch_failed, job_id))
        saved += batch_saved
        skipped += batch_skipped
        failed += batch_failed
        batch.clear()

    pulse = asyncio.create_task(_heartbeat(job_id))
    try:
        checkpoint = completed
        for number, raw in enumerate(csv_rows(path, job["encoding"]), start=2):
            if number <= checkpoint + 1:
                continue
            result = None
            error = None
            try:
                if isinstance(raw, CsvRowError):
                    raise ValueError(raw.reason)
                record = csv_row_to_record(raw, number, job["column_mapping"])
                if record is None:
                    raise ValueError("요약할 민원 본문이 비어 있습니다.")
                result = await analyze_import_record(record)
                result["_embedding"] = await vectors.vector(result)
            except Exception as exc:
                result = None
                error = str(exc)
            completed += 1
            batch.append((number, raw, result, error))
            if completed % IMPORT_PROGRESS_ROWS == 0:
                update_progress(job_id, completed)
            if len(batch) >= MAX_BATCH_SIZE:
                flush()
            await asyncio.sleep(0)
        flush()
        with connection() as conn:
            conn.execute("""UPDATE import_jobs SET status='completed',completed_rows=%s,
                checkpoint_rows=%s,total_rows=%s,saved_rows=%s,skipped_rows=%s,failed_rows=%s,
                completed_at=NOW(),heartbeat_at=NOW(),worker_id=NULL,last_error=NULL WHERE id=%s""",
                (completed, completed, completed, saved, skipped, failed, job_id))
    finally:
        pulse.cancel()
        with suppress(asyncio.CancelledError):
            await pulse


def process_csv_job(job: dict[str, Any]) -> None:
    try:
        asyncio.run(_process(job))
    except Exception as error:
        LOGGER.exception("CSV job %s failed", job["id"])
        with connection() as conn:
            conn.execute("""UPDATE import_jobs SET status='failed',completed_rows=checkpoint_rows,
                last_error=%s,heartbeat_at=NOW(),worker_id=NULL WHERE id=%s""", (str(error)[:2000], job["id"]))
