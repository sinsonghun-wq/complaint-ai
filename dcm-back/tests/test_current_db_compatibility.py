"""Use a disposable DB shaped like doc-main; never renumber the live catalog."""
import asyncio
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from fastapi.testclient import TestClient

from app.db import connection
from app.main import app
from app.settings import DATABASE_URL
from app.ai import fallback
from app.departments import ensure_intake_departments
from app.services.complaint import classify_submission


class CurrentDatabaseCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = "complaintai_doc_compat_test_" + uuid.uuid4().hex
        cls.url = make_conninfo(DATABASE_URL, dbname=cls.name)
        with psycopg.connect(DATABASE_URL, autocommit=True) as db:
            db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.name)))
        fixtures = Path(__file__).with_name("fixtures")
        with psycopg.connect(cls.url) as db:
            db.execute((fixtures / "init.sql").read_text(encoding="utf8"))
            db.execute((fixtures / "seed_demo_accounts.sql").read_text(encoding="utf8"))
            # The current doc-main schema has IDs, but no name-sync triggers,
            # department code column, or original text department columns.
            for table in ("app_users", "complaints", "complaint_responses", "department_documents"):
                db.execute(sql.SQL("DROP TRIGGER sync_department ON {}").format(sql.Identifier(table)))
                db.execute(sql.SQL("ALTER TABLE {} DROP COLUMN department").format(sql.Identifier(table)))
            db.execute("ALTER TABLE departments DROP COLUMN code, DROP COLUMN aliases")
            for identifier, name in enumerate(("행정·안전", "국토·교통", "주택건축", "환경·위생", "보건복지", "소방", "기타"), 1):
                db.execute("UPDATE departments SET name=%s WHERE id=%s", (name, identifier))
            db.execute("UPDATE app_users SET department_id=5 WHERE username='admin-welfare'")
        cls.db_patch = patch("app.db.DATABASE_URL", cls.url)
        cls.db_patch.start()
        cls.client = TestClient(app)
        cls.tokens = {}
        for username, password in (("user-a", "UserA!2026"), ("admin-land-transport", "AdminLand!2026"), ("admin-welfare", "AdminWelfare!2026")):
            response = cls.client.post("/api/auth/login", json={"username": username, "password": password})
            assert response.status_code == 200, response.text
            cls.tokens[username] = {"Authorization": "Bearer " + response.json()["token"]}
        with connection() as db:
            cls.owner = db.execute("SELECT owner_id FROM app_users WHERE username='user-a'").fetchone()["owner_id"]

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.db_patch.stop()
        assert cls.name.startswith("complaintai_doc_compat_test_")
        with psycopg.connect(DATABASE_URL, autocommit=True) as db:
            db.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.name)))

    def setUp(self):
        with connection() as db:
            db.execute("DELETE FROM complaints")

    def complaint(self, category="교통·국토", department=2, state="completed"):
        with connection() as db:
            return db.execute("""INSERT INTO complaints(title,content,summary,category,department_id,owner_user_id,analysis_state,analysis_revision)
                VALUES('도로 민원','학교 앞 도로를 보수해 주세요.','도로 보수 요청',%s,%s,%s,%s,1) RETURNING id""",
                (category, department, self.owner, state)).fetchone()["id"]

    def test_legacy_catalog_and_current_welfare_account_are_unchanged(self):
        result = self.client.get("/api/departments", headers=self.tokens["admin-welfare"])
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["departments"][4], {"id": 5, "name": "보건복지"})
        context = self.client.get("/api/department/context", headers=self.tokens["admin-welfare"])
        self.assertEqual(context.json()["categories"], ["보건복지"])

    def test_backfill_only_links_ids_and_counts_all_category_aliases(self):
        identifiers = [self.complaint(name, None) for name in ("기타", "노동·기업", "보건·복지")]
        with connection() as db:
            before = db.execute("SELECT id,content,category,embedding FROM complaints ORDER BY id").fetchall()
        ensure_intake_departments()
        with connection() as db:
            self.assertEqual(before, db.execute("SELECT id,content,category,embedding FROM complaints ORDER BY id").fetchall())
            self.assertEqual([r["department_id"] for r in db.execute("SELECT department_id FROM complaints ORDER BY id")], [7, 7, 5])
        counts = self.client.get("/api/complaints/counts", headers=self.tokens["admin-welfare"]).json()["categories"]
        self.assertEqual(next(row["count"] for row in counts if row["department_id"] == 7), 2)

    def test_classification_assigns_existing_id_without_triggers(self):
        identifier = self.complaint(None, None, "pending")
        record = {**fallback("도로 민원", "학교 앞 도로를 보수해 주세요."), "category": "교통·국토"}
        with patch("app.services.complaint.analyze", new=AsyncMock(return_value=record)), patch("app.services.complaint.embeddingForSelect", new=AsyncMock(return_value=None)):
            asyncio.run(classify_submission(identifier, 1))
        with connection() as db:
            self.assertEqual(db.execute("SELECT department_id FROM complaints WHERE id=%s", (identifier,)).fetchone()["department_id"], 2)

    def test_final_editor_text_and_status_reach_the_citizen(self):
        identifier = self.complaint()
        admin = self.tokens["admin-land-transport"]
        base = f"/api/department/complaints/{identifier}"
        self.assertEqual(self.client.post(base + "/start", headers=admin).status_code, 200)
        self.assertEqual(self.client.post(base + "/responses", headers=admin, json={"content": "임시로 작성한 답변"}).status_code, 201)
        sent = self.client.post(base + "/responses/send", headers=admin, json={"content": "최종 수정한 공식 답변입니다."})
        self.assertEqual(sent.status_code, 200, sent.text)
        citizen = self.client.get(f"/api/complaints/{identifier}", headers=self.tokens["user-a"]).json()["complaint"]
        self.assertEqual(citizen["complaint_status"], "완료")
        self.assertEqual(citizen["latest_response"], "최종 수정한 공식 답변입니다.")
        self.assertEqual(citizen["latest_response_state"], "sent")

    def test_transfer_updates_category_and_id_without_triggers(self):
        identifier = self.complaint()
        transferred = self.client.post(f"/api/department/complaints/{identifier}/transfer", headers=self.tokens["admin-land-transport"], json={"department_id": 5})
        self.assertEqual(transferred.status_code, 200, transferred.text)
        with connection() as db:
            row = db.execute("SELECT category,department_id FROM complaints WHERE id=%s", (identifier,)).fetchone()
        self.assertEqual(row, {"category": "보건·복지", "department_id": 5})

    def test_department_bulk_delete_includes_every_status_source_and_page(self):
        identifiers = []
        completed_id = None
        default_reason = "해당 민원은 잘못된 사유로 인해 취소되었습니다."
        with connection() as db:
            author = db.execute("SELECT id FROM app_users WHERE username='admin-land-transport'").fetchone()["id"]
        for status in ("접수", "진행중", "완료", "취소"):
            for source in (None, "sample.pdf"):
                identifier = self.complaint()
                identifiers.append(identifier)
                with connection() as db:
                    db.execute("UPDATE complaints SET complaint_status=%s,source_file=%s,owner_user_id=%s WHERE id=%s",
                               (status, source, self.owner if source is None else None, identifier))
                if status == "완료" and source is None:
                    completed_id = identifier
                    with connection() as db:
                        db.execute("""INSERT INTO complaint_responses(id,complaint_id,author_user_id,department_id,content,response_state)
                            VALUES(%s,%s,%s,2,'보존해야 하는 완료 답변','sent')""", (uuid.uuid4(), identifier, author))
        # Legacy file imports can still carry citizen ownership.
        legacy_file = self.complaint()
        identifiers.append(legacy_file)
        with connection() as db:
            db.execute("UPDATE complaints SET source_file='legacy.xlsx' WHERE id=%s", (legacy_file,))
        other = self.complaint("보건·복지", 5)
        unassigned = self.complaint(None, None, "pending")
        archived = self.complaint()
        with connection() as db:
            db.execute("UPDATE complaints SET deleted_at=NOW(),complaint_status='취소' WHERE id=%s", (archived,))
            untouched = db.execute("SELECT * FROM complaints WHERE id=ANY(%s) ORDER BY id", ([other, unassigned, archived],)).fetchall()
            originals = db.execute("SELECT id,content,complaint_status FROM complaints WHERE id=ANY(%s) ORDER BY id", (identifiers,)).fetchall()
        # List-only filters/pagination must never narrow department-wide deletion.
        response = self.client.request("DELETE", "/api/departments/2/complaints?source=file&limit=1&offset=7&status=접수",
            headers=self.tokens["admin-land-transport"], json={"reason": default_reason})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"deleted": 9, "automatically_purged": 0})
        with connection() as db:
            rows = db.execute("SELECT * FROM complaints WHERE id=ANY(%s) ORDER BY id", (identifiers,)).fetchall()
            for row, original in zip(rows, originals):
                self.assertIsNotNone(row["deleted_at"])
                self.assertEqual(row["complaint_status"], "취소")
                self.assertEqual(row["trash_previous_status"], original["complaint_status"])
                self.assertEqual(row["content"], original["content"])
                self.assertEqual(row["cancellation_reason"], default_reason)
            self.assertEqual(untouched, db.execute("SELECT * FROM complaints WHERE id=ANY(%s) ORDER BY id", ([other, unassigned, archived],)).fetchall())
            self.assertEqual(db.execute("SELECT content FROM complaint_responses WHERE complaint_id=%s", (completed_id,)).fetchone()["content"], "보존해야 하는 완료 답변")
        admin = self.tokens["admin-land-transport"]
        active = self.client.get("/api/complaints?department_id=2&deleted=false", headers=admin).json()
        self.assertEqual(active["complaints"], [])
        self.assertEqual(active["total"], 0)
        counts = self.client.get("/api/complaints/counts", headers=admin).json()["categories"]
        self.assertEqual(next((row["count"] for row in counts if row["department_id"] == 2), 0), 0)
        trash = self.client.get("/api/trash/items?department_id=2&limit=100", headers=admin).json()
        self.assertEqual({row["id"] for row in trash["items"]}, set(identifiers + [archived]))
        self.assertEqual(trash["total"], 10)
        for identifier in identifiers:
            detail = self.client.get(f"/api/trash/{identifier}", headers=admin).json()["complaint"]
            self.assertEqual(detail["cancellation_reason"], default_reason)
        citizen = self.client.get("/api/complaints?deleted=false", headers=self.tokens["user-a"]).json()
        # Citizens keep the existing cancellation notice and the recorded reason.
        for row in citizen["complaints"]:
            if row["id"] in identifiers:
                self.assertEqual(row["complaint_status"], "취소")
                self.assertIsNotNone(row["deleted_at"])
                self.assertEqual(row["cancellation_reason"], default_reason)
        restored = self.client.post(f"/api/trash/{completed_id}/restore", headers=self.tokens["admin-land-transport"])
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json()["complaint"]["complaint_status"], "완료")

    def test_department_bulk_delete_preserves_permission_and_reason_checks(self):
        identifier = self.complaint()
        for headers, body, expected in (
            (self.tokens["admin-welfare"], {"reason": "다른 부서 요청"}, 403),
            (self.tokens["user-a"], {"reason": "민원인 요청"}, 403),
            (self.tokens["admin-land-transport"], {"reason": "  "}, 400),
        ):
            response = self.client.request("DELETE", "/api/departments/2/complaints", headers=headers, json=body)
            self.assertEqual(response.status_code, expected, response.text)
            with connection() as db:
                self.assertIsNone(db.execute("SELECT deleted_at FROM complaints WHERE id=%s", (identifier,)).fetchone()["deleted_at"])
