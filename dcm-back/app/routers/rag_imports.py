"""Admin-only ingestion into the existing department_documents RAG source."""
import csv
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile

from ..db import connection, fetch_all, fetch_one
from ..dependencies import require_admin
from ..security import actor_from_auth
from ..settings import ACTIVE_EMBEDDING_MODEL, CSV_MAX_UPLOAD_BYTES
from ..utils.util import save_upload
from ..rag_worker_manager import ensure_rag_worker
from ..services.csv_reader import CsvRecordReader
from ..services.parser import csv_encoding
from ..services.rag_imports import BATCH_SIZE, reference_mapping

router = APIRouter(prefix="/api/rag-imports", tags=["RAG 자료 가져오기"])
PUBLIC_COLUMNS = "id,source_file,department_id,status,stage,total_rows,completed_rows,saved_rows,duplicate_rows,failed_rows,last_error,embedding_model,created_at,finished_at"
PUBLIC_COLUMNS += ',deferred_rows,long_processed_rows,active_source_row,active_chunks_total,active_chunks_completed'


def with_resume_info(row):
    if row:
        row['resumable_rows'] = fetch_one("SELECT count(*) AS n FROM department_documents_chunk_state WHERE job_id=%s AND status<>'ready'", (row['id'],))['n']
    return row


def start_worker(job_id):
    try:
        ensure_rag_worker()
    except Exception as error:
        with connection() as db:
            db.execute("UPDATE rag_import_jobs SET status='failed',stage='failed',last_error=%s WHERE id=%s AND status='queued'", (str(error)[:2000], job_id))


@router.post("", status_code=202)
async def create_import(background_tasks: BackgroundTasks, file: UploadFile = File(...), department_id: int | None = Form(None), actor: dict = Depends(actor_from_auth)):
    require_admin(actor)
    if Path(file.filename or "").suffix.lower() != ".csv":
        raise HTTPException(400, "파일 형식이 다릅니다. RAG용 데이터는 CSV 파일만 업로드할 수 있습니다.")
    target = department_id or actor.get("department_id") or 7
    if not fetch_one("SELECT id FROM departments WHERE id=%s", (target,)):
        raise HTTPException(400, "RAG 자료를 저장할 부서를 선택해 주세요.")
    path, _ = await save_upload(file, CSV_MAX_UPLOAD_BYTES)
    try:
        encoding = csv_encoding(path)
        with path.open("r", encoding=encoding, newline="") as source:
            reader = CsvRecordReader(source)
            mapping = reference_mapping(reader.fieldnames)
    except (UnicodeError, ValueError, csv.Error) as error:
        path.unlink(missing_ok=True)
        raise HTTPException(422, f"RAG CSV 파일을 읽을 수 없습니다. {error}") from error
    identifier = uuid.uuid4()
    try:
        with connection() as db:
            row = db.execute(f"""INSERT INTO rag_import_jobs(id,document_id,source_file,storage_path,encoding,column_mapping,department_id,created_by,embedding_model)
                VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s) RETURNING {PUBLIC_COLUMNS}""",
                (identifier, uuid.uuid4(), file.filename, str(path), encoding, json.dumps(mapping, ensure_ascii=False), target, actor["sub"], ACTIVE_EMBEDDING_MODEL)).fetchone()
    except Exception:
        path.unlink(missing_ok=True)
        raise
    background_tasks.add_task(start_worker, identifier)
    return {**row, "batch_size": BATCH_SIZE}


@router.get("/latest")
def latest_import(actor: dict = Depends(actor_from_auth)):
    require_admin(actor)
    return {"job": with_resume_info(fetch_one(f"SELECT {PUBLIC_COLUMNS} FROM rag_import_jobs WHERE created_by=%s ORDER BY created_at DESC LIMIT 1", (actor["sub"],)))}


@router.get("/{job_id}")
def status(job_id: uuid.UUID, actor: dict = Depends(actor_from_auth)):
    require_admin(actor)
    row = fetch_one(f"SELECT {PUBLIC_COLUMNS},created_by FROM rag_import_jobs WHERE id=%s", (job_id,))
    if not row:
        raise HTTPException(404, "RAG 자료 처리 작업을 찾지 못했습니다.")
    if str(row.pop("created_by")) != actor["sub"]:
        raise HTTPException(403, "본인이 시작한 RAG 자료 처리 작업만 조회할 수 있습니다.")
    return {**with_resume_info(row), "batch_size": BATCH_SIZE}


