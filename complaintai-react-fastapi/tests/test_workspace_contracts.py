"""DB-free API contract regressions; real PostgreSQL cases live in workflow tests."""
from copy import deepcopy
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app
from app.security import actor_from_auth


class WorkspaceContractTests(unittest.TestCase):
    def setUp(self):
        self.previous = dict(app.dependency_overrides)
        self.actor = {'role': 'admin', 'department': '교통·국토', 'sub': 'admin-test', 'owner_id': 'owner-test'}
        app.dependency_overrides[actor_from_auth] = lambda: self.actor
        self.client = TestClient(app)  # No startup hook or live DB connection.
        self.database = MagicMock()
        self.cursor = self.database.__enter__.return_value.cursor.return_value.__enter__.return_value
        self.connect = patch('app.main.connection', return_value=self.database)
        self.connection_mock = self.connect.start()
        self.password = patch('app.main.check_password')
        self.password_mock = self.password.start()

    def tearDown(self):
        self.password.stop(); self.connect.stop()
        app.dependency_overrides.clear(); app.dependency_overrides.update(self.previous)
        self.client.close()

    def send(self, body=None, status='진행중', already_sent=False):
        sent = {'id': 'draft-test', 'content': (body['content'].strip() if body else '기존 저장 답변'), 'response_state': 'sent'}
        self.cursor.fetchone.side_effect = [{'exists': 1} if already_sent else None, sent]
        with patch('app.main.department_complaint'), patch('app.main.locked_complaint', return_value={'complaint_status': status}):
            return self.client.post('/api/department/complaints/7/responses/send', json=body)

    def archive(self, *, summary='기존 요약', sent=False):
        row = {'id': 7, 'content_fingerprint': None, 'owner_user_id': 'owner-test', 'complaint_status': '취소', 'category': '교통·국토', 'summary': summary}
        restored = {'id': 7, 'complaint_status': '완료' if sent else '접수', 'analysis_state': 'pending' if not summary and not sent else 'completed', 'analysis_revision': 4}
        self.cursor.fetchone.side_effect = [row, {'exists': 1} if sent else None, restored]
        return row, restored

    def test_send_atomically_updates_the_current_editor_text(self):
        result = self.send({'content': '  수정한 최종 답변  '})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['response']['content'], '수정한 최종 답변')
        update = next(call for call in self.cursor.execute.call_args_list if 'UPDATE complaint_responses' in call.args[0])
        self.assertIn('content=COALESCE', update.args[0])
        self.assertEqual(update.args[1], ('수정한 최종 답변', 7, 'admin-test'))
        self.assertIn("response_state='sent'", update.args[0])

    def test_send_without_body_keeps_legacy_stored_draft_compatibility(self):
        self.assertEqual(self.send().status_code, 200)
        update = next(call for call in self.cursor.execute.call_args_list if 'UPDATE complaint_responses' in call.args[0])
        self.assertIsNone(update.args[1][0])

    def test_invalid_final_text_does_not_write_or_send_the_old_draft(self):
        for content in (' ', 'a', 'x' * 5001):
            self.connection_mock.reset_mock()
            self.assertEqual(self.send({'content': content}).status_code, 400)
            self.connection_mock.assert_not_called()

    def test_send_cannot_change_cancelled_completed_or_already_answered_records(self):
        for status in ('취소', '완료'):
            self.cursor.execute.reset_mock()
            self.assertEqual(self.send({'content': '변경 답변'}, status=status).status_code, 409)
            self.assertFalse(any('UPDATE complaint_responses' in call.args[0] for call in self.cursor.execute.call_args_list))
        self.assertEqual(self.send({'content': '변경 답변'}, already_sent=True).status_code, 409)

    def test_cancelled_archive_restore_clears_flags_and_uses_department_scope(self):
        self.archive()
        result = self.client.post('/api/complaints/7/restore')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['complaint']['complaint_status'], '접수')
        first = self.cursor.execute.call_args_list[0]
        self.assertIn('deleted_at IS NOT NULL', first.args[0])
        self.assertIn('FOR UPDATE', first.args[0])
        self.assertEqual(first.args[1], (7, ['교통·국토']))
        update = next(call for call in self.cursor.execute.call_args_list if 'UPDATE complaints' in call.args[0])
        self.assertIn('cancelled_by_role=NULL,cancellation_reason=NULL', update.args[0])
        self.assertEqual(update.args[1], ('접수', 'completed', 7))

    def test_restore_preserves_completion_when_a_sent_response_exists(self):
        self.archive(sent=True)
        result = self.client.post('/api/complaints/7/restore')
        self.assertEqual(result.json()['complaint']['complaint_status'], '완료')
        update = next(call for call in self.cursor.execute.call_args_list if 'UPDATE complaints' in call.args[0])
        self.assertEqual(update.args[1], ('완료', 'completed', 7))

    def test_restore_requeues_incomplete_analysis(self):
        self.archive(summary='')
        with patch('app.main.BackgroundTasks.add_task') as enqueue:
            result = self.client.post('/api/complaints/7/restore')
        self.assertEqual(result.json()['complaint']['analysis_state'], 'pending')
        self.assertEqual(enqueue.call_args.args[1:], (7, 4))

    def test_restore_rejects_a_duplicate_active_complaint(self):
        row, _ = self.archive()
        row = deepcopy(row); row['content_fingerprint'] = 'abc'
        self.cursor.fetchone.side_effect = [row, {'id': 8}]
        self.assertEqual(self.client.post('/api/complaints/7/restore').status_code, 409)
        self.assertFalse(any('UPDATE complaints' in call.args[0] for call in self.cursor.execute.call_args_list))

    def test_individual_permanent_delete_accepts_cancelled_archive_but_not_active_rows(self):
        self.cursor.fetchone.return_value = {'id': 7}
        result = self.client.request('DELETE', '/api/complaints/7/permanent', json={'password': 'test-password'})
        self.assertEqual(result.json()['permanently_deleted'], 1)
        self.password_mock.assert_called_once_with(self.actor, 'test-password')
        statement = self.cursor.execute.call_args.args
        self.assertIn('deleted_at IS NOT NULL', statement[0])
        self.assertNotIn('complaint_status NOT IN', statement[0])
        self.assertEqual(statement[1], (7, ['교통·국토']))
        self.cursor.fetchone.return_value = None
        self.assertEqual(self.client.request('DELETE', '/api/complaints/7/permanent', json={'password': 'test-password'}).status_code, 404)

    def test_archive_management_is_not_available_to_ordinary_users(self):
        self.actor = {'role': 'user', 'owner_id': 'owner-test'}
        self.assertEqual(self.client.post('/api/complaints/7/restore').status_code, 403)
        self.assertEqual(self.client.request('DELETE', '/api/complaints/7/permanent', json={'password': 'test-password'}).status_code, 403)
        self.connection_mock.assert_not_called()
        self.password_mock.assert_not_called()

    def test_incorrect_password_cannot_reach_the_delete_query(self):
        self.password_mock.side_effect = HTTPException(403, '비밀번호가 올바르지 않습니다.')
        self.assertEqual(self.client.request('DELETE', '/api/complaints/7/permanent', json={'password': 'wrong-password'}).status_code, 403)
        self.connection_mock.assert_not_called()

    def test_unavailable_archive_record_is_not_restored(self):
        self.cursor.fetchone.return_value = None
        self.assertEqual(self.client.post('/api/complaints/7/restore').status_code, 404)
        self.assertEqual(self.cursor.execute.call_count, 1)


if __name__ == '__main__':
    unittest.main()
