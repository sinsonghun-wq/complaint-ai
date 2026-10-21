"""CSV reference documents: exact content, compatible vectors, durable 100-row commits."""
from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid
from pathlib import Path

import psycopg

from ..ai import embeddingForReference as embeddingForSelect
from ..db import connection, fetch_one
from ..dependencies import vector_literal
from ..embeddings import normalize_embedding
from ..settings import EMBEDDING_TIMEOUT_MS
from .csv_reader import CsvRowError
from .parser import csv_rows

BATCH_SIZE = 100
DEDUP_LOCK_KEY = 831947265
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


def ensure_rag_import_schema():
    migration = Path(__file__).resolve().parents[3] / "database/migrations/20261020_rag_imports.sql"
    with connection() as db:
        db.execute(migration.read_text(encoding="utf-8"))


def reference_mapping(headers):
    by_name = {"".join(str(header).lower().split()).replace("_", ""): header for header in headers}
    def find(names):
        return next((by_name[name] for name in names if name in by_name), "")
    title = find(("title", "제목", "법령명", "법률명", "사례명", "민원제목"))
    body = find(("content", "본문", "내용", "조문내용", "법령내용", "법률내용", "원문", "민원내용"))
    question = find(("question", "질문", "질문내용", "민원내용"))
    answer = find(("answer", "답변", "답변내용", "처리결과"))
    if question and answer:
        return {"title": title, "question": question, "answer": answer}
    if not body:
        raise ValueError("CSV에서 본문 열을 찾지 못했습니다. ‘제목,본문’ 또는 ‘title,content’ 열을 사용해 주세요. 사례는 ‘질문,답변’ 열도 지원합니다.")
    return {"title": title, "body": body}


def reference_record(raw, mapping, source_file, source_row):
    if isinstance(raw, CsvRowError):
        raise ValueError(raw.reason)
    if mapping.get("body"):
        content = raw.get(mapping["body"]) or ""
    else:
        question, answer = raw.get(mapping["question"]) or "", raw.get(mapping["answer"]) or ""
        if not question.strip() or not answer.strip():
            raise ValueError("질문과 답변이 모두 있어야 사례 자료로 저장할 수 있습니다.")
        content = f"질문: {question}\n답변: {answer}"
    if not isinstance(content, str) or not content.strip():
        raise ValueError("본문이 비어 있어 RAG 자료로 저장할 수 없습니다.")
    title = str(raw.get(mapping.get("title"), "") or "").strip() or f"{Path(source_file).stem} {source_row - 1}"
    return {"title": title, "content": content, "source_row": source_row}


def is_duplicate(content):
    return bool(fetch_one("SELECT 1 FROM department_documents WHERE md5(content)=md5(%s) AND content=%s LIMIT 1", (content, content)))


def is_processing(job_id):
    row = fetch_one("SELECT status FROM rag_import_jobs WHERE id=%s", (job_id,))
    return bool(row and row["status"] == "processing")


def heartbeat(job_id):
    with connection() as db:
        db.execute("UPDATE rag_import_jobs SET heartbeat_at=NOW() WHERE id=%s AND status='processing'", (job_id,))


def count_source(job):
    total = 0
    for _ in csv_rows(Path(job["storage_path"]), job["encoding"]):
        total += 1
        if total % BATCH_SIZE == 0:
            if not is_processing(job["id"]):
                return False
            if total % 1000 == 0:
                heartbeat(job["id"])
    with connection() as db:
        changed = db.execute("UPDATE rag_import_jobs SET total_rows=%s,stage='embedding',heartbeat_at=NOW() WHERE id=%s AND status='processing' RETURNING id", (total, job["id"])).fetchone()
    return bool(changed)


