"""Shared administrator trash, bounded retention, and admin-only restoration."""
from pathlib import Path
import json

from fastapi import HTTPException

from ..db import connection
from ..dependencies import check_password, require_admin, require_department_id
from ..dedup import lock_identities, find_duplicate

TRASH_CAPACITY = 10_000
TRASH_BUFFER = 100
TRASH_LOCK_KEY = 831947262


def ensure_trash_schema():
    migration = Path(__file__).resolve().parents[3] / "database/migrations/20261019_trash_archive.sql"
    with connection() as conn:
        conn.execute(migration.read_text(encoding="utf-8"))


def lock_trash(conn):
    # All archive/restore/purge writers acquire this BEFORE complaint row locks.
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (TRASH_LOCK_KEY,))


def audit(conn, event, actor, **detail):
    conn.execute("INSERT INTO audit_events(event_type,entity_type,detail) VALUES(%s,'complaint',%s::jsonb)",
                 (event, json.dumps({"actor_user_id": str(actor.get("sub", "")), **detail}, ensure_ascii=False)))


def can_manage_trash(actor, row):
    return actor["role"] == "admin" and (row.get("department_id") is None or row["department_id"] == actor.get("department_id"))


def require_manage_trash(actor, row):
    require_department_id(actor)
    if not can_manage_trash(actor, row):
        raise HTTPException(403, "담당 부서의 삭제된 데이터만 복원·영구 삭제할 수 있습니다.")


def purge_recent(conn, count, actor):
    rows = conn.execute("""DELETE FROM complaints WHERE id IN (
        SELECT id FROM complaints WHERE deleted_at IS NOT NULL
        ORDER BY deleted_at DESC,id DESC LIMIT %s FOR UPDATE) RETURNING id""", (count,)).fetchall()
    if rows:
        audit(conn, "trash_auto_purged", actor, count=len(rows), order="newest_archived_first", capacity=TRASH_CAPACITY)
    return len(rows)


def reserve_capacity(conn, incoming, actor):
    """Reserve space using EXISTING trash, never active complaints.

    At capacity: incoming 1..100 -> remove 100; incoming >100 -> remove
    incoming+100. Legacy over-capacity archives are additionally brought in bound.
    """
    total = conn.execute("SELECT count(*) AS n FROM complaints WHERE deleted_at IS NOT NULL").fetchone()["n"]
    overflow = total + incoming - TRASH_CAPACITY
    if overflow <= 0:
        return 0
    requested = incoming + TRASH_BUFFER if incoming > TRASH_BUFFER else TRASH_BUFFER
    return purge_recent(conn, max(requested, overflow), actor)


def archive_rows(conn, ids, actor, reason):
    """Caller holds the global trash lock and locks/validates active rows."""
    if not ids:
        return {"deleted": 0, "automatically_purged": 0}
    purged = reserve_capacity(conn, len(ids), actor)
    rows = conn.execute("""UPDATE complaints SET trash_previous_status=complaint_status,
        original_owner_user_id=COALESCE(original_owner_user_id,owner_user_id),
        complaint_status='취소',cancelled_at=NOW(),cancelled_by_role=%s,
        cancellation_reason=%s,deleted_at=NOW(),status_updated_at=NOW(),
        analysis_state='cancelled',analysis_revision=analysis_revision+1
        WHERE id=ANY(%s) AND deleted_at IS NULL RETURNING id""", (actor["role"], reason, ids)).fetchall()
    moved = [row["id"] for row in rows]
    conn.execute("DELETE FROM complaint_responses WHERE complaint_id=ANY(%s) AND response_state='draft'", (moved,))
    # A single incoming batch itself can exceed the entire capacity. Bound that
    # case too, preferring the same newest-first rule and an extra 100-row buffer.
    total = conn.execute("SELECT count(*) AS n FROM complaints WHERE deleted_at IS NOT NULL").fetchone()["n"]
    if total > TRASH_CAPACITY:
        purged += purge_recent(conn, total - TRASH_CAPACITY + TRASH_BUFFER, actor)
    audit(conn, "complaints_archived", actor, count=len(moved), deleted_by_role=actor["role"], automatically_purged=purged)
    return {"deleted": len(moved), "automatically_purged": purged}


