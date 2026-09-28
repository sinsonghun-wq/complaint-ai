from __future__ import annotations

import asyncio
import csv
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import pandas as pd
import psycopg
from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pypdf import PdfReader

from .ai import CATEGORIES, analyze, embedding, fallback, fingerprint
from .db import connection, fetch_all, fetch_one
from .security import actor_from_auth, current_account, issue_token, password_hash, public_user, uuid4, verify_password
from .settings import CSV_MAX_UPLOAD_BYTES, DOCUMENT_MAX_UPLOAD_BYTES, FILE_STORAGE_DIR, LLM_IMPORT_ENABLED, MAX_BATCH_SIZE, WEB_ORIGINS

app = FastAPI(title="ComplaintAI FastAPI", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=WEB_ORIGINS if WEB_ORIGINS != ["*"] else ["*"], allow_credentials=WEB_ORIGINS != ["*"], allow_methods=["*"], allow_headers=["*"])

DEPARTMENT_CATEGORIES = {category: [category] for category in CATEGORIES}
STATUSES = ["접수", "진행중", "완료", "취소"]
STATIC_DIR = Path(__file__).resolve().parents[1] / "frontend" / "dist"
FILE_STORAGE_DIR.mkdir(parents=True, exist_ok=True)


class LoginBody(BaseModel):
    username: str
    password: str


class SignupBody(LoginBody):
    display_name: str = ""


class ComplaintBody(BaseModel):
    title: str = ""
    content: str = ""


class BatchBody(BaseModel):
    complaints: list[dict[str, Any]] = Field(default_factory=list)


class PasswordBody(BaseModel):
    password: str


class StatusBody(BaseModel):
    status: str


class ResponseBody(BaseModel):
    content: str


class UpdateComplaintBody(BaseModel):
    title: str = ""
    content: str = ""


class TransferBody(BaseModel):
    category: str


def owner_id(actor: dict[str, Any]) -> str | None:
    return None if actor["role"] == "admin" else actor["owner_id"]


def allowed(actor: dict[str, Any]) -> list[str]:
    return DEPARTMENT_CATEGORIES.get(actor.get("department"), []) if actor["role"] == "admin" else []


def require_department(actor: dict[str, Any]) -> list[str]:
    categories = allowed(actor)
    if not categories:
        raise HTTPException(403, "부서가 지정된 관리자만 사용할 수 있습니다.")
    return categories


def require_user(actor: dict[str, Any]) -> None:
    if actor["role"] != "user":
        raise HTTPException(403, "일반 사용자 민원 작성 기능입니다.")


def require_admin(actor: dict[str, Any]) -> None:
    if actor["role"] != "admin":
        raise HTTPException(403, "부서 관리자 전용 기능입니다.")


def check_password(actor: dict[str, Any], password: str) -> None:
    user = current_account(actor)
    if not verify_password(password, user["password_salt"], user["password_hash"]):
        raise HTTPException(403, "비밀번호가 올바르지 않습니다.")


def vector_literal(vector: list[float] | None) -> str | None:
    return f"[{','.join(str(value) for value in vector)}]" if vector else None


def row_to_complaint(row: dict[str, Any], source_row: int) -> dict[str, Any] | None:
    headers = list(row)
    if "해결명(SOLUTION_CRTR_NAME)" in headers and "분쟁유형명(DISPUTE_TYPE_NAME)" in headers:
        item, dispute, solution = str(row.get("품목명(ITEM_NAME)", "")).strip(), str(row.get("분쟁유형명(DISPUTE_TYPE_NAME)", "")).strip(), str(row.get("해결명(SOLUTION_CRTR_NAME)", "")).strip()
        return {"source_row": source_row, **fallback(" · ".join(filter(None, [item, dispute])), f"{dispute}\n{solution}")} if solution else None
    def column(words: list[str]) -> str | None:
        return next((header for header in headers if any(word.lower() in str(header).lower() for word in words)), None)
    title_key, content_key = column(["민원 제목", "제목", "title", "subject"]), column(["민원 내용", "민원내용", "원문", "내용", "complaint", "content", "질문"])
    content = str(row.get(content_key, "")).strip() if content_key else ""
    return {"source_row": source_row, **fallback(str(row.get(title_key, "")) if title_key else "", content)} if content else None


