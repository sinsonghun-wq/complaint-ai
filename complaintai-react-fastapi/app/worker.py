"""Persistent PostgreSQL-backed CSV import worker.

Run separately from Uvicorn so file imports survive web-server restarts:
    python -m app.worker
"""
from __future__ import annotations

import logging
import os
import socket
import time

from .db import connection
from .main import process_csv_job
from .settings import IMPORT_HEARTBEAT_TIMEOUT_SECONDS, IMPORT_WORKER_POLL_SECONDS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger("complaintai.import-worker")
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


def mark_stale_jobs_failed() -> int:
    """Expose interrupted jobs for an explicit retry without silently losing work."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE import_jobs
                SET status='failed', retry_count=retry_count+1,
                    last_error='작업 워커의 heartbeat가 중단되었습니다. 재처리 버튼으로 마지막 저장 지점부터 다시 시작할 수 있습니다.',
                    worker_id=NULL
                WHERE status='processing'
                  AND COALESCE(heartbeat_at, started_at, created_at) < NOW() - (%s * INTERVAL '1 second')
                RETURNING id""", (IMPORT_HEARTBEAT_TIMEOUT_SECONDS,))
            count = len(cur.fetchall())
        conn.commit()
    return count


def claim_next_job() -> dict | None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""WITH candidate AS (
                    SELECT id FROM import_jobs
                    WHERE status='queued'
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE import_jobs AS job
                SET status='processing', started_at=COALESCE(job.started_at, NOW()),
                    heartbeat_at=NOW(), worker_id=%s,
                    completed_rows=job.checkpoint_rows, last_error=NULL
                FROM candidate
                WHERE job.id=candidate.id
                RETURNING job.*""", (WORKER_ID,))
            job = cur.fetchone()
        conn.commit()
    return job


def run() -> None:
    LOGGER.info("CSV import worker started: %s", WORKER_ID)
    while True:
        stale = mark_stale_jobs_failed()
        if stale:
            LOGGER.warning("Marked %s stale import job(s) as failed.", stale)
        job = claim_next_job()
        if not job:
            time.sleep(IMPORT_WORKER_POLL_SECONDS)
            continue
        LOGGER.info("Processing import job %s (%s)", job["id"], job["source_file"])
        process_csv_job(job)


if __name__ == "__main__":
    run()
