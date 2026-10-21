"""Run reference CSV imports independently of complaint processing and Uvicorn."""
import logging
import time

from .db import connection
from .embeddings import check_embedding_schema
from .rag_worker_manager import WORKER_LOCK_KEY
from .services.rag_imports import WORKER_ID, ensure_rag_import_schema, run_job


def run():
    ensure_rag_import_schema()
    check_embedding_schema()
    with connection() as lease:
        lease.autocommit = True
        if not lease.execute("SELECT pg_try_advisory_lock(%s) acquired", (WORKER_LOCK_KEY,)).fetchone()["acquired"]:
            return
        while True:
            lease.execute("SELECT 1")
            with connection() as db:
                db.execute("""UPDATE rag_import_jobs SET status='failed',stage='failed',completed_rows=checkpoint_rows,
                    worker_id=NULL,last_error='RAG 자료 처리 워커가 중단되었습니다. 이미 저장된 자료는 유지됩니다.'
                    WHERE status='processing' AND heartbeat_at < NOW()-INTERVAL '5 minutes'""")
                job = db.execute("""WITH next AS (SELECT id FROM rag_import_jobs WHERE status='queued' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
                    UPDATE rag_import_jobs j SET status='processing',stage='counting',started_at=COALESCE(started_at,NOW()),heartbeat_at=NOW(),worker_id=%s
                    FROM next WHERE j.id=next.id RETURNING j.*""", (WORKER_ID,)).fetchone()
            if job:
                run_job(job)
            else:
                time.sleep(0.5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