def insert_complaint(conn: psycopg.Connection, record: dict[str, Any], source_file: str | None, source_row: int | None, stored_owner: str | None, vector: list[float] | None = None) -> bool:
    metadata = json.dumps({key: record.get(key) for key in ["key_points", "urgency", "needs_review", "review_reason", "reason", "keywords"]}, ensure_ascii=False)
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO complaints (title,content,summary,category,source_file,source_row,content_fingerprint,processing_mode,llm_model,embedding_model,prompt_version,analysis_metadata,embedding,owner_user_id)
            SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s
            WHERE NOT EXISTS (SELECT 1 FROM complaints WHERE content_fingerprint=%s AND deleted_at IS NULL AND owner_user_id IS NOT DISTINCT FROM %s) RETURNING id""",
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


@app.on_event("startup")
def startup() -> None:
    # 이전 설치 DB도 답변 임시 저장 상태를 바로 사용할 수 있도록 호환 컬럼을 보완한다.
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.execute("ALTER TABLE complaint_responses ADD COLUMN IF NOT EXISTS response_state TEXT NOT NULL DEFAULT 'sent'")
            cur.execute("ALTER TABLE complaint_responses ADD COLUMN IF NOT EXISTS sent_at TIMESTAMPTZ")
            cur.execute("UPDATE complaint_responses SET sent_at=created_at WHERE response_state='sent' AND sent_at IS NULL")
        conn.commit()


@app.get("/health")
def health(): return {"status": "ok", "service": "complaintai-fastapi"}


@app.get("/health/database")
def health_database():
    return {"status": "ok", "database": "connected", "pgvector": bool(fetch_one("SELECT 1 FROM pg_extension WHERE extname='vector'"))}


@app.post("/api/auth/login")
def login(body: LoginBody):
    user = fetch_one("SELECT id,owner_id,username,display_name,account_role,department,password_salt,password_hash FROM app_users WHERE username=%s", (body.username.strip(),))
    if not user or not verify_password(body.password, user["password_salt"], user["password_hash"]):
        raise HTTPException(401, "계정 또는 비밀번호가 올바르지 않습니다.")
    return {"token": issue_token(user), "user": public_user(user)}


@app.post("/api/auth/signup", status_code=201)
def signup(body: SignupBody):
    username = body.username.strip()
    if not username.replace("_", "").replace("-", "").isalnum() or not 3 <= len(username) <= 30 or len(body.password) < 8:
        raise HTTPException(400, "계정은 3~30자, 비밀번호는 8자 이상이어야 합니다.")
    salt, digest = password_hash(body.password)
    user = {"id": uuid4(), "owner_id": uuid4(), "username": username, "display_name": body.display_name.strip() or username, "account_role": "user", "department": None}
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO app_users(id,owner_id,username,email,display_name,password_salt,password_hash,account_role) VALUES(%s,%s,%s,%s,%s,%s,%s,'user')", (user["id"], user["owner_id"], username, f"{username}@complaintai.local", user["display_name"], salt, digest))
            conn.commit()
    except psycopg.errors.UniqueViolation:
        raise HTTPException(400, "이미 사용 중인 계정입니다.")
    return {"token": issue_token(user), "user": public_user(user)}


@app.get("/api/auth/context")
def auth_context(actor: Annotated[dict, Depends(actor_from_auth)]): return {"user": public_user(current_account(actor))}


@app.post("/api/auth/verify-password")
def verify(body: PasswordBody, actor: Annotated[dict, Depends(actor_from_auth)]): check_password(actor, body.password); return {"verified": True}


@app.post("/api/analyze")
async def analyze_endpoint(body: ComplaintBody, actor: Annotated[dict, Depends(actor_from_auth)]):
    require_user(actor)
    record = await analyze(body.title, body.content)
    similar = []
    vector = await embedding(record)
    if vector:
        similar = fetch_all("SELECT id,title,summary,category,1-(embedding <=> %s::vector) AS similarity FROM complaints WHERE deleted_at IS NULL AND embedding IS NOT NULL ORDER BY embedding <=> %s::vector LIMIT 5", (vector_literal(vector), vector_literal(vector)))
    return {"analysis": record, "similar": similar, "ai": {"llm_model": "qwen2.5:7b-instruct", "embedding_model": "Qwen/Qwen3-Embedding-4B", "vector_dimension": 1536}}


@app.post("/api/complaints/batch", status_code=201)
async def batch(body: BatchBody, actor: Annotated[dict, Depends(actor_from_auth)]):
    if not body.complaints: raise HTTPException(400, "저장할 민원이 없습니다.")
    if actor["role"] == "user" and (len(body.complaints) != 1 or body.complaints[0].get("source_file")):
        raise HTTPException(403, "일반 사용자는 새 민원을 한 건씩 작성할 수 있습니다.")
    saved, duplicates = await save_records(body.complaints[:MAX_BATCH_SIZE], actor)
    return {"saved": saved, "duplicates": duplicates}


@app.get("/api/complaints/counts")
def counts(actor: Annotated[dict, Depends(actor_from_auth)]):
    if actor["role"] == "admin":
        categories, args = allowed(actor), (allowed(actor),)
        clause = "category = ANY(%s)"
    else:
        args, clause = (actor["owner_id"],), "owner_user_id=%s"
    rows = fetch_all(f"SELECT COALESCE(category,'기타') category,COUNT(*)::int count FROM complaints WHERE deleted_at IS NULL AND {clause} GROUP BY category", args)
    deleted = fetch_one(f"SELECT COUNT(*)::int count FROM complaints WHERE deleted_at IS NOT NULL AND {clause}", args)
    return {"categories": rows, "deleted": deleted["count"]}


@app.get("/api/complaints")
def complaints(deleted: bool = False, category: str | None = None, limit: int = 100, offset: int = 0, actor: dict = Depends(actor_from_auth)):
    limit = max(1, min(limit, 100)); offset = max(offset, 0)
    if actor["role"] == "admin": clause, scope = "category=ANY(%s)", [allowed(actor)]
    else: clause, scope = "owner_user_id=%s", [actor["owner_id"]]
    where = "deleted_at IS NOT NULL" if deleted else "deleted_at IS NULL"
    params: list[Any] = [*scope, category, category, limit, offset]
    response_filter = "" if actor["role"] == "admin" else " AND r.response_state='sent'"
    sql = f"""SELECT id,title,content,summary,category,complaint_status,source_file,source_row,processing_mode,llm_model,embedding_model,created_at,deleted_at,
        (owner_user_id IS NOT NULL) AS submitted_by_user,
        COALESCE((SELECT content FROM complaint_responses r WHERE r.complaint_id=complaints.id{response_filter} ORDER BY r.created_at DESC LIMIT 1), '') AS latest_response,
        COALESCE((SELECT response_state FROM complaint_responses r WHERE r.complaint_id=complaints.id{response_filter} ORDER BY r.created_at DESC LIMIT 1), '') AS latest_response_state
        FROM complaints WHERE {where} AND {clause} AND (%s::text IS NULL OR category=%s)
        ORDER BY {'deleted_at' if deleted else 'created_at'} DESC LIMIT %s OFFSET %s"""
    rows = fetch_all(sql, params)
    total = fetch_one(f"SELECT COUNT(*)::int count FROM complaints WHERE {where} AND {clause} AND (%s::text IS NULL OR category=%s)", [*scope, category, category])
    return {"complaints": rows, "total": total["count"]}


def scoped_where(actor: dict, parameter: int = 2) -> tuple[str, list[Any]]:
    if actor["role"] == "admin": return " AND category=ANY(%s)", [allowed(actor)]
    return " AND owner_user_id=%s", [actor["owner_id"]]


@app.patch("/api/complaints/{complaint_id}")
async def update_own_complaint(complaint_id: int, body: UpdateComplaintBody, actor: Annotated[dict, Depends(actor_from_auth)]):
    require_user(actor)
    title, content = body.title.strip(), body.content.strip()
    if not content:
        raise HTTPException(400, "민원 내용을 입력해 주세요.")
    record = await analyze(title, content)
    metadata = json.dumps({key: record.get(key) for key in ["key_points", "urgency", "needs_review", "review_reason", "reason", "keywords"]}, ensure_ascii=False)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE complaints SET title=%s,content=%s,summary=%s,category=%s,content_fingerprint=%s,
                processing_mode=%s,llm_model=%s,prompt_version=%s,analysis_metadata=%s::jsonb,complaint_status='접수',status_updated_at=NOW()
                WHERE id=%s AND owner_user_id=%s AND deleted_at IS NULL RETURNING id,title,summary,category,complaint_status""",
                (record["title"], record["content"], record["summary"], record["category"], fingerprint(record["content"]), record.get("processing_mode", "fallback"), record.get("model"), record.get("prompt_version"), metadata, complaint_id, actor["owner_id"]))
            result = cur.fetchone()
        conn.commit()
    if not result:
        raise HTTPException(404, "수정할 본인 민원을 찾지 못했습니다.")
    return {"complaint": result}