@router.get("/{job_id}/failures")
def failures(job_id: uuid.UUID, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), actor: dict = Depends(actor_from_auth)):
    require_admin(actor)
    job = fetch_one("SELECT created_by,failed_rows FROM rag_import_jobs WHERE id=%s", (job_id,))
    if not job:
        raise HTTPException(404, "RAG 자료 처리 작업을 찾지 못했습니다.")
    if str(job["created_by"]) != actor["sub"]:
        raise HTTPException(403, "본인이 시작한 RAG 자료 처리 작업만 조회할 수 있습니다.")
    rows = fetch_all("SELECT source_row,reason FROM rag_import_failures WHERE job_id=%s ORDER BY source_row,id LIMIT %s OFFSET %s", (job_id, limit, offset))
    return {"failures": rows, "total": job["failed_rows"], "offset": offset}


@router.post("/{job_id}/cancel")
def cancel(job_id: uuid.UUID, actor: dict = Depends(actor_from_auth)):
    require_admin(actor)
    with connection() as db:
        row = db.execute("SELECT status,created_by FROM rag_import_jobs WHERE id=%s FOR UPDATE", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "RAG 자료 처리 작업을 찾지 못했습니다.")
        if str(row["created_by"]) != actor["sub"]:
            raise HTTPException(403, "본인이 시작한 RAG 자료 처리 작업만 취소할 수 있습니다.")
        if row["status"] in ("queued", "processing"):
            db.execute("""UPDATE rag_import_jobs SET status='cancelled',stage='cancelled',completed_rows=checkpoint_rows-deferred_rows+long_processed_rows,
                worker_id=NULL,finished_at=NOW(),last_error=NULL WHERE id=%s""", (job_id,))
            db.execute("UPDATE department_documents_chunk_state SET status='cancelled',updated_at=NOW() WHERE job_id=%s AND status IN ('deferred','processing')", (job_id,))
        result = db.execute(f"SELECT {PUBLIC_COLUMNS} FROM rag_import_jobs WHERE id=%s", (job_id,)).fetchone()
    return {**with_resume_info(result), "message": "해당 작업이 취소되었습니다. 이미 저장된 데이터는 유지됩니다." if result["status"] == "cancelled" else "이미 종료된 RAG 자료 처리 작업입니다."}


@router.post('/{job_id}/resume')
def resume(job_id: uuid.UUID, background_tasks: BackgroundTasks, actor: dict = Depends(actor_from_auth)):
    require_admin(actor)
    with connection() as db:
        job = db.execute('SELECT * FROM rag_import_jobs WHERE id=%s FOR UPDATE', (job_id,)).fetchone()
        if not job: raise HTTPException(404, 'RAG 자료 처리 작업을 찾지 못했습니다.')
        if str(job['created_by']) != actor['sub']: raise HTTPException(403, '본인이 시작한 RAG 자료 처리 작업만 재개할 수 있습니다.')
        if job['status'] in ('queued','processing'): raise HTTPException(409, '이미 실행 중인 작업입니다.')
        states = db.execute("SELECT status FROM department_documents_chunk_state WHERE job_id=%s AND status<>'ready' FOR UPDATE", (job_id,)).fetchall()
        if not states: raise HTTPException(409, '이어서 처리할 긴 원문이 없습니다.')
        failed = sum(row['status'] == 'failed' for row in states)
        removed = db.execute("DELETE FROM rag_import_failures WHERE job_id=%s AND phase='chunk'", (job_id,)).rowcount
        db.execute("UPDATE department_documents_chunk_state SET status='deferred',last_error=NULL WHERE job_id=%s AND status<>'ready'", (job_id,))
        row = db.execute(f'''UPDATE rag_import_jobs SET status='queued',stage=%s,finished_at=NULL,worker_id=NULL,last_error=NULL,
            failed_rows=GREATEST(0,failed_rows-%s),long_processed_rows=GREATEST(0,long_processed_rows-%s),
            completed_rows=checkpoint_rows-deferred_rows+GREATEST(0,long_processed_rows-%s),
            active_source_row=NULL,active_chunks_total=0,active_chunks_completed=0
            WHERE id=%s RETURNING {PUBLIC_COLUMNS}''',
            ('chunking' if job['checkpoint_rows'] >= job['total_rows'] else 'queued', removed, failed, failed, job_id)).fetchone()
    background_tasks.add_task(start_worker, job_id)
    return {**with_resume_info(row), 'message': '긴 원문 처리를 재개합니다. 저장된 조각 임베딩은 이어서 사용합니다.'}
