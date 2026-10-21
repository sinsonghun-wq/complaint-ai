"""Stable department identity shared with the React catalog and DB schema."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CATALOG = tuple(json.loads(Path(__file__).with_name("departments.json").read_text(encoding="utf-8")))
BY_ID = {row["id"]: row for row in CATALOG}
BY_NAME = {row["name"]: row for row in CATALOG}
if len(BY_ID) != 7 or len(BY_NAME) != 7:
    raise RuntimeError("The department catalog must contain seven unique IDs and names")


def department_id(value: Any) -> int | None:
    # Reject booleans and non-canonical types rather than silently granting ID 1.
    return value if type(value) is int and value in BY_ID else None


def department_name(value: Any) -> str | None:
    row = BY_ID.get(department_id(value))
    return row["name"] if row else None


# Existing doc-main databases may use the original department names/IDs.
# Resolve against their catalog without renumbering accounts or documents.
NAME_GROUPS = (
    {"노동·기업", "노동", "기업"},
    {"교통·국토", "국토·교통", "교통", "건설·국토", "국토교통"},
    {"주택·건축", "주택건축"},
    {"환경·위생"},
    {"문화·행정·안전", "행정·안전", "행정안전", "소방"},
    {"보건·복지", "보건복지"},
    {"기타"},
)


def canonical_category(name):
    aliases = next((group for group in NAME_GROUPS if name in group), {name})
    return next((row["name"] for row in CATALOG if row["name"] in aliases), name)


def resolve_department(conn, name):
    rows = conn.execute("SELECT id,name FROM departments ORDER BY id").fetchall()
    exact = next((row for row in rows if row["name"] == name), None)
    if exact:
        return exact
    aliases = next((group for group in NAME_GROUPS if name in group), set())
    return next((row for row in rows if row["name"] in aliases),
                next((row for row in rows if row["name"] == "기타"), None))


def ensure_intake_departments():
    """Link existing classifications to existing IDs; keep text/vectors intact."""
    from .db import connection
    with connection() as conn:
        rows = conn.execute("SELECT DISTINCT category FROM complaints WHERE department_id IS NULL AND category IS NOT NULL").fetchall()
        for row in rows:
            department = resolve_department(conn, row["category"])
            if department:
                conn.execute("UPDATE complaints SET department_id=%s WHERE department_id IS NULL AND category=%s",
                             (department["id"], row["category"]))