@app.delete("/api/complaints/{complaint_id}")
def soft_delete(complaint_id: int, actor: Annotated[dict, Depends(actor_from_auth)]):
    clause, args = scoped_where(actor)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE complaints SET deleted_at=NOW() WHERE id=%s AND deleted_at IS NULL{clause} RETURNING id", (complaint_id, *args)); moved = cur.fetchone()
            cur.execute(f"SELECT COUNT(*)::int count FROM complaints WHERE deleted_at IS NOT NULL{clause}", args); count = cur.fetchone()["count"]
        conn.commit()
    return {"deleted": int(bool(moved)), "cleanup_required": count >= 3000}


@app.delete("/api/complaints/category/{category}")
def delete_category(category: str, actor: Annotated[dict, Depends(actor_from_auth)]):
    if category not in CATEGORIES: raise HTTPException(400, "허용되지 않은 카테고리입니다.")
    clause, args = scoped_where(actor)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE complaints SET deleted_at=NOW() WHERE category=%s AND deleted_at IS NULL{clause} RETURNING id", (category, *args)); moved = len(cur.fetchall())
        conn.commit()
    return {"deleted": moved}


@app.post("/api/complaints/{complaint_id}/restore")
def restore(complaint_id: int, actor: Annotated[dict, Depends(actor_from_auth)]):
    clause, args = scoped_where(actor)
    with connection() as conn:
        with conn.cursor() as cur: cur.execute(f"UPDATE complaints SET deleted_at=NULL WHERE id=%s AND deleted_at IS NOT NULL{clause} RETURNING id", (complaint_id, *args)); row = cur.fetchone()
        conn.commit()
    return {"restored": int(bool(row))}


