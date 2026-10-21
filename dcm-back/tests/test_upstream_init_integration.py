"""Validate the real bootstrap SQL on a disposable DB, never the app DB."""
import asyncio
import csv
import io
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from fastapi.testclient import TestClient

from app.db import connection
from app.main import app
from app.routers import rag_imports as routes
from app.services import rag_imports as service
from app.services.trash import ensure_trash_schema
from app.dedup import ensure_dedup_schema
from app.settings import DATABASE_URL
from app.utils import util


class UpstreamInitIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = "complaintai_upstream_init_test_" + uuid.uuid4().hex
        cls.url = make_conninfo(DATABASE_URL, dbname=cls.name)
        with psycopg.connect(DATABASE_URL, autocommit=True) as db:
            db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.name)))
        cls.addClassCleanup(cls.drop_database)
        root = Path(__file__).resolve().parents[2]
        with psycopg.connect(cls.url, autocommit=True) as db:
            db.execute((root / "database/init.sql").read_text(encoding="utf-8"))
        db_patch = patch("app.db.DATABASE_URL", cls.url)
        db_patch.start()
        cls.addClassCleanup(db_patch.stop)
        ensure_trash_schema()
        ensure_dedup_schema()
        service.ensure_rag_import_schema()
        cls.client = TestClient(app)
        cls.addClassCleanup(cls.client.close)

    @classmethod
    def drop_database(cls):
        assert cls.name.startswith("complaintai_upstream_init_test_")
        assert cls.name != conninfo_to_dict(DATABASE_URL).get("dbname")
        with psycopg.connect(DATABASE_URL, autocommit=True) as db:
            db.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.name)))

    def login(self, username, password):
        response = self.client.post("/api/auth/login", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return {"Authorization": "Bearer " + response.json()["token"]}

    def test_all_initial_admins_have_usable_departments(self):
        accounts = (
            ("admin-administration-safety", "AdminService!2026", "문화·행정·안전"),
            ("admin-land-transport", "AdminLand!2026", "교통·국토"),
            ("admin-housing", "AdminHousing!2026", "주택·건축"),
            ("admin-environment", "AdminEnvironment!2026", "환경·위생"),
            ("admin-welfare", "AdminWelfare!2026", "보건·복지"),
            ("admin-fire", "AdminFire!2026", "노동·기업"),
            ("admin-other", "AdminOther!2026", "기타"),
        )
        for username, password, name in accounts:
            with self.subTest(username=username):
                headers = self.login(username, password)
                response = self.client.get("/api/department/context", headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["department_name"], name)
                self.assertEqual(response.json()["categories"], [name])

    def test_permissions_follow_the_new_database_ids(self):
        culture = self.login("admin-administration-safety", "AdminService!2026")
        labor = self.login("admin-fire", "AdminFire!2026")
        with connection() as db:
            owner = db.execute("SELECT owner_id FROM app_users WHERE username='user-a'").fetchone()["owner_id"]
            ids = {}
            for name in ("문화·행정·안전", "노동·기업"):
                department = db.execute("SELECT id FROM departments WHERE name=%s", (name,)).fetchone()["id"]
                ids[name] = db.execute("""INSERT INTO complaints(title,content,summary,category,department_id,owner_user_id)
                    VALUES('권한 테스트','테스트 민원 원문','테스트 요약',%s,%s,%s) RETURNING id""",
                    (name, department, owner)).fetchone()["id"]
        for headers, own, other in ((culture, "문화·행정·안전", "노동·기업"), (labor, "노동·기업", "문화·행정·안전")):
            with self.subTest(department=own):
                detail = self.client.get(f"/api/complaints/{ids[own]}", headers=headers)
                self.assertEqual(detail.status_code, 200, detail.text)
                self.assertTrue(detail.json()["complaint"]["can_manage"])
                detail = self.client.get(f"/api/complaints/{ids[other]}", headers=headers)
                self.assertEqual(detail.status_code, 200, detail.text)
                self.assertFalse(detail.json()["complaint"]["can_manage"])
                response = self.client.post(f"/api/department/complaints/{ids[other]}/start", headers=headers)
                self.assertEqual(response.status_code, 403, response.text)
                response = self.client.post(f"/api/department/complaints/{ids[own]}/start", headers=headers)
                self.assertEqual(response.status_code, 200, response.text)

    def test_rag_import_preserves_original_and_embedding_on_bootstrap_schema(self):
        headers = self.login("admin-fire", "AdminFire!2026")
        content = '  제1조 원문\n공백과 "인용"을 보존합니다.  '
        source = io.StringIO(newline="")
        writer = csv.writer(source)
        writer.writerow(["제목", "본문"])
        writer.writerows([["규칙", content], ["중복 원문", content]])
        with tempfile.TemporaryDirectory() as folder, patch.object(util, "FILE_STORAGE_DIR", Path(folder)), patch.object(routes, "start_worker"):
            response = self.client.post("/api/rag-imports", headers=headers,
                files={"file": ("rules.csv", source.getvalue().encode("utf-8-sig"), "text/csv")})
            self.assertEqual(response.status_code, 202, response.text)
            identifier = response.json()["id"]
            with connection() as db:
                job = db.execute("UPDATE rag_import_jobs SET status='processing',stage='counting' WHERE id=%s RETURNING *", (identifier,)).fetchone()
            vector = [1.0] + [0.0] * 1023
            with patch.object(service, "embeddingForSelect", new=AsyncMock(return_value=vector)):
                asyncio.run(service.process_job(job))
        result = self.client.get(f"/api/rag-imports/{identifier}", headers=headers).json()
        self.assertEqual(result["status"], "completed")
        self.assertEqual((result["saved_rows"], result["duplicate_rows"], result["failed_rows"]), (1, 1, 0))
        with connection() as db:
            document = db.execute("SELECT content,vector_dims(embedding) AS dimensions FROM department_documents WHERE document_id=%s", (job["document_id"],)).fetchone()
        self.assertEqual(document, {"content": content, "dimensions": 1024})
