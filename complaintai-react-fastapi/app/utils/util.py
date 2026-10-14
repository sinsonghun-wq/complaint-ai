from typing import Any
import uuid

from fastapi import HTTPException, Path, UploadFile

from app.dependencies import allowed
from app.settings import FILE_STORAGE_DIR

def require_open_complaint(row: dict) -> None:
    if row["complaint_status"] not in {"접수", "진행중"}:
        raise HTTPException(409, "해당 민원은 삭제(취소)되었습니다." if row["complaint_status"] == "취소" else "답변이 완료된 민원은 변경할 수 없습니다.")

def hide_cancelled_content(row: dict) -> dict:
    if row.get("complaint_status") == "취소":
        row.update(title="취소된 민원", content="", summary="", latest_response="", latest_response_state="", previous_context=None)
    return row

def locked_complaint(cur, complaint_id: int, actor: dict) -> dict:
    clause, args = scoped_where(actor)
    cur.execute(f"SELECT * FROM complaints WHERE id=%s{clause} FOR UPDATE", (complaint_id, *args))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "민원을 찾을 수 없거나 해당 민원에 접근할 수 없습니다.")
    if row['deleted_at']:
        raise HTTPException(409, '해당 민원은 삭제(취소)되었습니다.')
    return row

def scoped_where(actor: dict, parameter: int = 2) -> tuple[str, list[Any]]:
    if actor["role"] == "admin": return " AND category=ANY(%s)", [allowed(actor)]
    return " AND owner_user_id=%s", [actor["owner_id"]]

def hide_cancelled_content(row: dict) -> dict:
    if row.get("complaint_status") == "취소":
        row.update(title="취소된 민원", content="", summary="", latest_response="", latest_response_state="", previous_context=None)
    return row

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

def locked_complaint(cur, complaint_id: int, actor: dict) -> dict:
    clause, args = scoped_where(actor)
    cur.execute(f"SELECT * FROM complaints WHERE id=%s{clause} FOR UPDATE", (complaint_id, *args))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "민원을 찾을 수 없거나 해당 민원에 접근할 수 없습니다.")
    if row['deleted_at']:
        raise HTTPException(409, '해당 민원은 삭제(취소)되었습니다.')
    return row


def require_open_complaint(row: dict) -> None:
    if row["complaint_status"] not in {"접수", "진행중"}:
        raise HTTPException(409, "해당 민원은 삭제(취소)되었습니다." if row["complaint_status"] == "취소" else "답변이 완료된 민원은 변경할 수 없습니다.")

def require_admin(actor: dict[str, Any]) -> None:
    if actor["role"] != "admin":
        raise HTTPException(403, "부서 관리자 전용 기능입니다.")