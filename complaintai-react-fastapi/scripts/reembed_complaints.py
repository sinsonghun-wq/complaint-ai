"""기존 민원 벡터를 '본문 텍스트 그대로' 형식으로 다시 만듭니다.

임베딩 입력이 구조화 문서(제목·원문·요약·카테고리·핵심어)에서 민원 본문(content)만으로 바뀌었으므로,
예전에 저장된 벡터는 새 검색 벡터와 비교할 수 없습니다. 배포 후 한 번 실행하세요.

    cd complaintai-react-fastapi
    python -m scripts.reembed_complaints              # 삭제·취소되지 않은 민원 전부
    python -m scripts.reembed_complaints --only-missing  # 벡터가 비어 있는 민원만 (실패분 재시도)

임베딩에 실패한 민원은 예전 형식 벡터가 섞여 남지 않도록 embedding 을 NULL 로 비웁니다.
"""
from __future__ import annotations

import argparse
import asyncio

from app import ai
from app.ai import ACTIVE_EMBEDDING_MODEL, embeddingForSelect, redact
from app.db import connection, fetch_all
from app.dependencies import vector_literal


async def run(only_missing: bool) -> None:
    where = "deleted_at IS NULL AND complaint_status<>'취소' AND COALESCE(content,'')<>''"
    if only_missing:
        where += " AND embedding IS NULL"
    rows = fetch_all(f"SELECT id, content, analysis_revision FROM complaints WHERE {where} ORDER BY id")
    print(f"대상 민원 {len(rows)}건")
    done = failed = 0
    for index, row in enumerate(rows, 1):
        # 접수 경로와 같게: 분석 단계에서 개인정보를 가린 본문을 임베딩합니다.
        vector = await embeddingForSelect(redact(row["content"]))
        if vector is None:
            # 임베딩 서버 오류 후 30초 차단이 걸렸을 수 있으므로 한 번 기다렸다가 재시도합니다.
            ai._embedding_unavailable_until = 0.0
            await asyncio.sleep(1)
            vector = await embeddingForSelect(redact(row["content"]))
        with connection() as conn:
            conn.execute(
                """UPDATE complaints SET embedding=%s::vector, embedding_model=%s
                   WHERE id=%s AND analysis_revision=%s AND deleted_at IS NULL""",
                (vector_literal(vector), ACTIVE_EMBEDDING_MODEL if vector else None, row["id"], row["analysis_revision"]),
            )
            conn.commit()
        done += int(vector is not None)
        failed += int(vector is None)
        if index % 100 == 0:
            print(f"  {index}/{len(rows)} 처리 (성공 {done}, 실패 {failed})")
    print(f"완료: 성공 {done}건, 실패 {failed}건" + (" - 실패분은 --only-missing 으로 다시 실행하세요." if failed else ""))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only-missing", action="store_true", help="embedding 이 비어 있는 민원만 처리")
    asyncio.run(run(parser.parse_args().only_missing))
