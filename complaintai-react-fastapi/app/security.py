from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from typing import Any

from fastapi import Header, HTTPException

from .db import fetch_one
from .settings import AUTH_TOKEN_SECRET


def uuid4() -> str:
    return str(uuid.uuid4())


def password_hash(password: str, salt_hex: str | None = None) -> tuple[str, str]:
    salt_hex = salt_hex or secrets.token_hex(16)
    encoded = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=16384, r=8, p=1, dklen=64)
    return salt_hex, encoded.hex()


def verify_password(password: str, salt_hex: str, expected_hex: str) -> bool:
    _, actual = password_hash(password, salt_hex)
    return hmac.compare_digest(actual, expected_hex)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue_token(actor: dict[str, Any]) -> str:
    payload = {"sub": str(actor["id"]), "owner_id": str(actor["owner_id"]), "role": actor["account_role"], "department": actor.get("department"), "exp": int(time.time() * 1000) + 8 * 60 * 60 * 1000}
    body = _b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
    signature = _b64(hmac.new(AUTH_TOKEN_SECRET.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{signature}"


def read_token(value: str | None) -> dict[str, Any] | None:
    try:
        body, signature = (value or "").removeprefix("Bearer ").split(".")
        expected = _b64(hmac.new(AUTH_TOKEN_SECRET.encode(), body.encode(), hashlib.sha256).digest())
        payload = json.loads(_unb64(body))
        if not hmac.compare_digest(signature, expected) or int(payload["exp"]) <= int(time.time() * 1000):
            return None
        return payload
    except Exception:
        return None


def actor_from_auth(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    actor = read_token(authorization)
    if not actor:
        raise HTTPException(401, "로그인이 필요합니다.")
    return actor


def current_account(actor: dict[str, Any]) -> dict[str, Any]:
    user = fetch_one("SELECT id, owner_id, username, display_name, account_role, department, password_salt, password_hash FROM app_users WHERE id=%s", (actor["sub"],))
    if not user:
        raise HTTPException(401, "계정을 찾을 수 없습니다.")
    return user


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(user["id"]), "owner_id": str(user["owner_id"]), "username": user["username"], "name": user.get("display_name") or user["username"], "role": user["account_role"], "department": user.get("department")}