async def embed_cancellable(job_id, content):
    task = asyncio.create_task(embeddingForSelect(content))
    started = time.monotonic()
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=0.5)
            if not await asyncio.to_thread(is_processing, job_id):
                return None
            if done:
                return normalize_embedding(task.result())
            if time.monotonic() - started > EMBEDDING_TIMEOUT_MS / 1000 + 5:
                raise ValueError("임베딩 생성 시간이 초과되었습니다.")
            await asyncio.to_thread(heartbeat, job_id)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def commit_batch(job, batch, completed):
    """Cancellation and batch insertion serialize on the SAME job row lock."""
    with connection() as db:
        state = db.execute("SELECT status FROM rag_import_jobs WHERE id=%s FOR UPDATE", (job["id"],)).fetchone()
        if not state or state["status"] != "processing":
            return False
        db.execute("SELECT pg_advisory_xact_lock(%s)", (DEDUP_LOCK_KEY,))
        saved = duplicates = failed = 0
        for item in batch:
            if item.get("error"):
                failed += 1
                db.execute("INSERT INTO rag_import_failures(job_id,source_row,reason) VALUES(%s,%s,%s)", (job["id"], item["source_row"], item["error"][:2000]))
                continue
            if item.get("duplicate") or db.execute("SELECT 1 FROM department_documents WHERE md5(content)=md5(%s) AND content=%s LIMIT 1", (item["content"], item["content"])).fetchone():
                duplicates += 1
                continue
            try:
                # A bad value must not roll back the other records in this batch.
                with db.transaction():
                    db.execute("""INSERT INTO department_documents(id,document_id,department_id,title,original_name,version,storage_path,content,embedding,created_by)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s)""",
                        (uuid.uuid4(), job["document_id"], job["department_id"], item["title"], job["source_file"], item["source_row"] - 1,
                         job["storage_path"], item["content"], vector_literal(item["embedding"]), job["created_by"]))
            except (psycopg.DataError, psycopg.IntegrityError, ValueError) as error:
                failed += 1
                detail = getattr(getattr(error, "diag", None), "message_primary", None) or str(error)
                db.execute("INSERT INTO rag_import_failures(job_id,source_row,reason) VALUES(%s,%s,%s)",
                           (job["id"], item["source_row"], f"자료를 DB에 저장하지 못했습니다. ({detail})"[:2000]))
                continue
            saved += 1
        db.execute("""UPDATE rag_import_jobs SET completed_rows=%s,checkpoint_rows=%s,saved_rows=saved_rows+%s,
            duplicate_rows=duplicate_rows+%s,failed_rows=failed_rows+%s,heartbeat_at=NOW() WHERE id=%s""",
            (completed, completed, saved, duplicates, failed, job["id"]))
    return True


async def process_job(job):
    if not await asyncio.to_thread(count_source, job):
        return
    completed = job["checkpoint_rows"]
    batch, seen = [], {}
    for number, raw in enumerate(csv_rows(Path(job["storage_path"]), job["encoding"]), start=2):
        if number <= job["checkpoint_rows"] + 1:
            continue
        if not await asyncio.to_thread(is_processing, job["id"]):
            return
        try:
            item = reference_record(raw, job["column_mapping"], job["source_file"], number)
            if "\x00" in item["title"] or "\x00" in item["content"]:
                raise ValueError("DB에 저장할 수 없는 NUL 문자가 포함되어 있습니다.")
        except ValueError as error:
            item = {"source_row": number, "error": str(error)}
        else:
            key = item["content"]
            item["duplicate"] = await asyncio.to_thread(is_duplicate, item["content"])
            if key in seen:
                item["embedding"] = seen[key]
            elif not item["duplicate"]:
                try:
                    item["embedding"] = await embed_cancellable(job["id"], item["content"])
                except Exception as error:
                    item = {"source_row": number, "error": f"임베딩을 생성하지 못했습니다. {str(error)[:1500]}"}
                else:
                    if item["embedding"] is None:
                        return
                    seen[key] = item["embedding"]
        batch.append(item)
        completed += 1
        if len(batch) == BATCH_SIZE:
            if not await asyncio.to_thread(commit_batch, job, batch, completed):
                return
            batch.clear(); seen.clear()
    if batch and not await asyncio.to_thread(commit_batch, job, batch, completed):
        return
    with connection() as db:
        db.execute("UPDATE rag_import_jobs SET status='completed',stage='completed',finished_at=NOW(),worker_id=NULL,heartbeat_at=NOW() WHERE id=%s AND status='processing'", (job["id"],))


def run_job(job):
    try:
        asyncio.run(process_job(job))
    except Exception as error:
        with connection() as db:
            db.execute("""UPDATE rag_import_jobs SET status='failed',stage='failed',completed_rows=checkpoint_rows,
                last_error=%s,finished_at=NOW(),worker_id=NULL WHERE id=%s AND status='processing'""", (str(error)[:2000], job["id"]))
