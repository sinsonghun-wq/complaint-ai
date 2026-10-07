import asyncio
import csv
import json
from typing import Annotated, Any
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Path, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.agent import ChatResponseBody, enqueue_chat_request
from app.services.parser import csv_encoding, csv_preview, csv_rows, csv_schema_signature, default_csv_mapping, hwp_records, pdf_records, row_to_complaint, spreadsheet_records, start_import_worker, validate_csv_mapping
from app.settings import CSV_MAX_UPLOAD_BYTES, DOCUMENT_MAX_UPLOAD_BYTES
from app.utils.util import hide_cancelled_content, locked_complaint, require_admin, require_open_complaint, save_upload, scoped_where

from ..db import connection, fetch_all, fetch_one
from ..security import actor_from_auth, uuid4
from ..models import CancelBody, ComplaintBody, BatchBody, CsvMappingBody, PasswordBody, ResponseBody, SignupBody, StatusBody, TransferBody, UpdateComplaintBody
from ..dependencies import allowed, check_password, require_department, require_user, vector_literal
from ..ai import CATEGORIES, analyze, embedding, fallback, fingerprint, infer_csv_mapping
from ..services.complaint import classify_submission, save_records
from ..config import MAX_BATCH_SIZE, STATIC_DIR, STATUSES
from ..logger import logger

router = APIRouter(prefix="/api", tags=["complaints"])

@router.post('/complaints', status_code=201)
def submit_own_complaint(body: ComplaintBody, background_tasks: BackgroundTasks, actor: Annotated[dict, Depends(actor_from_auth)]):
    require_user(actor)
    content = body.content.strip()
    if not content:
        raise HTTPException(400, '민원 내용을 입력해 주세요.')
    with connection() as conn:
        content_hash = fingerprint(content)
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (str(actor['owner_id']) + content_hash,))
        existing = conn.execute("""SELECT id,title,content,category,complaint_status,analysis_state,analysis_revision FROM complaints
            WHERE owner_user_id=%s AND content_fingerprint=%s AND deleted_at IS NULL AND complaint_status<>'취소' LIMIT 1""",
            (actor['owner_id'], content_hash)).fetchone()
        if existing:
            return {'complaint': existing, 'duplicates': 1, 'message': '동일한 민원이 이미 접수되어 있습니다.'}
        result = conn.execute("""INSERT INTO complaints(title,content,summary,category,owner_user_id,content_fingerprint,analysis_state,analysis_revision)
            VALUES(%s,%s,'',NULL,%s,%s,'pending',1) RETURNING id,title,content,category,complaint_status,analysis_state,analysis_revision""",
            (body.title.strip() or '제목 없음', content, actor['owner_id'], content_hash)).fetchone()
    background_tasks.add_task(classify_submission, result['id'], result['analysis_revision'])
    return {'complaint': result, 'message': '민원이 접수되었습니다.'}

def owner_id(actor: dict[str, Any]) -> str | None:
    return None if actor["role"] == "admin" else actor["owner_id"]

@router.post("/analyze")
async def analyze_endpoint(body: ComplaintBody, actor: Annotated[dict, Depends(actor_from_auth)]):
    require_user(actor)
    record = await analyze(body.title, body.content)
    similar = []
    vector = await embedding(record)
    if vector:
        similar = fetch_all("SELECT id,title,summary,category,1-(embedding <=> %s::vector) AS similarity FROM complaints WHERE deleted_at IS NULL AND complaint_status<>'취소' AND owner_user_id=%s AND embedding IS NOT NULL ORDER BY embedding <=> %s::vector LIMIT 5", (vector_literal(vector), actor["owner_id"], vector_literal(vector)))
    return {"analysis": record, "similar": similar, "ai": {"llm_model": "qwen2.5:7b-instruct", "embedding_model": "Qwen/Qwen3-Embedding-4B", "vector_dimension": 1536}}


@router.post("/complaints/batch", status_code=201)
async def batch(body: BatchBody, actor: Annotated[dict, Depends(actor_from_auth)]):
    if not body.complaints: raise HTTPException(400, "저장할 민원이 없습니다.")
    if actor["role"] == "user" and (len(body.complaints) != 1 or body.complaints[0].get("source_file")):
        raise HTTPException(403, "일반 사용자는 새 민원을 한 건씩 작성할 수 있습니다.")
    saved = duplicates = 0
    for start in range(0, len(body.complaints), MAX_BATCH_SIZE):
        batch_saved, batch_duplicates = await save_records(body.complaints[start:start + MAX_BATCH_SIZE], actor)
        saved += batch_saved
        duplicates += batch_duplicates
    return {"saved": saved, "duplicates": duplicates}


