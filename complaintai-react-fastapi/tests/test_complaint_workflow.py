"""Integration tests against the configured PostgreSQL DB.

Run: python -m unittest discover -s tests -v
Only test-created accounts and complaints are removed during cleanup.
"""
import asyncio
import uuid
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.ai import fallback
from app.db import connection, fetch_one
from app.main import app, classify_submission
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

    def test_source_filter_and_pagination(self):
        direct = self.complaint()
        uploaded = self.complaint()
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE complaints SET source_file='workflow-test.csv' WHERE id=%s", (uploaded,))
        path = "/api/complaints?category=" + "국토·교통"
        for source, identifier, submitted in (("user", direct, True), ("file", uploaded, False)):
            data = self.request("GET", path + "&source=" + source).json()
            self.assertEqual(data["total"], 1)
            self.assertEqual(data["complaints"][0]["id"], identifier)
            self.assertEqual(data["complaints"][0]["submitted_by_user"], submitted)
            page = self.request("GET", path + "&source=" + source + "&limit=1&offset=1").json()
            self.assertEqual(page["total"], 1)
            self.assertEqual(page["complaints"], [])
        self.assertEqual(self.request("GET", path + "&source=all").json()["total"], 2)
        self.assertEqual(self.request("GET", path + "&source=file", self.other_user).json()["total"], 0)
        self.assertEqual(self.request("GET", path + "&source=invalid").status_code, 400)
        self.assertFalse(self.request("GET", f"/api/complaints/{uploaded}", self.admin).json()["complaint"]["submitted_by_user"])
        admin_list = self.request("GET", path + "&source=file", self.admin).json()["complaints"]
        self.assertTrue(any(item["id"] == uploaded for item in admin_list))
        self.assertTrue(all(not item["submitted_by_user"] for item in admin_list))

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

    def test_repeated_edits_remain_allowed_until_admin_starts(self):
        identifier = self.complaint()
        for index in range(3):
            body = {'title': f'도로 민원 수정 {index}', 'content': f'도로에 구멍이 있어 보수를 요청합니다. 수정 번호 {index}'}
            self.assertEqual(self.request('PATCH', f'/api/complaints/{identifier}', json=body).status_code, 200)
            row = self.request('GET', f'/api/complaints/{identifier}').json()['complaint']
            self.assertEqual(row['complaint_status'], '접수')
            self.assertEqual(row['analysis_state'], 'completed')
            self.assertEqual(row['title'], body['title'])
        self.assertEqual(self.request('POST', f'/api/department/complaints/{identifier}/start', self.admin).status_code, 200)
        self.assertEqual(self.request('PATCH', f'/api/complaints/{identifier}', json=body).status_code, 409)

    def pending_submission(self):
        with patch('app.main.BackgroundTasks.add_task'):
            response = self.request('POST', '/api/complaints', json={'title': '직접 작성 제목', 'content': '도로의 구멍을 보수해 주세요.'})
        self.assertEqual(response.status_code, 201)
        return response.json()['complaint']

    def test_submission_visible_before_classification(self):
        row = self.pending_submission()
        detail = self.request('GET', f"/api/complaints/{row['id']}").json()['complaint']
        self.assertEqual(detail['title'], '직접 작성 제목')
        self.assertEqual(detail['content'], '도로의 구멍을 보수해 주세요.')
        self.assertIsNone(detail['category'])
        self.assertEqual(detail['complaint_status'], '접수')
        self.assertEqual(detail['analysis_state'], 'pending')
        self.assertFalse(self.ai_patch.new.called)
        asyncio.run(classify_submission(row['id'], row['analysis_revision']))
        detail = self.request('GET', f"/api/complaints/{row['id']}").json()['complaint']
        self.assertEqual(detail['category'], '국토·교통')
        self.assertEqual(detail['analysis_state'], 'completed')
        self.assertEqual(self.request('POST', '/api/complaints', self.admin, json={'content': '관리자 작성'}).status_code, 403)

    def test_pending_submission_duplicates_and_deleted_resubmission(self):
        row = self.pending_submission()
        with patch('app.main.BackgroundTasks.add_task'):
            duplicate = self.request('POST', '/api/complaints', json={'title': '제목만 다른 민원', 'content': '도로의 구멍을 보수해 주세요.'})
            self.assertEqual(duplicate.json()['complaint']['id'], row['id'])
            self.assertEqual(duplicate.json()['duplicates'], 1)
            self.assertEqual(self.request('DELETE', f"/api/complaints/{row['id']}").status_code, 200)
            resubmitted = self.request('POST', '/api/complaints', json={'title': '다시 제출', 'content': '도로의 구멍을 보수해 주세요.'})
        self.assertNotEqual(resubmitted.json()['complaint']['id'], row['id'])

    def test_delete_while_classifying_does_not_resurrect(self):
        row = self.pending_submission()
        async def scenario():
            started, release = asyncio.Event(), asyncio.Event()
            async def delayed(title, content):
                started.set()
                await release.wait()
                return fallback(title, content)
            with patch('app.main.analyze', new=delayed):
                task = asyncio.create_task(classify_submission(row['id'], row['analysis_revision']))
                await asyncio.wait_for(started.wait(), 3)
                response = await asyncio.to_thread(self.request, 'DELETE', f"/api/complaints/{row['id']}")
                self.assertEqual(response.status_code, 200)
                release.set()
                await asyncio.wait_for(task, 3)
        asyncio.run(scenario())
        stored = fetch_one('SELECT * FROM complaints WHERE id=%s', (row['id'],))
        self.assertEqual(stored['complaint_status'], '취소')
        self.assertIsNotNone(stored['deleted_at'])
        self.assertIsNone(stored['category'])
        self.assertEqual(stored['analysis_state'], 'cancelled')
        self.assertNotIn(row['id'], [r['id'] for r in self.request('GET', '/api/complaints').json()['complaints']])
        self.assertFalse(self.embedding_patch.new.called)

    def test_edit_while_classifying_discards_old_result(self):
        row = self.pending_submission()
        async def scenario():
            started, release = asyncio.Event(), asyncio.Event()
            async def delayed(title, content):
                started.set()
                await release.wait()
                return fallback(title, content)
            with patch('app.main.analyze', new=delayed):
                task = asyncio.create_task(classify_submission(row['id'], row['analysis_revision']))
                await asyncio.wait_for(started.wait(), 3)
                with patch('app.main.BackgroundTasks.add_task'):
                    response = await asyncio.to_thread(self.request, 'PATCH', f"/api/complaints/{row['id']}", json={'title': '수정된 제목', 'content': '쓰레기 악취 및 환경 오염을 처리해 주세요.'})
                self.assertEqual(response.status_code, 200)
                release.set()
                await asyncio.wait_for(task, 3)
            stored = fetch_one('SELECT * FROM complaints WHERE id=%s', (row['id'],))
            self.assertIsNone(stored['category'])
            self.assertEqual(stored['analysis_state'], 'pending')
            self.assertEqual(stored['analysis_revision'], row['analysis_revision'] + 1)
            await classify_submission(row['id'], stored['analysis_revision'])
        asyncio.run(scenario())
        stored = fetch_one('SELECT * FROM complaints WHERE id=%s', (row['id'],))
        self.assertEqual(stored['title'], '수정된 제목')
        self.assertEqual(stored['category'], '환경·위생')

    def test_edit_clears_completed_analysis_and_preserves_original_text(self):
        identifier = self.complaint()
        with patch('app.main.BackgroundTasks.add_task'):
            response = self.request('PATCH', f'/api/complaints/{identifier}', json={'title': '새 제목', 'content': '수정된 민원 원문입니다.'})
        self.assertEqual(response.status_code, 200)
        stored = fetch_one('SELECT * FROM complaints WHERE id=%s', (identifier,))
        self.assertIsNone(stored['category'])
        self.assertEqual(stored['summary'], '')
        self.assertEqual(stored['content'], '수정된 민원 원문입니다.')
        self.assertEqual(stored['analysis_state'], 'pending')
        self.assertEqual(self.request('POST', '/api/complaints', json={'content': '  '}).status_code, 400)

    def test_delete_during_embedding_discards_vector(self):
        row = self.pending_submission()
        async def scenario():
            started, release = asyncio.Event(), asyncio.Event()
            async def delayed(record):
                started.set()
                await release.wait()
                return [0.0] * 1536
            with patch('app.main.embedding', new=delayed):
                task = asyncio.create_task(classify_submission(row['id'], row['analysis_revision']))
                await asyncio.wait_for(started.wait(), 3)
                self.assertEqual(fetch_one('SELECT analysis_state FROM complaints WHERE id=%s', (row['id'],))['analysis_state'], 'completed')
                response = await asyncio.to_thread(self.request, 'DELETE', f"/api/complaints/{row['id']}")
                self.assertEqual(response.status_code, 200)
                release.set()
                await asyncio.wait_for(task, 3)
        asyncio.run(scenario())
        self.assertIsNone(fetch_one('SELECT embedding FROM complaints WHERE id=%s', (row['id'],))['embedding'])

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
