from __future__ import annotations

import asyncio
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader

from app.agent import lifespan
from .db import connection, fetch_all, fetch_one
from .config import WEB_ORIGINS, FILE_STORAGE_DIR
from .services.complaint import classification_tasks, classify_submission
from .routers import auth, complaints

auth_header = APIKeyHeader(name="Authorization", auto_error=False)
app = FastAPI(title="ComplaintAI FastAPI", version="1.0.0", dependencies=[Depends(auth_header)], lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=WEB_ORIGINS if WEB_ORIGINS != ["*"] else ["*"], allow_credentials=WEB_ORIGINS != ["*"], allow_methods=["*"], allow_headers=["*"])

FILE_STORAGE_DIR.mkdir(parents=True, exist_ok=True)

# 라우터 등록
app.include_router(auth.router)
app.include_router(complaints.router)

@app.on_event("startup")
async def startup() -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.execute("ALTER TABLE complaint_responses ADD COLUMN IF NOT EXISTS response_state TEXT NOT NULL DEFAULT 'sent'")
            cur.execute("ALTER TABLE complaint_responses ADD COLUMN IF NOT EXISTS sent_at TIMESTAMPTZ")
            cur.execute("UPDATE complaint_responses SET sent_at=created_at WHERE response_state='sent' AND sent_at IS NULL")
            cur.execute("ALTER TABLE complaints ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ")
            cur.execute("ALTER TABLE complaints ADD COLUMN IF NOT EXISTS cancelled_by_role TEXT")
            cur.execute("ALTER TABLE complaints ADD COLUMN IF NOT EXISTS cancellation_reason TEXT")
            cur.execute("ALTER TABLE complaints ADD COLUMN IF NOT EXISTS parent_complaint_id BIGINT REFERENCES complaints(id) ON DELETE SET NULL")
            cur.execute("ALTER TABLE complaints ADD COLUMN IF NOT EXISTS previous_context JSONB")
            cur.execute("ALTER TABLE complaints ADD COLUMN IF NOT EXISTS analysis_state TEXT NOT NULL DEFAULT 'completed'")
            cur.execute("ALTER TABLE complaints ADD COLUMN IF NOT EXISTS analysis_revision INTEGER NOT NULL DEFAULT 0")
            cur.execute("UPDATE complaints SET analysis_state='pending' WHERE analysis_state='processing' AND deleted_at IS NULL AND complaint_status='접수'")
        conn.commit()
    for row in fetch_all("SELECT id,analysis_revision FROM complaints WHERE analysis_state='pending' AND deleted_at IS NULL AND complaint_status='접수'"):
        task = asyncio.create_task(classify_submission(row['id'], row['analysis_revision']))
        classification_tasks.add(task)
        task.add_done_callback(classification_tasks.discard)

@app.on_event("shutdown")
async def stop_classification() -> None:
    tasks = list(classification_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

@app.get("/health")
def health():
    return {"status": "ok", "service": "complaintai-fastapi"}

@app.get("/health/database")
def health_database():
    return {"status": "ok", "database": "connected", "pgvector": bool(fetch_one("SELECT 1 FROM pg_extension WHERE extname='vector'"))}