@app.delete("/api/complaints/{complaint_id}/permanent")
def permanent(complaint_id: int, body: PasswordBody, actor: Annotated[dict, Depends(actor_from_auth)]):
    check_password(actor, body.password); clause, args = scoped_where(actor)
    with connection() as conn:
        with conn.cursor() as cur: cur.execute(f"DELETE FROM complaints WHERE id=%s AND deleted_at IS NOT NULL{clause} RETURNING id", (complaint_id, *args)); row = cur.fetchone()
        conn.commit()
    return {"permanently_deleted": int(bool(row))}


@app.delete("/api/complaints/deleted/all")
def permanent_all(body: PasswordBody, actor: Annotated[dict, Depends(actor_from_auth)]):
    check_password(actor, body.password); clause, args = scoped_where(actor, 1)
    with connection() as conn:
        with conn.cursor() as cur: cur.execute(f"DELETE FROM complaints WHERE deleted_at IS NOT NULL{clause} RETURNING id", args); count = len(cur.fetchall())
        conn.commit()
    return {"permanently_deleted": count}


@app.post("/api/cleanup/deleted")
def cleanup(body: PasswordBody, actor: Annotated[dict, Depends(actor_from_auth)]):
    check_password(actor, body.password); clause, args = scoped_where(actor, 1)
    with connection() as conn:
        with conn.cursor() as cur: cur.execute(f"DELETE FROM complaints WHERE id IN (SELECT id FROM complaints WHERE deleted_at IS NOT NULL{clause} ORDER BY deleted_at ASC LIMIT 1000) RETURNING id", args); count = len(cur.fetchall())
        conn.commit()
    return {"permanently_deleted": count}


