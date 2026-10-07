"""Real PostgreSQL/auth/workflow tests; model calls are explicitly mocked."""
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.db import connection
from app.main import app
from app.rag_schema import ensure_rag_schema
from app.security import issue_token, password_hash


class RecommendationApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_rag_schema()

    def setUp(self):
        self.users = []
        self.complaints = []
        self.user = self.account('user')
        self.other = self.account('user')
        self.admin = self.account('admin', '교통·국토')
        self.client = TestClient(app)
        self.model = patch('app.rag.recommend', new=AsyncMock(return_value={'answer_status': 'answered', 'answer': '테스트용 답변 [1]', 'checks': [], 'next_steps': [], 'citations': [{'number': 1}], 'models': {'llm': 'test-double'}}))
        self.generator = self.model.start()

    def tearDown(self):
        self.model.stop()
        with connection() as conn:
            conn.execute('DELETE FROM ai_recommendations WHERE owner_user_id=ANY(%s::uuid[])', ([u['owner_id'] for u in self.users],))
            conn.execute('DELETE FROM complaints WHERE id=ANY(%s::bigint[])', (self.complaints,))
            conn.execute('DELETE FROM app_users WHERE id=ANY(%s::uuid[])', ([u['id'] for u in self.users],))

    def account(self, role, department=None):
        identifier, owner = str(uuid.uuid4()), str(uuid.uuid4())
        username = 'rag-test-' + uuid.uuid4().hex[:12]
        salt, digest = password_hash('RagTest!2026')
        with connection() as conn:
            conn.execute('INSERT INTO app_users(id,owner_id,username,email,display_name,password_salt,password_hash,account_role,department) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)', (identifier, owner, username, username + '@test.local', username, salt, digest, role, department))
        user = {'id': identifier, 'owner_id': owner, 'account_role': role, 'department': department}
        self.users.append(user)
        return user

    def request(self, method, path, actor=None, **kwargs):
        return self.client.request(method, path, headers={'Authorization': 'Bearer ' + issue_token(actor or self.user)}, **kwargs)

    def complaint(self, actor=None, status='접수'):
        with connection() as conn:
            row = conn.execute('INSERT INTO complaints(title,content,category,owner_user_id,complaint_status) VALUES(%s,%s,%s,%s,%s) RETURNING id', ('저장된 원문 제목', '저장된 원문 내용', '교통·국토', (actor or self.user)['owner_id'], status)).fetchone()
        self.complaints.append(row['id'])
        return row['id']

    def test_new_input_does_not_submit_a_complaint(self):
        with connection() as conn:
            before = conn.execute('SELECT COUNT(*) count FROM complaints WHERE owner_user_id=%s', (self.user['owner_id'],)).fetchone()['count']
        response = self.request('POST', '/api/ai-recommendations', json={'title': '신규 추천 요청', 'content': '제목과 내용으로 답변을 요청합니다.'})
        self.assertEqual(response.status_code, 202)
        job = self.request('GET', '/api/ai-recommendations/' + response.json()['id']).json()
        self.assertEqual(job['status'], 'completed')
        self.assertIsNone(job['complaint_id'])
        with connection() as conn:
            after = conn.execute('SELECT COUNT(*) count FROM complaints WHERE owner_user_id=%s', (self.user['owner_id'],)).fetchone()['count']
        self.assertEqual(before, after)

    def test_existing_input_uses_owned_database_original_and_preserves_status(self):
        identifier = self.complaint(status='완료')
        response = self.request('POST', '/api/ai-recommendations', json={'complaint_id': identifier})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['content'], '저장된 원문 내용')
        with connection() as conn:
            row = conn.execute('SELECT complaint_status,content FROM complaints WHERE id=%s', (identifier,)).fetchone()
        self.assertEqual(row['complaint_status'], '완료')
        self.assertEqual(row['content'], '저장된 원문 내용')

    def test_other_users_complaints_and_results_are_inaccessible(self):
        identifier = self.complaint(self.other)
        self.assertEqual(self.request('POST', '/api/ai-recommendations', json={'complaint_id': identifier}).status_code, 404)
        created = self.request('POST', '/api/ai-recommendations', json={'title': '나의 요청', 'content': '나의 원문 내용'}).json()
        self.assertEqual(self.request('GET', '/api/ai-recommendations/' + created['id'], self.other).status_code, 404)

    def test_cancelled_complaint_cannot_be_selected(self):
        self.assertEqual(self.request('POST', '/api/ai-recommendations', json={'complaint_id': self.complaint(status='취소')}).status_code, 409)

    def test_auth_role_validation_and_queue_limit(self):
        body = {'title': '제목', 'content': '내용'}
        self.assertEqual(self.client.post('/api/ai-recommendations', json=body).status_code, 401)
        self.assertEqual(self.request('POST', '/api/ai-recommendations', self.admin, json=body).status_code, 403)
        for invalid in ({'title': ' ', 'content': ' '}, {'content': '제목 누락'}, {'title': '제목', 'content': '가' * 12001}, {'complaint_id': 1, **body}):
            self.assertEqual(self.request('POST', '/api/ai-recommendations', json=invalid).status_code, 422)
        with patch('app.rag_api.BackgroundTasks.add_task'):
            self.assertEqual(self.request('POST', '/api/ai-recommendations', json=body).status_code, 202)
            self.assertEqual(self.request('POST', '/api/ai-recommendations', json=body).status_code, 202)
            self.assertEqual(self.request('POST', '/api/ai-recommendations', json=body).status_code, 429)

    def test_model_failure_is_saved_and_visible_without_fake_answer(self):
        from app.rag_models import RagError
        with patch('app.rag.recommend', new=AsyncMock(side_effect=RagError('reranker_unavailable', '근거 검토 모델 연결 실패'))):
            created = self.request('POST', '/api/ai-recommendations', json={'title': '제목', 'content': '내용'}).json()
        job = self.request('GET', '/api/ai-recommendations/' + created['id']).json()
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['error_code'], 'reranker_unavailable')
        self.assertIsNone(job['result'])

    def test_followup_uses_owned_server_history_and_preserves_original_complaint(self):
        identifier = self.complaint(status='완료')
        original = self.request('POST', '/api/ai-recommendations', json={'complaint_id': identifier}).json()
        response = self.request('POST', '/api/ai-recommendations', json={
            'parent_recommendation_id': original['id'], 'content': '그러면 어디에 신청하나요?'})
        self.assertEqual(response.status_code, 202)
        followup = response.json()
        self.assertEqual(followup['title'], original['title'])
        self.assertEqual(followup['complaint_id'], identifier)
        self.assertEqual(followup['parent_recommendation_id'], original['id'])
        self.assertEqual(self.generator.await_args.args, ('저장된 원문 제목', '그러면 어디에 신청하나요?'))
        self.assertEqual(self.generator.await_args.kwargs['conversation'], [
            {'role': 'user', 'content': '저장된 원문 내용'},
            {'role': 'assistant', 'content': '테스트용 답변 [1]'},
        ])
        with connection() as conn:
            stored = conn.execute('SELECT conversation FROM ai_recommendations WHERE id=%s', (followup['id'],)).fetchone()
            self.assertEqual(stored['conversation'], self.generator.await_args.kwargs['conversation'])
            self.assertEqual(conn.execute('SELECT complaint_status,content FROM complaints WHERE id=%s', (identifier,)).fetchone(),
                             {'complaint_status': '완료', 'content': '저장된 원문 내용'})

    def test_other_users_or_incomplete_conversations_cannot_be_used_for_followup(self):
        original = self.request('POST', '/api/ai-recommendations', json={'title': '원래 제목', 'content': '원래 설명'}).json()
        body = {'parent_recommendation_id': original['id'], 'content': '이어서 질문합니다.'}
        self.assertEqual(self.request('POST', '/api/ai-recommendations', self.other, json=body).status_code, 404)
        with patch('app.rag_api.BackgroundTasks.add_task'):
            queued = self.request('POST', '/api/ai-recommendations', json={'title': '대기 중 제목', 'content': '대기 중 내용'}).json()
        self.assertEqual(self.request('POST', '/api/ai-recommendations', json={
            'parent_recommendation_id': queued['id'], 'content': '아직 처리 중인가요?'}).status_code, 409)
        self.assertEqual(self.request('POST', '/api/ai-recommendations', json={
            'parent_recommendation_id': str(uuid.uuid4()), 'content': '없는 대화입니다.'}).status_code, 404)

    def test_followup_validation_and_bounded_history(self):
        from app.rag_api import followup_context
        parent = str(uuid.uuid4())
        for body in ({'parent_recommendation_id': parent, 'content': ' '},
                     {'parent_recommendation_id': parent, 'title': '임의 제목', 'content': '질문'},
                     {'parent_recommendation_id': parent, 'complaint_id': 1, 'content': '질문'},
                     {'parent_recommendation_id': 'not-a-uuid', 'content': '질문'}):
            self.assertEqual(self.request('POST', '/api/ai-recommendations', json=body).status_code, 422)
        turns = [{'role': 'user' if index % 2 == 0 else 'assistant', 'content': str(index) * 3000} for index in range(12)]
        context = followup_context({'conversation': turns, 'content': '최근 질문', 'result': {'answer': '최근 답변'}})
        self.assertLessEqual(len(context), 6)
        self.assertLessEqual(sum(len(turn['content']) for turn in context), 4000)
        self.assertEqual(context[0], {'role': 'user', 'content': '0' * 1000})
        self.assertEqual(context[-2:], [{'role': 'user', 'content': '최근 질문'}, {'role': 'assistant', 'content': '최근 답변'}])