@router.get("/complaints/counts")
def counts(actor: Annotated[dict, Depends(actor_from_auth)]):
    if actor["role"] == "admin":
        args, clause = (), "TRUE"
    else:
        args, clause = (actor["owner_id"],), "owner_user_id=%s AND NOT (complaint_status='취소' AND cancelled_by_role IS NOT DISTINCT FROM 'user')"
    rows = fetch_all(f"SELECT category,COUNT(*)::int count FROM complaints WHERE deleted_at IS NULL AND category IS NOT NULL AND {clause} GROUP BY category", args)
    deleted = fetch_one(f"SELECT COUNT(*)::int count FROM complaints WHERE deleted_at IS NOT NULL AND {clause}", args)
    logger.info({"categories": rows, "deleted": deleted["count"]})
    return {"categories": rows, "deleted": deleted["count"]}


@router.get("/complaints")
def complaints(deleted: bool = False, category: str | None = None, limit: int = 100, offset: int = 0, source: str = "all", actor: dict = Depends(actor_from_auth)):
    limit = max(1, min(limit, 100)); offset = max(offset, 0)
    category = category or None
    if actor["role"] == "admin": clause, scope = "TRUE", []
    else: clause, scope = "owner_user_id=%s", [actor["owner_id"]]
    where = "deleted_at IS NOT NULL" if deleted else "deleted_at IS NULL"
    if source not in ("all", "user", "file"):
        raise HTTPException(400, "민원 출처는 all, user, file 중 하나여야 합니다.")
    # File provenance is independent of ownership, including legacy imports.
    if source == "user":
        where += " AND NULLIF(BTRIM(source_file), '') IS NULL"
    elif source == "file":
        where += " AND NULLIF(BTRIM(source_file), '') IS NOT NULL"
    if actor["role"] == "user":
        where += " AND NOT (complaint_status='취소' AND cancelled_by_role IS NOT DISTINCT FROM 'user')"
    params: list[Any] = [*scope, category, category, limit, offset]
    response_filter = "" if actor["role"] == "admin" else " AND r.response_state='sent'"
    sql = f"""SELECT id,title,content,summary,category,analysis_state,complaint_status,source_file,source_row,processing_mode,llm_model,embedding_model,created_at,deleted_at,cancelled_at,cancelled_by_role,cancellation_reason,parent_complaint_id,previous_context,
        (NULLIF(BTRIM(source_file), '') IS NULL) AS submitted_by_user,
        COALESCE((SELECT content FROM complaint_responses r WHERE r.complaint_id=complaints.id{response_filter} ORDER BY r.created_at DESC LIMIT 1), '') AS latest_response,
        COALESCE((SELECT response_state FROM complaint_responses r WHERE r.complaint_id=complaints.id{response_filter} ORDER BY r.created_at DESC LIMIT 1), '') AS latest_response_state
        FROM complaints WHERE {where} AND {clause} AND (%s::text IS NULL OR category=%s)
        ORDER BY {'deleted_at' if deleted else 'created_at'} DESC LIMIT %s OFFSET %s"""
    rows = fetch_all(sql, params)
    if actor["role"] == "admin":
        manageable = set(allowed(actor))
        for row in rows:
            row["can_manage"] = row["category"] in manageable
            hide_cancelled_content(row)
    total = fetch_one(f"SELECT COUNT(*)::int count FROM complaints WHERE {where} AND {clause} AND (%s::text IS NULL OR category=%s)", [*scope, category, category])
    return {"complaints": rows, "total": total["count"]}


@router.get("/complaints/{complaint_id}")
def complaint_detail(complaint_id: int, actor: Annotated[dict, Depends(actor_from_auth)]):
    # 목록과 같은 접근 범위; 관리자는 다른 부서 원문을 읽을 수 있다.
    clause, args = ("", []) if actor["role"] == "admin" else (" AND owner_user_id=%s", [actor["owner_id"]])
    response_filter = "" if actor["role"] == "admin" else " AND r.response_state='sent'"
    row = fetch_one(f"""SELECT id,title,content,summary,category,analysis_state,complaint_status,created_at,deleted_at,cancelled_by_role,cancellation_reason,parent_complaint_id,previous_context,
        (NULLIF(BTRIM(source_file), '') IS NULL) submitted_by_user,
        COALESCE((SELECT content FROM complaint_responses r WHERE r.complaint_id=complaints.id{response_filter} ORDER BY r.created_at DESC LIMIT 1),'') latest_response,
        COALESCE((SELECT response_state FROM complaint_responses r WHERE r.complaint_id=complaints.id{response_filter} ORDER BY r.created_at DESC LIMIT 1),'') latest_response_state
        FROM complaints WHERE id=%s{clause}""", (complaint_id, *args))
    if not row:
        raise HTTPException(404, "민원을 찾을 수 없습니다.")
    row["can_manage"] = actor["role"] == "admin" and row["category"] in allowed(actor)
    if actor["role"] == "admin" or row.get("cancelled_by_role") == "user":
        hide_cancelled_content(row)
    return {"complaint": row}


