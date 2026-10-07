from pathlib import Path

from .db import connection


def ensure_rag_schema() -> None:
    migration = Path(__file__).resolve().parents[2] / 'database/migrations/20261007_ai_recommendations.sql'
    with connection() as conn:
        conn.execute(migration.read_text(encoding='utf-8'))
