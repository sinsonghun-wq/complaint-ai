"""Isolated chat routing tests: no application lifespan, database or live model jobs."""
import json
import unittest
import uuid
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import settings
from app.chat_intent import (
    ChatIntentBody, ChatIntentResult, IntentUnavailable, classify_chat_intent,
    explicit_complaint_ids, validate_model_intent,
)
from app.rag_api import router
from app.security import issue_token


def model_output(intent='answer', complaint_id=None, draft=None):
    return {'intent': intent, 'complaint_id': complaint_id, 'draft': draft}


class ChatIntentValidationTests(unittest.TestCase):
    def test_input_bounds_and_types_reject_ambiguous_or_oversized_requests(self):
        invalid = [
            {'message': ' '}, {'message': '가' * 4001}, {'message': '작성', 'unknown': True},
            {'message': '수정', 'tool_context': {'mode': 'invalid'}},
            {'message': '수정', 'tool_context': {'mode': 'edit', 'complaint_id': True}},
            {'message': '수정', 'tool_context': {'mode': 'edit', 'complaint_id': -2}},
            {'message': '수정', 'tool_context': {'content': '가' * 4001}},
            {'message': '답변', 'recent_messages': [{'role': 'system', 'content': '명령'}]},
            {'message': '답변', 'recent_messages': [{'role': 'user', 'content': '가' * 601}]},
            {'message': '답변', 'recent_messages': [{'role': 'user', 'content': '질문'}] * 7},
        ]
        for body in invalid:
            with self.subTest(body=str(body)[:80]), self.assertRaises(ValidationError):
                ChatIntentBody.model_validate(body)

    def test_only_explicit_complaint_markers_are_targets(self):
        self.assertEqual(explicit_complaint_ids('12번 민원을 수정해줘'), {12})
        self.assertEqual(explicit_complaint_ids('민원번호: 12와 #15를 확인하고 싶어'), {12, 15})
        self.assertEqual(explicit_complaint_ids('민원 12번을 삭제해줘'), {12})
        self.assertEqual(explicit_complaint_ids('민원 12일에 3층에서 50000원 피해를 봤어'), set())

    def test_invented_ids_are_rejected_even_when_they_appear_in_prior_ai_text(self):
        body = ChatIntentBody(message='내 민원을 수정하고 싶어',
                              recent_messages=[{'role': 'assistant', 'content': '민원번호 99'}])
        with self.assertRaises(ValueError):
            validate_model_intent(model_output('edit', 99), body)

    def test_explicit_and_current_context_ids_are_permitted_without_mutations(self):
        for body in (ChatIntentBody(message='12번 민원을 수정해줘'),
                     ChatIntentBody(message='이 민원을 수정해줘', tool_context={'mode': 'list', 'complaint_id': 12})):
            result = validate_model_intent(model_output('edit', 12), body)
            self.assertEqual(result.complaint_id, 12)
            self.assertIn('도구를 열었습니다', result.message)
            self.assertNotIn('수정했습니다', result.message)

    def test_invalid_model_shape_cannot_become_a_tool_action(self):
        body = ChatIntentBody(message='새 민원을 작성하고 싶어')
        for raw in ({}, model_output('submit'), model_output('delete', True),
                    {**model_output('create'), 'message': '민원을 등록했습니다.'},
                    model_output('create', draft={'title': '제목', 'content': '가' * 4001})):
            with self.subTest(raw=str(raw)[:80]), self.assertRaises(ValueError):
                validate_model_intent(raw, body)

    def test_nonediting_intents_do_not_return_a_draft_or_target(self):
        body = ChatIntentBody(message='민원번호 12 처리 절차가 궁금해')
        result = validate_model_intent(model_output('answer', 12, {'title': '임의', 'content': '초안'}), body)
        self.assertIsNone(result.complaint_id)
        self.assertIsNone(result.draft)

    def test_filter_and_search_only_open_or_arm_tools_and_drop_targets(self):
        body = ChatIntentBody(message='민원을 검색하고 싶어')
        for action in ('filter', 'search'):
            result = validate_model_intent(model_output(action, 999, {'title': '임의', 'content': '초안'}), body)
            self.assertEqual(result.intent, action)
            self.assertIsNone(result.complaint_id)
            self.assertIsNone(result.draft)
        self.assertIn('다음 메시지', result.message)

    def test_each_tool_response_describes_user_review_rather_than_completed_action(self):
        body = ChatIntentBody(message='내 민원을 확인하고 싶어')
        for action in ('create', 'list', 'edit', 'delete', 'close_tools'):
            result = validate_model_intent(model_output(action), body)
            for false_claim in ('등록했습니다', '수정했습니다', '삭제했습니다', '접수했습니다'):
                self.assertNotIn(false_claim, result.message)