@router.post("/department/complaints/{complaint_id}/start")
def start_complaint(complaint_id: int, actor: Annotated[dict, Depends(actor_from_auth)]):
    require_department(actor)
    with connection() as conn:
        with conn.cursor() as cur:
            row = locked_complaint(cur, complaint_id, actor)
            if row["complaint_status"] == "취소":
                raise HTTPException(409, "해당 민원은 삭제(취소)되었습니다.")
            if row["complaint_status"] == "접수":
                cur.execute("UPDATE complaints SET complaint_status='진행중',status_updated_at=NOW() WHERE id=%s", (complaint_id,))
    return complaint_detail(complaint_id, actor)


@router.patch("/complaints/{complaint_id}")
async def update_own_complaint(complaint_id: int, body: UpdateComplaintBody, background_tasks: BackgroundTasks, actor: Annotated[dict, Depends(actor_from_auth)]):
    require_user(actor)
    title, content = body.title.strip(), body.content.strip()
    if not content:
        raise HTTPException(400, "민원 내용을 입력해 주세요.")
    original = fetch_one("SELECT complaint_status FROM complaints WHERE id=%s AND owner_user_id=%s AND deleted_at IS NULL", (complaint_id, actor["owner_id"]))
    if not original:
        raise HTTPException(404, "본인 민원을 찾을 수 없습니다.")
    if original["complaint_status"] != "접수":
        raise HTTPException(409, "접수 대기 상태의 민원만 수정할 수 있습니다.")
    with connection() as conn:
        with conn.cursor() as cur:
            original = locked_complaint(cur, complaint_id, actor)
            if original["complaint_status"] != "접수":
                raise HTTPException(409, "접수 대기 상태의 민원만 수정할 수 있습니다.")
            cur.execute("DELETE FROM complaint_responses WHERE complaint_id=%s AND response_state='draft'", (complaint_id,))
            cur.execute("""UPDATE complaints SET title=%s,content=%s,summary='',category=NULL,content_fingerprint=%s,
                processing_mode='pending',llm_model=NULL,prompt_version=NULL,analysis_metadata='{}'::jsonb,embedding=NULL,embedding_model=NULL,
                analysis_state='pending',analysis_revision=analysis_revision+1,status_updated_at=NOW()
                WHERE id=%s AND owner_user_id=%s AND deleted_at IS NULL RETURNING id,title,content,category,complaint_status,analysis_state,analysis_revision""",
                (title or '제목 없음', content, fingerprint(content), complaint_id, actor['owner_id']))
            result = cur.fetchone()
        conn.commit()
    if not result:
        raise HTTPException(404, "수정할 본인 민원을 찾지 못했습니다.")
    background_tasks.add_task(classify_submission, result['id'], result['analysis_revision'])
    return {"complaint": result}


@router.delete("/complaints/{complaint_id}")
def soft_delete(complaint_id: int, actor: Annotated[dict, Depends(actor_from_auth)], body: CancelBody | None = None):
    reason = (body.reason if body else "").strip()
    if actor["role"] == "admin" and not reason:
        raise HTTPException(400, "민원을 취소하는 이유를 입력해 주세요.")
    with connection() as conn:
        with conn.cursor() as cur:
            row = locked_complaint(cur, complaint_id, actor)
            require_open_complaint(row)
            cur.execute("UPDATE complaints SET complaint_status='취소',cancelled_at=NOW(),cancelled_by_role=%s,cancellation_reason=%s,status_updated_at=NOW() WHERE id=%s", (actor["role"], reason or "민원인이 해당 민원을 취소했습니다.", complaint_id))
            if actor['role'] == 'user':
                cur.execute("UPDATE complaints SET deleted_at=NOW(),analysis_state='cancelled',analysis_revision=analysis_revision+1 WHERE id=%s", (complaint_id,))
            cur.execute("DELETE FROM complaint_responses WHERE complaint_id=%s AND response_state='draft'", (complaint_id,))
        conn.commit()
    return {"deleted": 1, "complaint_status": "취소", "message": "민원이 취소되었습니다." if actor["role"] == "user" else "민원이 삭제되었습니다."}


