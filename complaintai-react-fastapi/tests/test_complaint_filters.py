"""Real PostgreSQL filters; only private test accounts/rows are created and removed."""
import unittest
import uuid

from fastapi.testclient import TestClient

from app.db import connection
from app.main import app
from app.security import actor_from_auth


class ComplaintFilterTests(unittest.TestCase):
    def setUp(self):
        self.accounts = [(str(uuid.uuid4()), str(uuid.uuid4())) for _ in range(2)]
        self.actor = {'role': 'user', 'owner_id': self.accounts[0][1], 'sub': self.accounts[0][0]}
        self.original_overrides = dict(app.dependency_overrides)
        app.dependency_overrides[actor_from_auth] = lambda: self.actor
        self.client = TestClient(app)  # Never enters app lifespan/startup.
        self.ids = {}
        with connection() as conn:
            for identifier, owner in self.accounts:
                conn.execute('INSERT INTO app_users(id,owner_id,email,username,account_role) VALUES(%s,%s,%s,%s,%s)',
                             (identifier, owner, identifier + '@filter.test', 'filter-' + identifier, 'user'))
            records = [
                ('unassigned', None, '접수', None, False, False),
                ('blank', '  ', '접수', None, False, False),
                ('assigned', '교통·국토', '접수', None, False, False),
                ('draft', '교통·국토', '진행중', 'draft', False, False),
                ('sent', '교통·국토', '완료', 'sent', False, False),
                ('status-only', '교통·국토', '완료', None, False, False),
                ('other-department', '환경·위생', '완료', 'sent', False, False),
                ('cancelled', '교통·국토', '취소', 'sent', False, False),
                ('deleted', '교통·국토', '완료', 'sent', True, False),
                ('other-owner', '교통·국토', '완료', 'sent', False, True),
            ]
            for label, department, state, response_state, deleted, other in records:
                owner = self.accounts[1 if other else 0][1]
                row = conn.execute('''INSERT INTO complaints(title,content,category,owner_user_id,complaint_status,deleted_at)
                    VALUES(%s,%s,%s,%s,%s,CASE WHEN %s THEN NOW() ELSE NULL END) RETURNING id''',
                    ('필터 테스트 ' + label, '검증용 원문 ' + label, department, owner, state, deleted)).fetchone()
                self.ids[label] = row['id']
                if response_state:
                    conn.execute('''INSERT INTO complaint_responses(id,complaint_id,author_user_id,department,content,response_state,sent_at)
                        VALUES(%s,%s,%s,%s,%s,%s,CASE WHEN %s THEN NOW() ELSE NULL END)''',
                        (str(uuid.uuid4()), row['id'], self.accounts[0][0], department,
                         '비공개 임시 답변' if response_state == 'draft' else '전송된 공개 답변', response_state, response_state == 'sent'))

    def tearDown(self):
        with connection() as conn:
            conn.execute('DELETE FROM complaints WHERE owner_user_id=ANY(%s::uuid[])', ([owner for _, owner in self.accounts],))
            conn.execute('DELETE FROM app_users WHERE id=ANY(%s::uuid[])', ([identifier for identifier, _ in self.accounts],))
        app.dependency_overrides.clear(); app.dependency_overrides.update(self.original_overrides)
        self.client.close()

    def query(self, departments, state, **extra):
        params = [('departments', department) for department in departments] + [('view_status', state)]
        params.extend(extra.items())
        return self.client.get('/api/complaints', params=params)

    def test_unassigned_ignores_selected_department_but_preserves_ownership(self):
        for department in ('교통·국토', '환경·위생'):
            response = self.query([department], 'unassigned')
            self.assertEqual(response.status_code, 200)
            self.assertEqual({row['id'] for row in response.json()['complaints']}, {self.ids['unassigned'], self.ids['blank']})
            self.assertEqual(response.json()['total'], 2)

    def test_in_progress_includes_assigned_without_final_sent_answer_and_hides_draft(self):
        response = self.query(['교통·국토'], 'in_progress')
        self.assertEqual(response.status_code, 200)
        self.assertEqual({row['id'] for row in response.json()['complaints']},
                         {self.ids['assigned'], self.ids['draft'], self.ids['status-only']})
        draft = next(row for row in response.json()['complaints'] if row['id'] == self.ids['draft'])
        self.assertEqual(draft['latest_response'], '')
        self.assertEqual(draft['latest_response_state'], '')

    def test_completed_multidepartment_pagination_and_no_cancelled_deleted_or_other_owner(self):
        response = self.query(['교통·국토', '환경·위생'], 'completed')
        self.assertEqual(response.status_code, 200)
        self.assertEqual({row['id'] for row in response.json()['complaints']},
                         {self.ids['sent'], self.ids['other-department']})
        self.assertEqual(response.json()['total'], 2)
        page = self.query(['교통·국토', '환경·위생'], 'completed', limit=1, offset=1).json()
        self.assertEqual(len(page['complaints']), 1)
        self.assertEqual(page['total'], 2)

    def test_requires_department_and_single_status_and_rejects_noncanonical_departments(self):
        invalid = [
            [('departments', '교통·국토')], [('view_status', 'completed')],
            [('departments', '가짜 부서'), ('view_status', 'completed')],
            [('departments', '교통·국토'), ('view_status', 'bad')],
            [('departments', '교통·국토'), ('view_status', 'completed'), ('view_status', 'in_progress')],
            [('departments', '교통·국토'), ('view_status', 'completed'), ('view_status', 'completed')],
        ]
        for params in invalid:
            with self.subTest(params=params):
                self.assertEqual(self.client.get('/api/complaints', params=params).status_code, 400)
        self.actor = {**self.actor, 'role': 'admin', 'department': '교통·국토'}
        self.assertEqual(self.query(['교통·국토'], 'completed').status_code, 403)

    def test_existing_no_filter_api_is_preserved(self):
        response = self.client.get('/api/complaints', params={'category': '교통·국토', 'limit': 2})
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(response.json()['complaints']), 2)
        self.assertGreater(response.json()['total'], 2)