def restore_record(complaint_id, actor):
    require_admin(actor)
    require_department_id(actor)
    with connection() as conn:
        lock_trash(conn)
        row = conn.execute("SELECT * FROM complaints WHERE id=%s AND deleted_at IS NOT NULL FOR UPDATE", (complaint_id,)).fetchone()
        if not row:
            raise HTTPException(404, "삭제 보관함에서 해당 민원을 찾을 수 없습니다.")
        require_manage_trash(actor, row)
        profile = row.get('dedup_profile') or {}
        if profile.get('v') == 1:
            lock_identities(conn, [profile], None)
            if find_duplicate(conn, profile, None, exclude_id=complaint_id):
                raise HTTPException(409, '같은 원문의 활성 민원이 관리자 보관함에 이미 있습니다. 기존 민원을 먼저 확인해 주세요.')
        # Detaching ownership protects ALL citizen APIs, search and future replies;
        # original ownership is retained solely for administrator audit purposes.
        restored = conn.execute("""UPDATE complaints SET
            original_owner_user_id=COALESCE(original_owner_user_id,owner_user_id),owner_user_id=NULL,
            category=CASE WHEN department_id IS NULL THEN (SELECT name FROM departments WHERE id=%s) ELSE category END,
            department_id=COALESCE(department_id,%s),deleted_at=NULL,cancelled_at=NULL,
            cancelled_by_role=NULL,cancellation_reason=NULL,restored_at=NOW(),
            complaint_status=CASE WHEN trash_previous_status='완료' OR EXISTS (
                SELECT 1 FROM complaint_responses r WHERE r.complaint_id=complaints.id AND r.response_state='sent')
                THEN '완료' ELSE '접수' END,
            summary=CASE WHEN BTRIM(summary)='' THEN LEFT(content,700) ELSE summary END,
            analysis_state='completed',analysis_revision=analysis_revision+1,status_updated_at=NOW()
            WHERE id=%s RETURNING id,title,department_id,category AS department_name,complaint_status""",
            (actor["department_id"], actor["department_id"], complaint_id)).fetchone()
        audit(conn, "trash_restored_admin_only", actor, complaint_id=complaint_id, original_deleted_by=row.get("cancelled_by_role"))
    return {"restored": 1, "complaint": restored, "admin_only": True}


def permanent_ids(ids, password, actor):
    require_admin(actor)
    require_department_id(actor)
    check_password(actor, password)
    ids = list(dict.fromkeys(ids))
    with connection() as conn:
        lock_trash(conn)
        rows = conn.execute("SELECT id,department_id FROM complaints WHERE id=ANY(%s) AND deleted_at IS NOT NULL ORDER BY id FOR UPDATE", (ids,)).fetchall()
        if len(rows) != len(ids):
            raise HTTPException(409, "선택한 데이터가 변경되었습니다. 보관함을 갱신하고 다시 선택해 주세요.")
        for row in rows:
            require_manage_trash(actor, row)
        conn.execute("DELETE FROM complaints WHERE id=ANY(%s) AND deleted_at IS NOT NULL", (ids,))
        audit(conn, "trash_permanently_deleted", actor, count=len(ids), ids=ids)
    return {"permanently_deleted": len(ids)}


def permanent_department(department_id, password, actor):
    require_admin(actor)
    if require_department_id(actor) != department_id:
        raise HTTPException(403, "담당 부서의 삭제된 데이터만 전체 영구 삭제할 수 있습니다.")
    check_password(actor, password)
    with connection() as conn:
        lock_trash(conn)
        rows = conn.execute("DELETE FROM complaints WHERE deleted_at IS NOT NULL AND department_id=%s RETURNING id", (department_id,)).fetchall()
        audit(conn, "trash_department_permanently_deleted", actor, department_id=department_id, count=len(rows))
    return {"permanently_deleted": len(rows)}
