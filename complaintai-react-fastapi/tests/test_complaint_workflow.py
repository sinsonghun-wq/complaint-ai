"""Integration tests against the configured PostgreSQL DB.

Run: python -m unittest discover -s tests -v
Only test-created accounts and complaints are removed during cleanup.
"""
import uuid
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.ai import fallback
from app.db import connection, fetch_one
from app.main import app
from app.security import issue_token, password_hash


class ComplaintWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.users = []
        self.ids = []
        self.ai_patch = patch("app.main.analyze", new=AsyncMock(side_effect=lambda title, content: fallback(title, content)))
        self.embedding_patch = patch("app.main.embedding", new=AsyncMock(return_value=None))
        self.ai_patch.start(); self.embedding_patch.start()
        self.client = TestClient(app)
        self.client.__enter__()
        self.user = self.account("user")
        self.other_user = self.account("user")
        self.admin = self.account("admin", "국토·교통")
        self.other_admin = self.account("admin", "환경·위생")

    def tearDown(self):
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM complaints WHERE owner_user_id=ANY(%s::uuid[])", ([u["owner_id"] for u in self.users],))
                cur.execute("DELETE FROM app_users WHERE id=ANY(%s::uuid[])", ([u["id"] for u in self.users],))
        self.client.__exit__(None, None, None)
        self.ai_patch.stop(); self.embedding_patch.stop()

    def account(self, role, department=None):
        identifier, owner = str(uuid.uuid4()), str(uuid.uuid4())
        name = "workflow-" + uuid.uuid4().hex[:16]
        salt, digest = password_hash("WorkflowTest!2026")
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO app_users(id,owner_id,username,email,display_name,password_salt,password_hash,account_role,department) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)", (identifier, owner, name, name + "@test.local", name, salt, digest, role, department))
        user = {"id": identifier, "owner_id": owner, "account_role": role, "department": department}
        self.users.append(user)
        return user

    def request(self, method, path, actor=None, **kwargs):
        return self.client.request(method, path, headers={"Authorization": "Bearer " + issue_token(actor or self.user)}, **kwargs)

    def complaint(self):
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO complaints(title,content,summary,category,owner_user_id) VALUES('도로 보수 요청','도로에 구멍이 있습니다. 보수해 주세요.','도로 보수 요청','국토·교통',%s) RETURNING id", (self.user["owner_id"],))
                identifier = cur.fetchone()["id"]
        self.ids.append(identifier)
        return identifier

    def complete(self, identifier):
        base = f"/api/department/complaints/{identifier}"
        self.assertEqual(self.request("POST", base + "/start", self.admin).status_code, 200)
        self.assertEqual(self.request("POST", base + "/responses", self.admin, json={"content": "보수를 완료했습니다."}).status_code, 201)
        self.assertEqual(self.request("POST", base + "/responses/send", self.admin).status_code, 200)

    def test_initial_edit_and_start_lock_edit(self):
        identifier = self.complaint()
        body = {"title": "도로 민원", "content": "도로에 구멍이 있습니다. 보수가 필요합니다."}
        self.assertEqual(self.request("PATCH", f"/api/complaints/{identifier}", json=body).status_code, 200)
        started = self.request("POST", f"/api/department/complaints/{identifier}/start", self.admin)
        self.assertEqual(started.json()["complaint"]["complaint_status"], "진행중")
        self.assertEqual(self.request("PATCH", f"/api/complaints/{identifier}", json=body).status_code, 409)

    def test_user_cancellation_stops_draft_and_hides_content(self):
        identifier = self.complaint()
        base = f"/api/department/complaints/{identifier}"
        self.request("POST", base + "/start", self.admin)
        self.request("POST", base + "/responses", self.admin, json={"content": "관리자 초안입니다."})
        user_view = self.request("GET", f"/api/complaints/{identifier}").json()["complaint"]
        self.assertEqual(user_view["latest_response"], "")
        self.assertEqual(self.request("DELETE", f"/api/complaints/{identifier}").status_code, 200)
        admin_view = self.request("GET", f"/api/complaints/{identifier}", self.admin).json()["complaint"]
        self.assertEqual(admin_view["complaint_status"], "취소")
        self.assertEqual(admin_view["content"], "")
        self.assertFalse(any(r["id"] == identifier for r in self.request("GET", "/api/complaints").json()["complaints"]))
        for method, route, body in [("POST", "/responses", {"content": "다시 답변"}), ("POST", "/responses/send", None), ("POST", "/transfer", {"category": "환경·위생"}), ("POST", "/start", None)]:
            self.assertEqual(self.request(method, base + route, self.admin, json=body).status_code, 409)

    def test_admin_reason_required_and_visible_to_owner(self):
        identifier = self.complaint()
        route = f"/api/complaints/{identifier}"
        self.assertEqual(self.request("DELETE", route, self.admin).status_code, 400)
        self.assertEqual(self.request("DELETE", route, self.admin, json={"reason": "민원 대상과 무관한 광고입니다."}).status_code, 200)
        record = self.request("GET", route).json()["complaint"]
        self.assertEqual(record["cancelled_by_role"], "admin")
        self.assertIn("광고", record["cancellation_reason"])
        self.assertTrue(any(r["id"] == identifier for r in self.request("GET", "/api/complaints").json()["complaints"]))
        self.assertEqual(self.request("DELETE", route).status_code, 409)

    def test_completed_immutable_and_followup_snapshot(self):
        identifier = self.complaint(); self.complete(identifier)
        route = f"/api/complaints/{identifier}"
        self.assertEqual(self.request("PATCH", route, json={"title": "변경", "content": "변경된 민원"}).status_code, 409)
        self.assertEqual(self.request("DELETE", route).status_code, 409)
        self.assertEqual(self.request("DELETE", route, self.admin, json={"reason": "완료 삭제"}).status_code, 409)
        base = f"/api/department/complaints/{identifier}"
        self.assertEqual(self.request("POST", base + "/responses", self.admin, json={"content": "또 답변"}).status_code, 409)
        self.assertEqual(self.request("POST", base + "/responses/send", self.admin).status_code, 409)
        self.assertEqual(self.request("POST", base + "/transfer", self.admin, json={"category": "환경·위생"}).status_code, 409)
        response = self.request("POST", route + "/follow-up", json={"title": "보수 추가 요청", "content": "아직 다른 위치에 구멍이 남아 있습니다."})
        self.assertEqual(response.status_code, 201)
        child = self.request("GET", f"/api/complaints/{response.json()['complaint']['id']}", self.admin).json()["complaint"]
        self.assertEqual(child["complaint_status"], "접수")
        self.assertEqual(child["parent_complaint_id"], identifier)
        self.assertIn("구멍", child["previous_context"]["content"])
        self.assertEqual(child["previous_context"]["response"], "보수를 완료했습니다.")

    def test_owner_and_department_permissions(self):
        identifier = self.complaint()
        self.assertEqual(self.request("GET", f"/api/complaints/{identifier}", self.other_user).status_code, 404)
        self.assertEqual(self.request("DELETE", f"/api/complaints/{identifier}", self.other_user).status_code, 404)
        self.assertEqual(self.request("POST", f"/api/department/complaints/{identifier}/start", self.other_admin).status_code, 404)
        self.assertEqual(self.request("POST", f"/api/department/complaints/{identifier}/start", self.user).status_code, 403)
        self.assertEqual(self.request("GET", f"/api/complaints/{identifier}", self.other_admin).status_code, 200)

    def test_draft_delete_keeps_in_progress(self):
        identifier = self.complaint()
        base = f"/api/department/complaints/{identifier}"
        self.request("POST", base + "/start", self.admin)
        self.request("POST", base + "/responses", self.admin, json={"content": "임시 답변"})
        result = self.request("DELETE", base + "/responses/draft", self.admin)
        self.assertEqual(result.json()["complaint_status"], "진행중")
        self.assertEqual(self.request("PATCH", base + "/status", self.admin, json={"status": "완료"}).status_code, 409)

    def test_transfer_removes_previous_draft_and_new_department_starts(self):
        identifier = self.complaint()
        base = f"/api/department/complaints/{identifier}"
        self.request("POST", base + "/responses", self.admin, json={"content": "이전 부서 초안"})
        result = self.request("POST", base + "/transfer", self.admin, json={"category": "환경·위생"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["complaint"]["complaint_status"], "접수")
        self.assertEqual(self.request("POST", base + "/start", self.admin).status_code, 404)
        self.assertEqual(self.request("POST", base + "/start", self.other_admin).status_code, 200)
        self.assertEqual(fetch_one("SELECT count(*) count FROM complaint_responses WHERE complaint_id=%s", (identifier,))["count"], 0)

    def test_followup_rejects_open_cancelled_and_foreign_parent(self):
        identifier = self.complaint()
        route = f"/api/complaints/{identifier}/follow-up"
        body = {"title": "재민원", "content": "추가로 요청하는 사항입니다."}
        self.assertEqual(self.request("POST", route, json=body).status_code, 409)
        self.complete(identifier)
        self.assertEqual(self.request("POST", route, self.other_user, json=body).status_code, 409)
        cancelled = self.complaint()
        self.request("DELETE", f"/api/complaints/{cancelled}")
        self.assertEqual(self.request("POST", f"/api/complaints/{cancelled}/follow-up", json=body).status_code, 409)

    def test_send_cancel_race_has_only_one_winner(self):
        identifier = self.complaint()
        base = f"/api/department/complaints/{identifier}"
        self.request("POST", base + "/responses", self.admin, json={"content": "동시 처리 답변"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            send = pool.submit(self.request, "POST", base + "/responses/send", self.admin)
            cancel = pool.submit(self.request, "DELETE", f"/api/complaints/{identifier}")
            self.assertEqual(sorted([send.result().status_code, cancel.result().status_code]), [200, 409])
        row = fetch_one("SELECT complaint_status FROM complaints WHERE id=%s", (identifier,))
        count = fetch_one("SELECT count(*) count FROM complaint_responses WHERE complaint_id=%s AND response_state='sent'", (identifier,))["count"]
        self.assertEqual(count, int(row["complaint_status"] == "완료"))


if __name__ == "__main__":
    unittest.main()