@router.post("/complaints/{complaint_id}/follow-up", status_code=201)
async def follow_up(complaint_id: int, body: ComplaintBody, background_tasks: BackgroundTasks, actor: Annotated[dict, Depends(actor_from_auth)]):
    require_user(actor)
    if not body.content.strip():
        raise HTTPException(400, "새 민원 내용을 입력해 주세요.")
    parent = fetch_one("SELECT id FROM complaints WHERE id=%s AND owner_user_id=%s AND complaint_status='완료' AND deleted_at IS NULL", (complaint_id, actor["owner_id"]))
    if not parent:
        raise HTTPException(409, "답변이 완료된 본인 민원에만 재민원을 접수할 수 있습니다.")
    with connection() as conn:
        with conn.cursor() as cur:
            parent = locked_complaint(cur, complaint_id, actor)
            if parent["complaint_status"] != "완료":
                raise HTTPException(409, "답변이 완료된 본인 민원에만 재민원을 접수할 수 있습니다.")
            cur.execute("SELECT content FROM complaint_responses WHERE complaint_id=%s AND response_state='sent' ORDER BY sent_at DESC LIMIT 1", (complaint_id,))
            answer = cur.fetchone()
            if not answer:
                raise HTTPException(409, "전송 완료된 답변이 없습니다.")
            context = {"complaint_id": complaint_id, "title": parent["title"], "content": parent["content"], "response": answer["content"], "previous_context": parent.get("previous_context")}
            cur.execute("""INSERT INTO complaints(title,content,summary,category,owner_user_id,complaint_status,parent_complaint_id,previous_context,content_fingerprint,processing_mode,analysis_state,analysis_revision)
                VALUES(%s,%s,'',NULL,%s,'접수',%s,%s::jsonb,%s,'pending','pending',1) RETURNING id,analysis_revision""",
                (body.title.strip() or '제목 없음', body.content.strip(), actor['owner_id'], complaint_id, json.dumps(context, ensure_ascii=False), fingerprint(body.content)))
            result = cur.fetchone()
    background_tasks.add_task(classify_submission, result['id'], result['analysis_revision'])
    return {"complaint": result, "message": "이전 민원과 답변을 포함한 재민원이 접수되었습니다."}


@router.delete("/complaints/category/{category}")
def delete_category(category: str, body: CancelBody, actor: Annotated[dict, Depends(actor_from_auth)]):
    if category not in CATEGORIES: raise HTTPException(400, "허용되지 않은 카테고리입니다.")
    if category not in require_department(actor):
        raise HTTPException(403, "담당 부서의 민원만 전체 삭제할 수 있습니다.")
    clause, args = scoped_where(actor)
    if not body.reason.strip():
        raise HTTPException(400, "민원을 취소하는 이유를 입력해 주세요.")
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE complaints SET complaint_status='취소',cancelled_at=NOW(),cancelled_by_role='admin',cancellation_reason=%s,status_updated_at=NOW() WHERE category=%s AND deleted_at IS NULL AND complaint_status IN ('접수','진행중'){clause} RETURNING id", (body.reason.strip(), category, *args))
            ids = [row["id"] for row in cur.fetchall()]; moved = len(ids)
            cur.execute("DELETE FROM complaint_responses WHERE complaint_id=ANY(%s) AND response_state='draft'", (ids,))
        conn.commit()
    return {"deleted": moved}


@router.post("/complaints/{complaint_id}/restore")
def restore(complaint_id: int, actor: Annotated[dict, Depends(actor_from_auth)]):
    clause, args = scoped_where(actor)
    with connection() as conn:
        with conn.cursor() as cur: cur.execute(f"UPDATE complaints SET deleted_at=NULL WHERE id=%s AND deleted_at IS NOT NULL AND cancelled_at IS NULL{clause} RETURNING id", (complaint_id, *args)); row = cur.fetchone()
        conn.commit()
    return {"restored": int(bool(row))}


@router.delete("/complaints/{complaint_id}/permanent")
def permanent(complaint_id: int, body: PasswordBody, actor: Annotated[dict, Depends(actor_from_auth)]):
    check_password(actor, body.password); clause, args = scoped_where(actor)
    with connection() as conn:
        with conn.cursor() as cur: cur.execute(f"DELETE FROM complaints WHERE id=%s AND deleted_at IS NOT NULL AND complaint_status NOT IN ('완료','취소'){clause} RETURNING id", (complaint_id, *args)); row = cur.fetchone()
        conn.commit()
    return {"permanently_deleted": int(bool(row))}


@router.delete("/complaints/deleted/all")
def permanent_all(body: PasswordBody, actor: Annotated[dict, Depends(actor_from_auth)]):
    # Temporary cross-department test cleanup; do not grant access to users.
    require_admin(actor)
    check_password(actor, body.password)
    with connection() as conn:
        with conn.cursor() as cur: cur.execute("DELETE FROM complaints WHERE deleted_at IS NOT NULL RETURNING id"); count = len(cur.fetchall())
        conn.commit()
    return {"permanently_deleted": count}


@router.delete("/complaints/department/all")
def permanent_department_all(actor: Annotated[dict, Depends(actor_from_auth)]):
    """Testing-only archive of all active complaints, including terminal states."""
    # This route is deliberately restricted to department administrators.  It is
    # a temporary test-reset tool and must be removed before production release.
    require_department(actor)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE complaints SET complaint_status='취소',cancelled_at=NOW(),cancelled_by_role='admin',cancellation_reason='테스트 데이터 전체 정리',status_updated_at=NOW(),deleted_at=NOW() WHERE deleted_at IS NULL RETURNING id")
            ids = [row["id"] for row in cur.fetchall()]
            count = len(ids)
            cur.execute("DELETE FROM complaint_responses WHERE complaint_id=ANY(%s) AND response_state='draft'", (ids,))
        conn.commit()
    return {"deleted": count, "scope": "all_categories"}


