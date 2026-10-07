from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException
import psycopg

from ..db import connection, fetch_one
from ..security import actor_from_auth, current_account, issue_token, password_hash, public_user, uuid4, verify_password
from ..models import LoginBody, SignupBody, PasswordBody
from ..dependencies import check_password

router = APIRouter(prefix="/api/auth", tags=["auth"])

@router.post("/login")
def login(body: LoginBody):
    user = fetch_one("SELECT id,owner_id,username,display_name,account_role,department,password_salt,password_hash FROM app_users WHERE username=%s", (body.username.strip(),))
    if not user or not verify_password(body.password, user["password_salt"], user["password_hash"]):
        raise HTTPException(401, "계정 또는 비밀번호가 올바르지 않습니다.")
    return {"token": issue_token(user), "user": public_user(user)}

@router.post("/signup", status_code=201)
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

@router.get("/context")
def auth_context(actor: Annotated[dict, Depends(actor_from_auth)]):
    return {"user": public_user(current_account(actor))}

@router.post("/verify-password")
def verify(body: PasswordBody, actor: Annotated[dict, Depends(actor_from_auth)]):
    check_password(actor, body.password)
    return {"verified": True}