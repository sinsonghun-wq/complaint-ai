from pathlib import Path
import re
from typing import Any
import uuid

from fastapi import HTTPException, UploadFile

from app.dependencies import allowed
from app.settings import FILE_STORAGE_DIR
from ..logger import logger

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
    cur.execute(f"SELECT * FROM complaints WHERE id=%s{clause} FOR UPDATE", (complaint_id, [return_category(*args[0])]))
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

def requestFilter(content: str) -> str:
    # 공통 치환 함수: 매칭된 문자열 내의 숫자와 영문자만 '▲'로 변경
    def mask_match(match):
        return re.sub(r'[0-9a-zA-Z]', '▲', match.group(0))
    
    # 1. 주민등록번호 필터링 (하이픈 유무 모두 대응)
    # 예: 123456-1234567 또는 1234561234567
    content = re.sub(r'\d{6}-?[1-8]\d{6}', mask_match, content)
    
    # 2. 전화번호 필터링 (하이픈이 있거나 없는 경우 모두 대응)
    # 예: 010-1234-5678, 01012345678, 02-123-4567, 021234567 등
    content = re.sub(r'\d{2,3}-?\d{3,4}-?\d{4}', mask_match, content)
    
    # 3. 이메일 주소 필터링
    content = re.sub(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', mask_match, content)
    
    return content

def return_category(num):
    if type(num) is str:
         num = int(num)
    if num==1:
        return "문화·행정·안전"
    elif num ==2:
        return "교통·국토"
    elif num ==3:
            return "주택·건축"
    elif num ==4:
            return "환경·위생"
    elif num ==5:
            return "보건·복지"
    elif num ==6:
            return "노동·기업"
    else:
         return "기타"