@router.post("/cleanup/deleted")
def cleanup(body: PasswordBody, actor: Annotated[dict, Depends(actor_from_auth)]):
    check_password(actor, body.password); clause, args = scoped_where(actor, 1)
    with connection() as conn:
        with conn.cursor() as cur: cur.execute(f"DELETE FROM complaints WHERE id IN (SELECT id FROM complaints WHERE deleted_at IS NOT NULL AND complaint_status NOT IN ('완료','취소'){clause} ORDER BY deleted_at ASC LIMIT 1000) RETURNING id", args); count = len(cur.fetchall())
        conn.commit()
    return {"permanently_deleted": count}

@router.post("/imports", status_code=202)
async def create_import(file: UploadFile = File(...), actor: dict = Depends(actor_from_auth)):
    require_admin(actor)
    path, suffix = await save_upload(file, CSV_MAX_UPLOAD_BYTES)
    if suffix != ".csv": path.unlink(missing_ok=True); raise HTTPException(400, "일괄 처리는 CSV 파일만 지원합니다.")
    try:
        encoding = csv_encoding(path)
        total = sum(1 for _ in csv_rows(path, encoding))
        headers, samples = csv_preview(path, encoding)
    except (UnicodeError, csv.Error) as error:
        path.unlink(missing_ok=True)
        raise HTTPException(422, "CSV 문자 인코딩 또는 형식을 읽을 수 없습니다. 파일을 CSV UTF-8 형식으로 다시 저장해 업로드해 주세요.") from error
    if not headers:
        path.unlink(missing_ok=True); raise HTTPException(422, "CSV 헤더를 찾지 못했습니다.")
    signature = csv_schema_signature(headers)
    stored_mapping = fetch_one("SELECT profile_name,column_mapping,confidence FROM csv_schema_mappings WHERE schema_signature=%s", (signature,))
    if stored_mapping:
        mapping = {**stored_mapping["column_mapping"], "confidence": float(stored_mapping["confidence"]), "source": "saved", "reason": "저장된 CSV 헤더 매핑을 적용했습니다."}
    else:
        llm_mapping = validate_csv_mapping(await infer_csv_mapping(headers, samples), headers, samples)
        heuristic_mapping = validate_csv_mapping(default_csv_mapping(headers), headers, samples)
        mapping = llm_mapping if llm_mapping["valid"] and llm_mapping["confidence"] >= 0.65 else heuristic_mapping
    mapping = validate_csv_mapping(mapping, headers, samples)
    needs_mapping = not mapping["valid"] or mapping["confidence"] < 0.65
    status = "awaiting_mapping" if needs_mapping else "queued"
    job_id = uuid4(); stored_owner = owner_id(actor)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO source_files(id,original_name,storage_path,mime_type,size_bytes) VALUES(%s,%s,%s,%s,%s)", (job_id, file.filename, str(path), file.content_type, path.stat().st_size))
            cur.execute("INSERT INTO import_jobs(id,source_file,status,total_rows,storage_path,encoding,column_mapping,schema_signature,owner_user_id) VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)", (job_id, file.filename, status, total, str(path), encoding, json.dumps(mapping, ensure_ascii=False), signature, stored_owner))
            if not needs_mapping and not stored_mapping:
                cur.execute("INSERT INTO csv_schema_mappings(schema_signature,column_mapping,confidence,created_by_user_id) VALUES(%s,%s::jsonb,%s,%s) ON CONFLICT(schema_signature) DO NOTHING", (signature, json.dumps(mapping, ensure_ascii=False), mapping["confidence"], actor["sub"]))
        conn.commit()
    if not needs_mapping:
        await asyncio.to_thread(start_import_worker, str(job_id))
    return {"job_id": job_id, "status": status, "total_rows": total, "batch_size": MAX_BATCH_SIZE, "needs_mapping": needs_mapping, "headers": headers, "samples": samples, "mapping": mapping}


