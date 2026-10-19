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