async def save_upload(upload: UploadFile, maximum: int) -> tuple[Path, str]:
    suffix = Path(upload.filename or "upload").suffix.lower()
    target = FILE_STORAGE_DIR / f"{uuid.uuid4()}{suffix}"
    total = 0
    with target.open("wb") as output:
        while chunk := await upload.read(1024 * 1024):
            total += len(chunk)
            if total > maximum:
                output.close(); target.unlink(missing_ok=True); raise HTTPException(413, "파일 크기 제한을 초과했습니다.")
            output.write(chunk)
    return target, suffix


def csv_encoding(path: Path) -> str:
    # 한국어 UTF-8 CSV가 통계 기반 감지에서 CP949로 잘못 판정되면 제목·카테고리가 깨진다.
    # 우선 엄격 UTF-8을 확인하고, 실패할 때만 국내 공공데이터의 CP949로 폴백한다.
    sample = path.read_bytes()[:65536]
    try:
        sample.decode("utf-8")
        return "utf-8-sig"
    except UnicodeDecodeError:
        return "cp949"


def csv_rows(path: Path, encoding: str):
    with path.open("r", encoding=encoding, newline="") as source:
        yield from csv.DictReader(source)


def process_csv_job(job_id: str, path: Path, source_file: str, stored_owner: str | None, encoding: str) -> None:
    try:
        with connection() as conn:
            with conn.cursor() as cur: cur.execute("UPDATE import_jobs SET status='processing' WHERE id=%s", (job_id,))
            conn.commit()
        batch: list[dict[str, Any]] = []; failures: list[tuple[int, dict[str, Any], str]] = []; completed = saved = skipped = failed = 0
        for number, row in enumerate(csv_rows(path, encoding), start=2):
            completed += 1
            try:
                record = row_to_complaint(row, number)
                if record: batch.append(record)
                else: failures.append((number, row, "요약할 민원 내용 열을 찾지 못했습니다."))
            except Exception as error: failures.append((number, row, str(error)))
            if len(batch) + len(failures) >= MAX_BATCH_SIZE:
                with connection() as conn:
                    for record in batch:
                        vector = asyncio.run(embedding(record))
                        if insert_complaint(conn, record, source_file, record["source_row"], stored_owner, vector): saved += 1
                        else: skipped += 1
                    with conn.cursor() as cur:
                        for row_number, raw, reason in failures: cur.execute("INSERT INTO import_failures(job_id,source_row,raw_data,reason) VALUES(%s,%s,%s,%s)", (job_id, row_number, json.dumps(raw, ensure_ascii=False), reason))
                    failed += len(failures); conn.commit()
                batch = []; failures = []
                with connection() as conn:
                    with conn.cursor() as cur: cur.execute("UPDATE import_jobs SET completed_rows=%s,saved_rows=%s,skipped_rows=%s,failed_rows=%s WHERE id=%s", (completed, saved, skipped, failed, job_id))
                    conn.commit()
        if batch or failures:
            with connection() as conn:
                for record in batch:
                    vector = asyncio.run(embedding(record))
                    if insert_complaint(conn, record, source_file, record["source_row"], stored_owner, vector): saved += 1
                    else: skipped += 1
                with conn.cursor() as cur:
                    for row_number, raw, reason in failures: cur.execute("INSERT INTO import_failures(job_id,source_row,raw_data,reason) VALUES(%s,%s,%s,%s)", (job_id, row_number, json.dumps(raw, ensure_ascii=False), reason))
                failed += len(failures)
                with conn.cursor() as cur: cur.execute("UPDATE import_jobs SET status='completed',completed_rows=%s,saved_rows=%s,skipped_rows=%s,failed_rows=%s,completed_at=NOW(),last_error=NULL WHERE id=%s", (completed, saved, skipped, failed, job_id))
                conn.commit()
    except Exception as error:
        with connection() as conn:
            with conn.cursor() as cur: cur.execute("UPDATE import_jobs SET status='queued',retry_count=retry_count+1,last_error=%s WHERE id=%s", (str(error), job_id))
            conn.commit()


