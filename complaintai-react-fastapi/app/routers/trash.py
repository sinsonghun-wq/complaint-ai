from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..db import fetch_all, fetch_one
from ..security import actor_from_auth
from ..dependencies import require_admin
from ..services.trash import TRASH_CAPACITY, TRASH_BUFFER, can_manage_trash, restore_record, permanent_ids, permanent_department
from ..models import PasswordBody

router = APIRouter(prefix="/api/trash", tags=["삭제된 데이터"])


class PermanentSelectionBody(PasswordBody):
    ids: list[Annotated[int, Field(strict=True, gt=0)]] = Field(min_length=1, max_length=1000)


@router.get("/counts")
def counts(actor: Annotated[dict, Depends(actor_from_auth)]):
    require_admin(actor)
    departments = fetch_all("""SELECT d.id,d.name,count(c.id)::int AS count FROM departments d
        LEFT JOIN complaints c ON c.department_id=d.id AND c.deleted_at IS NOT NULL GROUP BY d.id,d.name ORDER BY d.id""")
    totals = fetch_one("SELECT count(*)::int AS total,count(*) FILTER(WHERE department_id IS NULL)::int AS unassigned FROM complaints WHERE deleted_at IS NOT NULL")
    return {**totals, "departments": departments, "policy": {"capacity": TRASH_CAPACITY, "buffer": TRASH_BUFFER,
        "order": "newest_archived_first", "large_batch": "incoming_plus_100"}}


@router.get("/items")
def items(actor: Annotated[dict, Depends(actor_from_auth)], department_id: int | None = Query(None, ge=1, le=7),
          unassigned: bool = False, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    require_admin(actor)
    if unassigned and department_id is not None:
        raise HTTPException(400, "미분류와 부서를 동시에 선택할 수 없습니다.")
    where, params = "c.deleted_at IS NOT NULL", []
    if department_id is not None:
        where += " AND c.department_id=%s"
        params.append(department_id)
    if unassigned:
        where += " AND c.department_id IS NULL"
    rows = fetch_all(f"""SELECT c.id,c.title,c.department_id,d.name AS department_name,c.deleted_at,c.created_at,
        COALESCE(c.cancelled_by_role,'unknown') AS deleted_by_role
        FROM complaints c LEFT JOIN departments d ON d.id=c.department_id
        WHERE {where} ORDER BY c.deleted_at DESC,c.id DESC LIMIT %s OFFSET %s""", [*params,limit,offset])
    for row in rows:
        row["can_manage"] = can_manage_trash(actor, row)
    total = fetch_one(f"SELECT count(*)::int AS total FROM complaints c WHERE {where}", params)
    return {"items": rows, "total": total["total"]}


@router.get("/{complaint_id}")
def detail(complaint_id: int, actor: Annotated[dict, Depends(actor_from_auth)]):
    require_admin(actor)
    row = fetch_one("""SELECT c.id,c.title,c.content,c.summary,c.department_id,d.name AS department_name,
        c.deleted_at,c.created_at,COALESCE(c.cancelled_by_role,'unknown') AS deleted_by_role,
        c.cancellation_reason,c.source_file FROM complaints c LEFT JOIN departments d ON d.id=c.department_id
        WHERE c.id=%s AND c.deleted_at IS NOT NULL""", (complaint_id,))
    if not row:
        raise HTTPException(404, "삭제 보관함에서 해당 민원을 찾을 수 없습니다.")
    return {"complaint": row}


@router.post("/{complaint_id}/restore")
def restore(complaint_id: int, actor: Annotated[dict, Depends(actor_from_auth)]):
    return restore_record(complaint_id, actor)


@router.post("/permanent")
def purge(body: PermanentSelectionBody, actor: Annotated[dict, Depends(actor_from_auth)]):
    return permanent_ids(body.ids, body.password, actor)


@router.post("/department/{department_id}/permanent")
def purge_department(department_id: int, body: PasswordBody, actor: Annotated[dict, Depends(actor_from_auth)]):
    return permanent_department(department_id, body.password, actor)
