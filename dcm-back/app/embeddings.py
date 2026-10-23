"""Shared 1024-dimensional vector contract; never pad or truncate vectors."""
import math

from .settings import EMBEDDING_DIMENSIONS


def normalize_embedding(values):
    if not isinstance(values, (list, tuple)) or len(values) != EMBEDDING_DIMENSIONS:
        raise ValueError(f"임베딩은 {EMBEDDING_DIMENSIONS}차원이어야 합니다.")
    vector = [float(value) for value in values]
    if not all(math.isfinite(value) for value in vector):
        raise ValueError("임베딩에 유효하지 않은 값이 있습니다.")
    norm = math.sqrt(sum(value * value for value in vector))
    if not norm:
        raise ValueError("빈 임베딩 벡터는 저장할 수 없습니다.")
    return [value / norm for value in vector]


def check_embedding_schema():
    from .db import connection

    with connection() as db:
        rows = db.execute("""SELECT c.relname,format_type(a.atttypid,a.atttypmod) AS type
            FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relkind='r'
              AND c.relname IN ('complaints','department_documents','rag_chunks','department_documents_chunks')
              AND a.attname='embedding' AND NOT a.attisdropped""").fetchall()
    for row in rows:
        if row['type'] != f'vector({EMBEDDING_DIMENSIONS})':
            raise RuntimeError(f"{row['relname']}.embedding={row['type']}: 1024차원 이전이 필요합니다. "
                               "scripts/reindex_embeddings.py 안내를 확인하세요. 기존 벡터를 자르지 마세요.")
