"""Verify real files/API/worker using a disposable DB, never the user's records.

Run from complaintai-react-fastapi:
  python scripts/verify_file_imports.py --samples-dir PATH --csv PATH
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from fastapi.testclient import TestClient

from app.db import connection
from app.main import app
from app.settings import DATABASE_URL, FILE_STORAGE_DIR
from app import worker_manager


def run(samples: Path, csv_path: Path):
    repo = Path(__file__).resolve().parents[2]
    name = "complaintai_file_verify_" + uuid.uuid4().hex
    url = make_conninfo(DATABASE_URL, dbname=name)
    report = {"files": [], "csv": {}, "ocr": {}}
    uploaded = []
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with psycopg.connect(url) as db:
            db.execute((repo / "database/init.sql").read_text(encoding="utf-8"))
            db.execute((repo / "database/seed_demo_accounts.sql").read_text(encoding="utf-8"))
        with patch("app.db.DATABASE_URL", url), patch.dict(os.environ, {"DATABASE_URL": url}):
            client = TestClient(app)
            login = client.post("/api/auth/login", json={"username": "admin-land-transport", "password": "AdminLand!2026"})
            assert login.status_code == 200, login.text
            headers = {"Authorization": "Bearer " + login.json()["token"]}
            for suffix in (".xlsx", ".pdf", ".hwp"):
                path = next(samples.glob("*72건" + suffix))
                started = time.monotonic()
                with path.open("rb") as stream:
                    response = client.post("/api/intake", headers=headers, files={"file": (path.name, stream)})
                assert response.status_code == 200, response.text
                records = response.json()["complaints"]
                assert len(records) == 72, (suffix, len(records))
                result = client.post("/api/complaints/batch", headers=headers, json={"complaints": records})
                assert result.status_code == 201, result.text
                assert result.json()["saved"] == 72, result.text
                listing = client.get("/api/complaints?source=file&limit=100", headers=headers).json()
                counts = client.get("/api/complaints/counts", headers=headers).json()
                assert listing["total"] == 72
                assert sum(c["count"] for c in counts["categories"]) == 72
                assert all(bool(r["can_manage"]) == (r["department_id"] == 2) for r in listing["complaints"])
                duplicate = client.post("/api/complaints/batch", headers=headers, json={"complaints": records})
                assert duplicate.status_code == 201, duplicate.text
                assert duplicate.json() == {"saved": 0, "duplicates": 72}, duplicate.text
                with connection() as db:
                    warnings = db.execute("SELECT count(*) AS n FROM complaints WHERE analysis_metadata ? 'embedding_warning'").fetchone()["n"]
                    db.execute("DELETE FROM complaints")  # This newly created test DB only.
                item = {"format": suffix, "extracted": 72, "saved": 72, "duplicates": 72,
                        "category_counts_total": 72, "embedding_dimension_warnings": warnings,
                        "seconds": round(time.monotonic() - started, 2)}
                report["files"].append(item)
                print(json.dumps(item), flush=True)

            # A rasterized real case has no PDF text layer and must use OCR.
            import pymupdf as fitz
            with tempfile.TemporaryDirectory(prefix="complaintai-ocr-verify-") as folder:
                path = Path(folder) / "scan.pdf"
                original = next(samples.glob("*72건.pdf"))
                with fitz.open(original) as doc, fitz.open() as scan:
                    source = next(p for p in doc if "신청원인" in p.get_text() and "사례" in p.get_text())
                    pix = source.get_pixmap(matrix=fitz.Matrix(2, 2))
                    page = scan.new_page(width=source.rect.width, height=source.rect.height)
                    page.insert_image(page.rect, stream=pix.tobytes("png"))
                    scan.save(path)
                with path.open("rb") as stream:
                    result = client.post("/api/intake", headers=headers, files={"file": ("scan.pdf", stream)})
                assert result.status_code == 200, result.text
                assert len(result.json()["complaints"]) == 1
                assert len(result.json()["complaints"][0]["content"]) > 30
                report["ocr"] = {"scanned_pdf": "passed", "complaints": 1}
                print(json.dumps(report["ocr"]), flush=True)

            user = client.post("/api/auth/login", json={"username": "user-a", "password": "UserA!2026"}).json()
            denied = client.post("/api/intake", headers={"Authorization": "Bearer " + user["token"]}, files={"file": ("no.csv", b"title,content\na,b")})
            assert denied.status_code == 403
            report["citizen_upload_blocked"] = True

            started = time.monotonic()
            with csv_path.open("rb") as stream:
                result = client.post("/api/imports", headers=headers, files={"file": (csv_path.name, stream, "text/csv")})
            assert result.status_code == 202, result.text
            job = result.json()
            job_id = job["job_id"]
            with connection() as db:
                uploaded.append(Path(db.execute("SELECT storage_path FROM import_jobs WHERE id=%s", (job_id,)).fetchone()["storage_path"]))
            if job.get("needs_mapping"):
                assert job["mapping"]["content_columns"], "Manual mapping required: inspect the provided CSV"
                mapped = client.post(f"/api/imports/{job_id}/mapping", headers=headers, json={
                    "title_column": job["mapping"]["title_column"], "content_columns": job["mapping"]["content_columns"], "save_mapping": False})
                assert mapped.status_code == 202, mapped.text
            observed = set()
            while time.monotonic() - started < 1200:
                state = client.get(f"/api/imports/{job_id}", headers=headers).json()
                progress = (state["status"], state["completed_rows"])
                if progress not in observed:
                    observed.add(progress)
                    print(json.dumps({"csv_status": progress[0], "completed": progress[1], "total": state["total_rows"]}), flush=True)
                if state["status"] in ("completed", "failed"):
                    break
                time.sleep(2)
            assert state["status"] == "completed", state
            assert state["failed_rows"] == 0, state
            assert state["completed_rows"] == state["total_rows"]
            assert state["saved_rows"] + state["skipped_rows"] == state["total_rows"]
            counts = client.get("/api/complaints/counts", headers=headers).json()
            assert sum(c["count"] for c in counts["categories"]) == state["saved_rows"]
            with connection() as db:
                modes = db.execute("SELECT processing_mode,count(*) AS n FROM complaints GROUP BY processing_mode").fetchall()
            report["csv"] = {k: state[k] for k in ("status", "total_rows", "completed_rows", "checkpoint_rows", "saved_rows", "skipped_rows", "failed_rows")}
            report["csv"].update(seconds=round(time.monotonic()-started,2), processing_modes=modes, progress_observations=len(observed))
            print(json.dumps(report["csv"], default=str), flush=True)
            client.close()
    finally:
        process = worker_manager._process
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=15)
        for path in uploaded:
            assert path.resolve().parent == FILE_STORAGE_DIR.resolve()
            path.unlink(missing_ok=True)
        assert name.startswith("complaintai_file_verify_")
        assert name != conninfo_to_dict(DATABASE_URL).get("dbname")
        with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
    return report


if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("--samples-dir", required=True, type=Path)
    args.add_argument("--csv", required=True, type=Path)
    args.add_argument("--report", type=Path)
    options = args.parse_args()
    report = run(options.samples_dir, options.csv)
    if options.report:
        options.report.parent.mkdir(parents=True, exist_ok=True)
        options.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
