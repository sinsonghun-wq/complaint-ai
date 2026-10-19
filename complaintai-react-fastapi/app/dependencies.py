from typing import Any

from fastapi import HTTPException
from .departments import department_id, department_name
from .security import current_account, verify_password

def owner_id(actor: dict[str, Any]) -> str | None:
    return None if actor["role"] == "admin" else actor["owner_id"]

def allowed(actor: dict[str, Any]) -> list[str]:
    name = department_name(actor.get("department_id")) if actor["role"] == "admin" else None
    return [name] if name else []

def require_department(actor: dict[str, Any]) -> list[str]:
    categories = allowed(actor)
    if not categories:
        raise HTTPException(403, "부서가 지정된 관리자만 사용할 수 있습니다.")
    return categories


def require_department_id(actor: dict[str, Any]) -> int:
    identifier = department_id(actor.get("department_id"))
    if actor["role"] != "admin" or identifier is None:
        raise HTTPException(403, "부서가 지정된 관리자만 사용할 수 있습니다.")
    return identifier


def can_manage(actor: dict[str, Any], row: dict[str, Any]) -> bool:
    identifier = department_id(actor.get("department_id"))
    return actor["role"] == "admin" and identifier is not None and identifier == row.get("department_id")


def require_department_access(actor: dict[str, Any], row: dict[str, Any]) -> None:
    require_department_id(actor)
    if not can_manage(actor, row):
        raise HTTPException(403, "담당 부서의 민원만 변경할 수 있습니다.")


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