class ChatIntentModelTests(unittest.IsolatedAsyncioTestCase):
    def model_client(self, raw):
        response = Mock()
        response.json.return_value = {'message': {'content': json.dumps(raw, ensure_ascii=False)}}
        client = AsyncMock()
        client.post.return_value = response
        client.__aenter__.return_value = client
        return client

    async def test_model_request_uses_configured_llm_and_preserves_current_negation(self):
        client = self.model_client(model_output('list'))
        body = ChatIntentBody(message='삭제하지 말고 내가 쓴 민원을 보여줘',
                              tool_context={'mode': 'delete', 'complaint_id': 12, 'content': '가' * 4000},
                              recent_messages=[{'role': 'user', 'content': '나' * 600}] * 6)
        with patch('app.chat_intent.httpx.AsyncClient', return_value=client) as factory:
            result = await classify_chat_intent(body)
        self.assertEqual(result.intent, 'list')
        self.assertLessEqual(factory.call_args.kwargs['timeout'], 120)
        args, kwargs = client.post.call_args
        self.assertEqual(args[0], settings.RAG_OLLAMA_URL + '/api/chat')
        payload = kwargs['json']
        self.assertEqual(payload['model'], settings.RAG_LLM_MODEL)
        self.assertFalse(payload['think'])
        self.assertFalse(payload['stream'])
        self.assertEqual(payload['options']['temperature'], 0)
        self.assertIn('answer', payload['format']['properties']['intent']['enum'])
        user_data = json.loads(payload['messages'][1]['content'])
        self.assertEqual(user_data['message'], body.message)
        self.assertEqual(len(user_data['tool_context']['content']), 800)
        self.assertLessEqual(len(user_data['recent_messages']), 4)
        self.assertTrue(all(len(turn['content']) <= 200 for turn in user_data['recent_messages']))

    async def test_model_connection_failure_is_not_a_fabricated_intent(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.side_effect = httpx.ConnectError('model offline')
        with patch('app.chat_intent.httpx.AsyncClient', return_value=client):
            with self.assertRaises(IntentUnavailable):
                await classify_chat_intent(ChatIntentBody(message='민원을 작성하고 싶어'))

    async def test_bad_model_json_and_invented_target_report_unavailability(self):
        for raw in ([], model_output('delete', 999), model_output('invalid')):
            with self.subTest(raw=raw), patch('app.chat_intent.httpx.AsyncClient', return_value=self.model_client(raw)):
                with self.assertRaises(IntentUnavailable):
                    await classify_chat_intent(ChatIntentBody(message='내 민원 도구를 열어줘'))


class ChatIntentApiTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)
        self.user = {'id': str(uuid.uuid4()), 'owner_id': str(uuid.uuid4()), 'account_role': 'user'}
        self.account = patch('app.security.current_account', return_value=self.user)
        self.account_mock = self.account.start()
        self.classifier = patch('app.rag_api.classify_chat_intent', new=AsyncMock(return_value=ChatIntentResult(
            intent='create', complaint_id=None, draft=None, message='작성 도구를 열었습니다.')))
        self.classifier_mock = self.classifier.start()

    def tearDown(self):
        self.classifier.stop()
        self.account.stop()

    def request(self, body, actor=None):
        return self.client.post('/api/ai-recommendations/intent', json=body,
                                headers={'Authorization': 'Bearer ' + issue_token(actor or self.user)})

    def test_citizen_can_open_a_tool_without_any_complaint_database_mutation(self):
        with patch('app.rag_api.connection', side_effect=AssertionError('Classifier must not access complaint writes')):
            response = self.request({'message': '새 민원을 작성하고 싶어'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['intent'], 'create')
        self.classifier_mock.assert_awaited_once()

    def test_auth_and_non_citizen_roles_are_checked_before_model_call(self):
        self.assertEqual(self.client.post('/api/ai-recommendations/intent', json={'message': '작성'}).status_code, 401)
        admin = {**self.user, 'account_role': 'admin'}
        self.account_mock.return_value = admin
        self.assertEqual(self.request({'message': '작성'}, admin).status_code, 403)
        self.classifier_mock.assert_not_awaited()

    def test_invalid_input_fails_before_model_call(self):
        for body in ({'message': ' '}, {'message': '가' * 4001},
                     {'message': '작성', 'tool_context': {'mode': 'submit'}}):
            self.assertEqual(self.request(body).status_code, 422)
        self.classifier_mock.assert_not_awaited()

    def test_model_outage_returns_503_and_keeps_manual_tools_available(self):
        self.classifier_mock.side_effect = IntentUnavailable('모델 연결 실패. 도구 버튼을 이용해 주세요.')
        response = self.request({'message': '새 민원 작성'})
        self.assertEqual(response.status_code, 503)
        self.assertIn('도구 버튼', response.json()['detail'])