@app.post("/api/imports", status_code=202)
async def create_import(background: BackgroundTasks, file: UploadFile = File(...), actor: dict = Depends(actor_from_auth)):
    require_admin(actor)
    path, suffix = await save_upload(file, CSV_MAX_UPLOAD_BYTES)
    if suffix != ".csv": path.unlink(missing_ok=True); raise HTTPException(400, "일괄 처리는 CSV 파일만 지원합니다.")
    encoding = csv_encoding(path)
    total = sum(1 for _ in csv_rows(path, encoding))
    job_id = uuid4(); stored_owner = owner_id(actor)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO source_files(id,original_name,storage_path,mime_type,size_bytes) VALUES(%s,%s,%s,%s,%s)", (job_id, file.filename, str(path), file.content_type, path.stat().st_size))
            cur.execute("INSERT INTO import_jobs(id,source_file,status,total_rows,storage_path,encoding,owner_user_id) VALUES(%s,%s,'queued',%s,%s,%s,%s)", (job_id, file.filename, total, str(path), encoding, stored_owner))
        conn.commit()
    background.add_task(process_csv_job, job_id, path, file.filename or "upload.csv", stored_owner, encoding)
    return {"job_id": job_id, "status": "queued", "total_rows": total, "batch_size": MAX_BATCH_SIZE}


@app.get("/api/imports/{job_id}")
def import_status(job_id: str, actor: Annotated[dict, Depends(actor_from_auth)]):
    clause, params = ("", [job_id]) if actor["role"] == "admin" else (" AND owner_user_id=%s", [job_id, actor["owner_id"]])
    row = fetch_one(f"SELECT id,source_file,status,total_rows,completed_rows,saved_rows,skipped_rows,failed_rows,retry_count,last_error,created_at,completed_at FROM import_jobs WHERE id=%s{clause}", params)
    if not row: raise HTTPException(404, "처리 작업을 찾을 수 없습니다.")
    return row


@app.get("/api/imports/{job_id}/failures")
def import_failures(job_id: str, actor: Annotated[dict, Depends(actor_from_auth)]):
    import_status(job_id, actor)
    return {"failures": fetch_all("SELECT source_row,reason FROM import_failures WHERE job_id=%s ORDER BY source_row LIMIT 100", (job_id,))}


@app.post("/api/intake")
async def intake(file: UploadFile = File(...), actor: dict = Depends(actor_from_auth)):
    require_admin(actor)
    path, suffix = await save_upload(file, DOCUMENT_MAX_UPLOAD_BYTES)
    try:
        records: list[dict[str, Any]] = []
        if suffix == ".csv":
            rows = list(csv_rows(path, csv_encoding(path)))[:MAX_BATCH_SIZE]
            records = [record for index, row in enumerate(rows, start=2) if (record := row_to_complaint(row, index))]
        elif suffix in {".xlsx", ".xls"}:
            frame = pd.read_excel(path, nrows=MAX_BATCH_SIZE)
            records = [record for index, row in enumerate(frame.fillna("").to_dict("records"), start=2) if (record := row_to_complaint(row, index))]
        elif suffix == ".pdf":
            text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
            records = [{"source_row": 1, **fallback(Path(file.filename or "문서").stem, text)}]
        elif suffix in {".png", ".jpg", ".jpeg", ".webp"}:
            try:
                from PIL import Image
                import pytesseract
                text = pytesseract.image_to_string(Image.open(path), lang="kor+eng")
            except Exception as error: raise HTTPException(503, f"OCR을 실행하지 못했습니다: {error}")
            records = [{"source_row": 1, **fallback(Path(file.filename or "이미지").stem, text)}]
        else: raise HTTPException(400, "PDF, 이미지, XLSX, XLS, CSV 파일만 지원합니다.")
        return {"file_name": file.filename, "processed": len(records), "max_batch_size": MAX_BATCH_SIZE, "complaints": records}
    finally: path.unlink(missing_ok=True)


