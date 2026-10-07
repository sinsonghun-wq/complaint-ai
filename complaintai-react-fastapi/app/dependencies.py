from typing import Any

from fastapi import HTTPException
from .config import DEPARTMENT_CATEGORIES
from .security import current_account, verify_password

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