@router.post("/imports/{job_id}/mapping", status_code=202)
def confirm_import_mapping(job_id: str, body: CsvMappingBody, actor: Annotated[dict, Depends(actor_from_auth)]):
    require_admin(actor)
    job = fetch_one("SELECT id,source_file,status,storage_path,encoding,owner_user_id,schema_signature FROM import_jobs WHERE id=%s", (job_id,))
    if not job or job["status"] != "awaiting_mapping":
        raise HTTPException(404, "헤더 매핑 대기 중인 작업을 찾지 못했습니다.")
    path = Path(job["storage_path"])
    if not path.is_file():
        raise HTTPException(404, "원본 CSV 파일을 찾지 못했습니다.")
    headers, samples = csv_preview(path, job["encoding"])
    mapping = validate_csv_mapping({**body.model_dump(), "confidence": 1.0, "source": "manual", "reason": "관리자가 CSV 열 매핑을 확인했습니다."}, headers, samples)
    if not mapping["valid"]:
        raise HTTPException(422, "본문 열을 하나 이상 선택하고, 예시 행에 충분한 민원 내용이 있는지 확인해 주세요.")
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE import_jobs SET status='queued',column_mapping=%s::jsonb WHERE id=%s", (json.dumps(mapping, ensure_ascii=False), job_id))
            if body.save_mapping:
                cur.execute("""INSERT INTO csv_schema_mappings(schema_signature,profile_name,column_mapping,confidence,created_by_user_id)
                    VALUES(%s,%s,%s::jsonb,%s,%s)
                    ON CONFLICT(schema_signature) DO UPDATE SET profile_name=EXCLUDED.profile_name,column_mapping=EXCLUDED.column_mapping,confidence=EXCLUDED.confidence,created_by_user_id=EXCLUDED.created_by_user_id,updated_at=NOW()""", (job["schema_signature"], body.profile_name.strip() or None, json.dumps(mapping, ensure_ascii=False), mapping["confidence"], actor["sub"]))
        conn.commit()
    start_import_worker(job_id)
    return {"job_id": job_id, "status": "queued", "mapping": mapping}


@router.get("/imports/{job_id}")
def import_status(job_id: str, actor: Annotated[dict, Depends(actor_from_auth)]):
    clause, params = ("", [job_id]) if actor["role"] == "admin" else (" AND owner_user_id=%s", [job_id, actor["owner_id"]])
    row = fetch_one(f"SELECT id,source_file,status,total_rows,completed_rows,checkpoint_rows,saved_rows,skipped_rows,failed_rows,retry_count,last_error,column_mapping,created_at,started_at,heartbeat_at,completed_at FROM import_jobs WHERE id=%s{clause}", params)
    if not row: raise HTTPException(404, "처리 작업을 찾을 수 없습니다.")
    return row


@router.get("/imports")
def recent_imports(actor: Annotated[dict, Depends(actor_from_auth)], limit: int = 10):
    require_admin(actor)
    limit = max(1, min(limit, 30))
    jobs = fetch_all("""SELECT id,source_file,status,total_rows,completed_rows,checkpoint_rows,saved_rows,skipped_rows,failed_rows,retry_count,last_error,created_at,started_at,heartbeat_at,completed_at
        FROM import_jobs ORDER BY created_at DESC LIMIT %s""", (limit,))
    return {"jobs": jobs}