@app.get("/api/department/context")
def department_context(actor: Annotated[dict, Depends(actor_from_auth)]):
    return {"department": actor.get("department"), "categories": require_department(actor), "statuses": STATUSES}


def department_complaint(complaint_id: int, actor: dict) -> dict:
    row = fetch_one("SELECT id,title,category,complaint_status FROM complaints WHERE id=%s AND deleted_at IS NULL AND category=ANY(%s)", (complaint_id, require_department(actor)))
    if not row: raise HTTPException(404, "소속 부서에서 처리할 수 있는 민원을 찾지 못했습니다.")
    return row


@app.get("/api/department/complaints")
def department_complaints(status: str = "", actor: dict = Depends(actor_from_auth)):
    categories = require_department(actor)
    rows = fetch_all("""SELECT id,title,content,summary,category,complaint_status,status_updated_at,created_at,(owner_user_id IS NOT NULL) submitted_by_user,
        COALESCE((SELECT content FROM complaint_responses r WHERE r.complaint_id=complaints.id ORDER BY r.created_at DESC LIMIT 1), '') AS latest_response,
        COALESCE((SELECT response_state FROM complaint_responses r WHERE r.complaint_id=complaints.id ORDER BY r.created_at DESC LIMIT 1), '') AS latest_response_state
        FROM complaints WHERE deleted_at IS NULL AND category=ANY(%s) AND (%s='' OR complaint_status=%s) ORDER BY created_at DESC LIMIT 200""", (categories, status, status))
    return {"complaints": rows}


@app.patch("/api/department/complaints/{complaint_id}/status")
def department_status(complaint_id: int, body: StatusBody, actor: Annotated[dict, Depends(actor_from_auth)]):
    if body.status not in STATUSES: raise HTTPException(400, "허용되지 않은 민원 상태입니다.")
    department_complaint(complaint_id, actor)
    with connection() as conn:
        with conn.cursor() as cur: cur.execute("UPDATE complaints SET complaint_status=%s,status_updated_at=NOW() WHERE id=%s RETURNING id,complaint_status,status_updated_at", (body.status, complaint_id)); result = cur.fetchone()
        conn.commit()
    return {"complaint": result}


@app.get("/api/department/complaints/{complaint_id}/responses")
def responses(complaint_id: int, actor: Annotated[dict, Depends(actor_from_auth)]):
    department_complaint(complaint_id, actor)
    return {"responses": fetch_all("SELECT r.id,r.content,r.response_state,r.created_at,r.sent_at,u.display_name author_name FROM complaint_responses r JOIN app_users u ON u.id=r.author_user_id WHERE r.complaint_id=%s ORDER BY r.created_at", (complaint_id,))}


@app.post("/api/department/complaints/{complaint_id}/responses", status_code=201)
def create_response(complaint_id: int, body: ResponseBody, actor: Annotated[dict, Depends(actor_from_auth)]):
    department_complaint(complaint_id, actor)
    content = body.content.strip()
    if not 2 <= len(content) <= 5000: raise HTTPException(400, "응답은 2~5,000자로 작성해 주세요.")
    with connection() as conn:
        with conn.cursor() as cur:
            # 같은 관리자가 남긴 이전 임시 답변은 교체해 한 민원에 하나의 최신 초안만 유지한다.
            cur.execute("DELETE FROM complaint_responses WHERE complaint_id=%s AND author_user_id=%s AND response_state='draft'", (complaint_id, actor["sub"]))
            cur.execute("INSERT INTO complaint_responses(id,complaint_id,author_user_id,department,content,response_state) VALUES(%s,%s,%s,%s,%s,'draft') RETURNING id,content,response_state,created_at", (uuid4(), complaint_id, actor["sub"], actor.get("department"), content)); result = cur.fetchone()
            cur.execute("UPDATE complaints SET complaint_status='진행중',status_updated_at=NOW() WHERE id=%s", (complaint_id,))
        conn.commit()
    return {"response": result, "complaint_status": "진행중"}


