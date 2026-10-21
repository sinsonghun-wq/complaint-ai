"""Exercise all seven admins against a disposable PostgreSQL DB, never reset app DB."""
import json
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from fastapi.testclient import TestClient

from app.main import app
from app.db import connection
from app.settings import DATABASE_URL
from app.departments import CATALOG

ACCOUNTS = [
    (1, "admin-labor-business", "AdminLabor!2026"),
    (2, "admin-land-transport", "AdminLand!2026"),
    (3, "admin-housing", "AdminHousing!2026"),
    (4, "admin-environment", "AdminEnvironment!2026"),
    (5, "admin-administration-safety", "AdminService!2026"),
    (6, "admin-welfare", "AdminWelfare!2026"),
    (7, "admin-other", "AdminOther!2026"),
]


class DepartmentPermissionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[2]
        cls.name = "complaintai_permissions_test_" + uuid.uuid4().hex
        cls.params = conninfo_to_dict(DATABASE_URL)
        cls.params["dbname"] = cls.name
        cls.url = make_conninfo(**cls.params)
        with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.name)))
        try:
            with psycopg.connect(cls.url) as conn:
                conn.execute((cls.root / "dcm-back/tests/fixtures/init.sql").read_text(encoding="utf-8"))
                conn.execute((cls.root / "dcm-back/tests/fixtures/seed_demo_accounts.sql").read_text(encoding="utf-8"))
        except Exception:
            cls.drop_test_database()
            raise
        cls.database_patch = patch("app.db.DATABASE_URL", cls.url)
        cls.database_patch.start()
        cls.client = TestClient(app)

    @classmethod
    def drop_test_database(cls):
        assert cls.name.startswith("complaintai_permissions_test_")
        assert cls.name != conninfo_to_dict(DATABASE_URL).get("dbname")
        with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.name)))

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.database_patch.stop()
        cls.drop_test_database()

    def setUp(self):
        self.tokens = {}
        for identifier, username, password in ACCOUNTS:
            response = self.client.post("/api/auth/login", json={"username": username, "password": password})
            self.assertEqual(response.status_code, 200, response.text)
            self.tokens[identifier] = response.json()["token"]
        with connection() as conn:
            self.owner = conn.execute("SELECT owner_id FROM app_users WHERE username='user-a'").fetchone()["owner_id"]
            self.ids = {}
            for department in CATALOG:
                row = conn.execute("""INSERT INTO complaints(title,content,summary,department_id,owner_user_id)
                    VALUES(%s,%s,%s,%s,%s) RETURNING id""",
                    (f"권한 테스트 {department['id']}", "민원 원문입니다.", "요약입니다.", department["id"], self.owner)).fetchone()
                self.ids[department["id"]] = row["id"]

    def tearDown(self):
        with connection() as conn:
            conn.execute("DELETE FROM complaints")

    def request(self, department, method, path, **kwargs):
        return self.client.request(method, path, headers={"Authorization": "Bearer " + self.tokens[department]}, **kwargs)

    def snapshot(self, identifier):
        with connection() as conn:
            return conn.execute("SELECT * FROM complaints WHERE id=%s", (identifier,)).fetchone()

    def test_all_seven_logins_catalog_and_authoritative_names(self):
        for department in CATALOG:
            with self.subTest(department=department["id"]):
                response = self.request(department["id"], "GET", "/api/auth/context")
                user = response.json()["user"]
                self.assertEqual(user["department_id"], department["id"])
                self.assertEqual(user["department_name"], department["name"])
                self.assertNotIn("department", user)
                context = self.request(department["id"], "GET", "/api/department/context")
                self.assertEqual(context.status_code, 200, context.text)
                self.assertEqual(context.json()["categories"], [department["name"]])
                result = self.request(department["id"], "GET", "/api/departments").json()["departments"]
                self.assertEqual([(d["id"],d["name"]) for d in result], [(d["id"],d["name"]) for d in CATALOG])

    def test_49_pairs_read_allowed_manage_flag_matches_department_id(self):
        for actor in range(1, 8):
            for target in range(1, 8):
                with self.subTest(actor=actor, target=target):
                    listing = self.request(actor, "GET", f"/api/complaints?department_id={target}").json()
                    self.assertEqual(listing["total"], 1)
                    row = listing["complaints"][0]
                    self.assertEqual(row["department_id"], target)
                    self.assertEqual(row["can_manage"], actor == target)
                    detail = self.request(actor, "GET", f"/api/complaints/{self.ids[target]}")
                    self.assertEqual(detail.status_code, 200)
                    self.assertEqual(detail.json()["complaint"]["content"], "민원 원문입니다.")
                    self.assertEqual(detail.json()["complaint"]["can_manage"], actor == target)
            own_list = self.request(actor, "GET", "/api/department/complaints")
            self.assertEqual({r["department_id"] for r in own_list.json()["complaints"]}, {actor})

    def test_42_cross_department_pairs_cannot_start_answer_edit_send_transfer_cancel(self):
        for actor in range(1, 8):
            for target in range(1, 8):
                if actor == target:
                    continue
                identifier = self.ids[target]
                before = self.snapshot(identifier)
                base = f"/api/department/complaints/{identifier}"
                actions = [
                    ("POST", base+"/start", {}),
                    ("PATCH", base+"/status", {"json":{"status":"진행중"}}),
                    ("POST", base+"/responses", {"json":{"content":"권한 없는 답변입니다."}}),
                    ("DELETE", base+"/responses/draft", {}),
                    ("POST", base+"/responses/send", {"json":{"content":"권한 없는 최종 답변입니다."}}),
                    ("POST", base+"/transfer", {"json":{"department_id":actor}}),
                    ("DELETE", f"/api/complaints/{identifier}", {"json":{"reason":"타 부서 삭제 시도"}}),
                    ("DELETE", f"/api/departments/{target}/complaints", {"json":{"reason":"타 부서 일괄 삭제 시도"}}),
                ]
                for method, path, options in actions:
                    with self.subTest(actor=actor, target=target, action=path):
                        result = self.request(actor, method, path, **options)
                        self.assertEqual(result.status_code, 403, result.text)
                self.assertEqual(self.snapshot(identifier), before)
        with connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) count FROM complaint_responses").fetchone()["count"], 0)

    def test_all_seven_can_start_update_draft_and_send_current_answer(self):
        for actor in range(1,8):
            with self.subTest(actor=actor):
                base = f"/api/department/complaints/{self.ids[actor]}"
                self.assertEqual(self.request(actor,"POST",base+"/start").status_code,200)
                for text in ("첫 번째 답변 초안입니다.", "수정한 답변 초안입니다."):
                    result = self.request(actor,"POST",base+"/responses",json={"content":text})
                    self.assertEqual(result.status_code,201,result.text)
                final = "수정 후 최종 전송할 답변입니다."
                result = self.request(actor,"POST",base+"/responses/send",json={"content":final})
                self.assertEqual(result.status_code,200,result.text)
                self.assertEqual(result.json()["response"]["content"],final)
                self.assertEqual(self.snapshot(self.ids[actor])["complaint_status"],"완료")
                self.assertEqual(self.request(actor,"POST",base+"/responses",json={"content":"완료 후 수정 금지"}).status_code,409)
        with connection() as conn:
            rows=conn.execute("SELECT department_id,department,content,response_state FROM complaint_responses ORDER BY department_id").fetchall()
            self.assertEqual(len(rows),7)
            self.assertEqual([r["department"] for r in rows],[d["name"] for d in CATALOG])

    def test_transfer_changes_permissions_immediately(self):
        identifier=self.ids[1]
        base=f"/api/department/complaints/{identifier}"
        result=self.request(1,"POST",base+"/transfer",json={"department_id":2})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()["complaint"]["department_name"],CATALOG[1]["name"])
        self.assertEqual(self.request(1,"POST",base+"/responses",json={"content":"이전 부서 답변 금지"}).status_code,403)
        self.assertEqual(self.request(2,"POST",base+"/responses",json={"content":"새 부서 답변 허용"}).status_code,201)
        self.assertEqual(self.request(2,"POST",base+"/transfer",json={"department_id":999}).status_code,422)

    def test_stale_token_cannot_keep_old_department_or_role(self):
        with connection() as conn:
            conn.execute("UPDATE app_users SET department_id=2 WHERE username='admin-labor-business'")
        try:
            context=self.request(1,"GET","/api/auth/context").json()["user"]
            self.assertEqual(context["department_id"],2)
            self.assertEqual(context["department_name"],CATALOG[1]["name"])
            self.assertEqual(self.request(1,"POST",f"/api/department/complaints/{self.ids[1]}/start").status_code,403)
            self.assertEqual(self.request(1,"POST",f"/api/department/complaints/{self.ids[2]}/start").status_code,200)
            with connection() as conn:
                conn.execute("UPDATE app_users SET account_role='user',department_id=NULL WHERE username='admin-labor-business'")
            self.assertEqual(self.request(1,"POST",f"/api/department/complaints/{self.ids[2]}/start").status_code,403)
        finally:
            with connection() as conn:
                conn.execute("UPDATE app_users SET account_role='admin',department_id=1 WHERE username='admin-labor-business'")

    def test_normal_users_have_no_department_and_cannot_manage_admin_data(self):
        login=self.client.post("/api/auth/login",json={"username":"user-b","password":"UserB!2026"}).json()
        self.assertIsNone(login["user"]["department_id"])
        headers={"Authorization":"Bearer "+login["token"]}
        self.assertEqual(self.client.get("/api/complaints",headers=headers).json()["total"],0)
        self.assertEqual(self.client.get("/api/department/context",headers=headers).status_code,403)
        self.assertEqual(self.client.post(f"/api/department/complaints/{self.ids[1]}/responses",headers=headers,json={"content":"차단 테스트"}).status_code,403)

    def test_global_test_cleanup_cannot_bypass_department_scope(self):
        for actor,_,password in ACCOUNTS:
            for path in ("/api/complaints/department/all","/api/complaints/deleted/all"):
                result=self.request(actor,"DELETE",path,json={"password":password})
                self.assertEqual(result.status_code,403,result.text)
        with connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) count FROM complaints").fetchone()["count"],7)

    def test_database_schema_migration_is_repeatable_and_catalog_is_consistent(self):
        migration=(self.root/"dcm-back/tests/fixtures/department_ids.sql").read_text(encoding="utf-8")
        before=[self.snapshot(self.ids[i]) for i in range(1,8)]
        with connection() as conn:
            conn.execute(migration)
            conn.execute(migration)
            for department in CATALOG:
                row=conn.execute("SELECT code,name FROM departments WHERE id=%s",(department["id"],)).fetchone()
                self.assertEqual((row["code"],row["name"]),(department["code"],department["name"]))
        self.assertEqual([self.snapshot(self.ids[i]) for i in range(1,8)],before)
        with self.assertRaises(psycopg.Error):
            with connection() as conn:
                conn.execute("UPDATE app_users SET department_id=999 WHERE username='admin-land-transport'")
        with self.assertRaises(psycopg.Error):
            with connection() as conn:
                conn.execute("UPDATE app_users SET department_id=2 WHERE username='user-a'")

    def test_pending_and_legacy_classification_get_the_same_department_id(self):
        with connection() as conn:
            row=conn.execute("INSERT INTO complaints(title,content) VALUES('미분류 민원','원문') RETURNING id,department_id").fetchone()
            self.assertIsNone(row["department_id"])
            row=conn.execute("UPDATE complaints SET category='국토·교통' WHERE id=%s RETURNING category,department_id,department",(row["id"],)).fetchone()
            self.assertEqual((row["category"],row["department_id"],row["department"]),("교통·국토",2,"교통·국토"))
            row=conn.execute("UPDATE complaints SET category=NULL WHERE title='미분류 민원' RETURNING department_id,department").fetchone()
            self.assertIsNone(row["department_id"])
            self.assertIsNone(row["department"])

    def test_legacy_schema_migration_preserves_accounts_complaints_and_answers(self):
        migration=(self.root/"dcm-back/tests/fixtures/department_ids.sql").read_text(encoding="utf-8")
        with connection() as conn:
            before=conn.execute("SELECT id,owner_id,password_hash FROM app_users ORDER BY id").fetchall()
            conn.execute("ALTER TABLE app_users DROP CONSTRAINT app_users_department_role_check")
            for table in ("app_users","complaints","complaint_responses","department_documents"):
                conn.execute(sql.SQL("DROP TRIGGER sync_department ON {}").format(sql.Identifier(table)))
                conn.execute(sql.SQL("ALTER TABLE {} DROP COLUMN department_id").format(sql.Identifier(table)))
            conn.execute("DROP TABLE departments")
            conn.execute("UPDATE complaints SET category='국토·교통',department=NULL WHERE id=%s",(self.ids[2],))
            admin=conn.execute("SELECT id FROM app_users WHERE username='admin-land-transport'").fetchone()["id"]
            conn.execute("""INSERT INTO complaint_responses(id,complaint_id,author_user_id,department,content)
                VALUES(%s,%s,%s,'2','보존할 기존 답변')""",(uuid.uuid4(),self.ids[2],admin))
            conn.execute(migration)
            self.assertEqual(conn.execute("SELECT id,owner_id,password_hash FROM app_users ORDER BY id").fetchall(),before)
            complaint=conn.execute("SELECT title,content,category,department_id FROM complaints WHERE id=%s",(self.ids[2],)).fetchone()
            self.assertEqual(complaint["content"],"민원 원문입니다.")
            self.assertEqual((complaint["category"],complaint["department_id"]),("교통·국토",2))
            answer=conn.execute("SELECT content,department_id,department FROM complaint_responses").fetchone()
            self.assertEqual((answer["content"],answer["department_id"],answer["department"]),("보존할 기존 답변",2,"교통·국토"))


if __name__ == "__main__":
    unittest.main()