@router.post("/imports/{job_id}/retry", status_code=202)
def retry_import(job_id: str, actor: Annotated[dict, Depends(actor_from_auth)]):
    require_admin(actor)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE import_jobs
                SET status='queued', completed_rows=checkpoint_rows, heartbeat_at=NULL, worker_id=NULL, last_error=NULL, retry_count=retry_count+1
                WHERE id=%s AND status='failed'
                RETURNING id,status,total_rows,completed_rows,checkpoint_rows,saved_rows,skipped_rows,failed_rows,retry_count,last_error""", (job_id,))
            row = cur.fetchone()
        conn.commit()
    if not row:
        raise HTTPException(404, "재처리할 실패 작업을 찾지 못했습니다.")
    start_import_worker(job_id)
    return row


@router.get("/imports/{job_id}/failures")
def import_failures(job_id: str, actor: Annotated[dict, Depends(actor_from_auth)]):
    import_status(job_id, actor)
    return {"failures": fetch_all("SELECT source_row,reason FROM import_failures WHERE job_id=%s ORDER BY source_row LIMIT 100", (job_id,))}


@router.post("/intake")
async def intake(file: UploadFile = File(...), actor: dict = Depends(actor_from_auth)):
    require_admin(actor)
    path, suffix = await save_upload(file, DOCUMENT_MAX_UPLOAD_BYTES)
    try:
        records: list[dict[str, Any]] = []
        if suffix == ".csv":
            records = [record for index, row in enumerate(csv_rows(path, csv_encoding(path)), start=2) if (record := row_to_complaint(row, index))]
        elif suffix in {".xlsx", ".xls"}:
            records = spreadsheet_records(path, suffix)
        elif suffix == ".pdf":
            records = pdf_records(path, file.filename or "문서.pdf")
        elif suffix == ".hwp":
            records = hwp_records(path, file.filename or "문서.hwp")
        elif suffix in {".png", ".jpg", ".jpeg", ".webp"}:
            try:
                from PIL import Image
                import pytesseract
                text = pytesseract.image_to_string(Image.open(path), lang="kor+eng")
            except Exception as error: raise HTTPException(503, f"OCR을 실행하지 못했습니다: {error}")
            records = [{"source_row": 1, **fallback(Path(file.filename or "이미지").stem, text)}]
        else: raise HTTPException(400, "HWP, PDF, 이미지, XLSX, XLS, CSV 파일만 지원합니다.")
        if not records:
            raise HTTPException(422, "민원 제목과 본문을 가진 데이터를 찾지 못했습니다.")
        for record in records:
            record["source_file"] = file.filename or "업로드 문서"
        return {"file_name": file.filename, "processed": len(records), "max_batch_size": MAX_BATCH_SIZE, "complaints": records}
    except (UnicodeError, csv.Error) as error:
        raise HTTPException(422, "파일 문자 인코딩 또는 형식을 읽을 수 없습니다. CSV는 UTF-8 형식으로 다시 저장해 주세요.") from error
    finally: path.unlink(missing_ok=True)


@router.get("/department/context")
def department_context(actor: Annotated[dict, Depends(actor_from_auth)]):
    return {"department": actor.get("department"), "categories": require_department(actor), "statuses": STATUSES}


def department_complaint(complaint_id: int, actor: dict) -> dict:
    row = fetch_one("SELECT id,title,category,complaint_status,deleted_at FROM complaints WHERE id=%s AND category=ANY(%s)", (complaint_id, require_department(actor)))
    if not row: raise HTTPException(404, "소속 부서에서 처리할 수 있는 민원을 찾지 못했습니다.")
    if row["complaint_status"] == "취소" or row['deleted_at']:
        raise HTTPException(409, "해당 민원은 삭제(취소)되었습니다.")
    return row


@router.get("/department/complaints")
def department_complaints(status: str = "", actor: dict = Depends(actor_from_auth)):
    categories = require_department(actor)
    rows = fetch_all("""SELECT id,title,content,summary,category,complaint_status,status_updated_at,created_at,(NULLIF(BTRIM(source_file), '') IS NULL) submitted_by_user,
        COALESCE((SELECT content FROM complaint_responses r WHERE r.complaint_id=complaints.id ORDER BY r.created_at DESC LIMIT 1), '') AS latest_response,
        COALESCE((SELECT response_state FROM complaint_responses r WHERE r.complaint_id=complaints.id ORDER BY r.created_at DESC LIMIT 1), '') AS latest_response_state
        FROM complaints WHERE deleted_at IS NULL AND category=ANY(%s) AND (%s='' OR complaint_status=%s) ORDER BY created_at DESC LIMIT 200""", (categories, status, status))
    return {"complaints": [hide_cancelled_content(row) for row in rows]}


@router.patch("/department/complaints/{complaint_id}/status")
def department_status(complaint_id: int, body: StatusBody, actor: Annotated[dict, Depends(actor_from_auth)]):
    if body.status != "진행중":
        raise HTTPException(409, "민원 접수 시작, 답변 전송 또는 사유를 입력한 취소 기능으로 상태를 변경해 주세요.")
    return start_complaint(complaint_id, actor)


@router.get("/department/complaints/{complaint_id}/responses")
def responses(complaint_id: int, actor: Annotated[dict, Depends(actor_from_auth)]):
    department_complaint(complaint_id, actor)
    return {"responses": fetch_all("SELECT r.id,r.content,r.response_state,r.created_at,r.sent_at,u.display_name author_name FROM complaint_responses r JOIN app_users u ON u.id=r.author_user_id WHERE r.complaint_id=%s ORDER BY r.created_at", (complaint_id,))}


@router.post("/department/complaints/{complaint_id}/responses", status_code=201)
def create_response(complaint_id: int, body: ResponseBody, actor: Annotated[dict, Depends(actor_from_auth)]):
    department_complaint(complaint_id, actor)
    content = body.content.strip()
    if not 2 <= len(content) <= 5000: raise HTTPException(400, "응답은 2~5,000자로 작성해 주세요.")
    with connection() as conn:
        with conn.cursor() as cur:
            row = locked_complaint(cur, complaint_id, actor)
            require_open_complaint(row)
            cur.execute("SELECT 1 FROM complaint_responses WHERE complaint_id=%s AND response_state='sent' LIMIT 1", (complaint_id,))
            if cur.fetchone():
                raise HTTPException(409, "이미 답변 전송이 완료된 민원입니다.")
            # 같은 관리자가 남긴 이전 임시 답변은 교체해 한 민원에 하나의 최신 초안만 유지한다.
            cur.execute("DELETE FROM complaint_responses WHERE complaint_id=%s AND author_user_id=%s AND response_state='draft'", (complaint_id, actor["sub"]))
            cur.execute("INSERT INTO complaint_responses(id,complaint_id,author_user_id,department,content,response_state) VALUES(%s,%s,%s,%s,%s,'draft') RETURNING id,content,response_state,created_at", (uuid4(), complaint_id, actor["sub"], actor.get("department"), content)); result = cur.fetchone()
            cur.execute("UPDATE complaints SET complaint_status='진행중',status_updated_at=NOW() WHERE id=%s", (complaint_id,))
        conn.commit()
    return {"response": result, "complaint_status": "진행중"}


@router.delete("/department/complaints/{complaint_id}/responses/draft")
def delete_draft_response(complaint_id: int, actor: Annotated[dict, Depends(actor_from_auth)]):
    department_complaint(complaint_id, actor)
    with connection() as conn:
        with conn.cursor() as cur:
            row = locked_complaint(cur, complaint_id, actor)
            require_open_complaint(row)
            cur.execute("DELETE FROM complaint_responses WHERE complaint_id=%s AND author_user_id=%s AND response_state='draft' RETURNING id", (complaint_id, actor["sub"]))
            deleted = len(cur.fetchall())
            if deleted:
                cur.execute("UPDATE complaints SET status_updated_at=NOW() WHERE id=%s", (complaint_id,))
        conn.commit()
    return {"deleted": deleted, "complaint_status": row["complaint_status"]}


@router.post("/department/complaints/{complaint_id}/responses/send")
def send_draft_response(complaint_id: int, actor: Annotated[dict, Depends(actor_from_auth)]):
    department_complaint(complaint_id, actor)
    with connection() as conn:
        with conn.cursor() as cur:
            row = locked_complaint(cur, complaint_id, actor)
            require_open_complaint(row)
            cur.execute("SELECT 1 FROM complaint_responses WHERE complaint_id=%s AND response_state='sent'", (complaint_id,))
            if cur.fetchone():
                raise HTTPException(409, "이미 답변이 완료된 민원입니다.")
            cur.execute("""UPDATE complaint_responses SET response_state='sent',sent_at=NOW()
                WHERE id=(SELECT id FROM complaint_responses WHERE complaint_id=%s AND author_user_id=%s AND response_state='draft' ORDER BY created_at DESC LIMIT 1)
                RETURNING id,content,response_state,sent_at""", (complaint_id, actor["sub"]))
            response = cur.fetchone()
            if not response:
                raise HTTPException(400, "전송할 임시 저장 답변이 없습니다.")
            cur.execute("UPDATE complaints SET complaint_status='완료',status_updated_at=NOW() WHERE id=%s", (complaint_id,))
            cur.execute("DELETE FROM complaint_responses WHERE complaint_id=%s AND response_state='draft'", (complaint_id,))
        conn.commit()
    return {"response": response, "complaint_status": "완료"}


@router.post("/department/complaints/{complaint_id}/transfer")
def transfer_complaint(complaint_id: int, body: TransferBody, actor: Annotated[dict, Depends(actor_from_auth)]):
    require_department(actor)
    if body.category not in CATEGORIES:
        raise HTTPException(400, "전달할 부서를 선택해 주세요.")
    department_complaint(complaint_id, actor)
    with connection() as conn:
        with conn.cursor() as cur:
            row = locked_complaint(cur, complaint_id, actor)
            require_open_complaint(row)
            if body.category == row["category"]:
                raise HTTPException(400, "다른 부서를 선택해 주세요.")
            cur.execute("DELETE FROM complaint_responses WHERE complaint_id=%s AND response_state='draft'", (complaint_id,))
            cur.execute("UPDATE complaints SET category=%s,complaint_status='접수',status_updated_at=NOW() WHERE id=%s RETURNING id,category,complaint_status", (body.category, complaint_id))
            result = cur.fetchone()
        conn.commit()
    return {"complaint": result}


@router.get("/my/complaints/{complaint_id}/responses")
def my_responses(complaint_id: int, actor: Annotated[dict, Depends(actor_from_auth)]):
    require_user(actor)
    exists = fetch_one("SELECT id FROM complaints WHERE id=%s AND owner_user_id=%s AND complaint_status<>'취소' AND deleted_at IS NULL", (complaint_id, actor["owner_id"]))
    if not exists:
        raise HTTPException(404, "본인 민원을 찾지 못했습니다.")
    return {"responses": fetch_all("SELECT r.id,r.content,r.created_at,r.sent_at,u.display_name author_name FROM complaint_responses r JOIN app_users u ON u.id=r.author_user_id WHERE r.complaint_id=%s AND r.response_state='sent' ORDER BY r.created_at", (complaint_id,))}


@router.get("/{path:path}")
def react_app(path: str, request: Request):
    if path.startswith("api/"):
        raise HTTPException(404, "요청한 API를 찾을 수 없습니다.")
    if STATIC_DIR.exists():
        candidate = STATIC_DIR / path
        if path and candidate.is_file(): return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")
    raise HTTPException(404, "React 빌드 결과가 없습니다. frontend에서 npm run build를 실행하세요.")

class ChatRequestBody(BaseModel):
    content: str

@router.post("/chat", response_model=ChatResponseBody)
async def chatAI(chatBody: ChatRequestBody, actor: Annotated[dict, Depends(actor_from_auth)]):
    user_id = actor["owner_id"]
    # 큐에 요청을 넣고 워커가 처리 완료하여 Future에 결과를 담을 때까지 비동기 대기
    result = await enqueue_chat_request(user_id, chatBody.content)
    return result