@app.delete("/api/department/complaints/{complaint_id}/responses/draft")
def delete_draft_response(complaint_id: int, actor: Annotated[dict, Depends(actor_from_auth)]):
    department_complaint(complaint_id, actor)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM complaint_responses WHERE complaint_id=%s AND author_user_id=%s AND response_state='draft' RETURNING id", (complaint_id, actor["sub"]))
            deleted = len(cur.fetchall())
            if deleted:
                cur.execute("UPDATE complaints SET complaint_status='접수',status_updated_at=NOW() WHERE id=%s", (complaint_id,))
        conn.commit()
    return {"deleted": deleted, "complaint_status": "접수" if deleted else None}


@app.post("/api/department/complaints/{complaint_id}/responses/send")
def send_draft_response(complaint_id: int, actor: Annotated[dict, Depends(actor_from_auth)]):
    department_complaint(complaint_id, actor)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE complaint_responses SET response_state='sent',sent_at=NOW()
                WHERE id=(SELECT id FROM complaint_responses WHERE complaint_id=%s AND author_user_id=%s AND response_state='draft' ORDER BY created_at DESC LIMIT 1)
                RETURNING id,content,response_state,sent_at""", (complaint_id, actor["sub"]))
            response = cur.fetchone()
            if not response:
                raise HTTPException(400, "전송할 임시 저장 답변이 없습니다.")
            cur.execute("UPDATE complaints SET complaint_status='완료',status_updated_at=NOW() WHERE id=%s", (complaint_id,))
        conn.commit()
    return {"response": response, "complaint_status": "완료"}


@app.post("/api/department/complaints/{complaint_id}/transfer")
def transfer_complaint(complaint_id: int, body: TransferBody, actor: Annotated[dict, Depends(actor_from_auth)]):
    require_department(actor)
    if body.category not in CATEGORIES:
        raise HTTPException(400, "전달할 부서를 선택해 주세요.")
    department_complaint(complaint_id, actor)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE complaints SET category=%s,complaint_status='접수',status_updated_at=NOW() WHERE id=%s RETURNING id,category,complaint_status", (body.category, complaint_id))
            result = cur.fetchone()
        conn.commit()
    return {"complaint": result}


@app.get("/api/my/complaints/{complaint_id}/responses")
def my_responses(complaint_id: int, actor: Annotated[dict, Depends(actor_from_auth)]):
    require_user(actor)
    exists = fetch_one("SELECT id FROM complaints WHERE id=%s AND owner_user_id=%s", (complaint_id, actor["owner_id"]))
    if not exists:
        raise HTTPException(404, "본인 민원을 찾지 못했습니다.")
    return {"responses": fetch_all("SELECT r.id,r.content,r.created_at,r.sent_at,u.display_name author_name FROM complaint_responses r JOIN app_users u ON u.id=r.author_user_id WHERE r.complaint_id=%s AND r.response_state='sent' ORDER BY r.created_at", (complaint_id,))}


@app.get("/{path:path}")
def react_app(path: str, request: Request):
    if path.startswith("api/"):
        raise HTTPException(404, "요청한 API를 찾을 수 없습니다.")
    if STATIC_DIR.exists():
        candidate = STATIC_DIR / path
        if path and candidate.is_file(): return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")
    raise HTTPException(404, "React 빌드 결과가 없습니다. frontend에서 npm run build를 실행하세요.")

