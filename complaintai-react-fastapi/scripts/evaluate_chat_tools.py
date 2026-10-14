"""Authenticated handler integration with PostgreSQL, real intent and RAG models.

Uses an exact temporary owner only. Normal complaint department classification
is mocked to keep that separate model out of this chatbot integration check.
No app lifespan is started, and no existing recommendation job is recovered.
"""
import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import connection
from app.main import app
from app.security import password_hash


def evaluate(output):
    owner, identifier = str(uuid.uuid4()), str(uuid.uuid4())
    username, password = 'chat-tools-eval-' + uuid.uuid4().hex[:12], uuid.uuid4().hex
    salt, digest = password_hash(password)
    report = {'evaluated_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'Authenticated TestClient handlers, real PostgreSQL and intent/RAG models; normal complaint classification mocked; no browser test',
              'steps': []}

    def record(name, checks, details=None, started=None):
        entry = {'step': name, 'checks': checks, 'passed': all(checks.values()), 'details': details or {}}
        if started is not None:
            entry['seconds'] = round(time.monotonic() - started, 2)
        report['steps'].append(entry)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'step': name, 'passed': entry['passed'], 'seconds': entry.get('seconds')}, ensure_ascii=False), flush=True)
        if not entry['passed']:
            raise AssertionError(name + ' failed')

    with connection() as conn:
        conn.execute('''INSERT INTO app_users
            (id,owner_id,username,email,display_name,password_salt,password_hash,account_role)
            VALUES(%s,%s,%s,%s,%s,%s,%s,'user')''',
            (identifier, owner, username, username + '@test.local', 'Chat tools evaluation', salt, digest))
    client = TestClient(app)
    try:
        login = client.post('/api/auth/login', json={'username': username, 'password': password})
        login.raise_for_status()
        client.headers['Authorization'] = 'Bearer ' + login.json()['token']

        started = time.monotonic()
        print('START real create intent', flush=True)
        route = client.post('/api/ai-recommendations/intent', json={'message': '내가 민원을 작성하고 싶어'})
        route.raise_for_status()
        before = client.get('/api/complaints').json()
        record('natural_create_opens_only_tool', {'intent_create': route.json()['intent'] == 'create',
               'no_complaint_created': before['total'] == 0}, route.json(), started)

        with patch('app.main.classify_submission', new=AsyncMock()):
            created = client.post('/api/complaints', json={'title': '도구 통합 검증 민원', 'content': '검증 계정이 작성한 보도 파손 민원입니다.'})
            created.raise_for_status()
            complaint = created.json()['complaint']
            listed = client.get('/api/complaints').json()
            record('explicit_create_and_own_list', {'created_201': created.status_code == 201,
                   'owned_list_one': listed['total'] == 1 and listed['complaints'][0]['id'] == complaint['id']})

            updated = client.patch(f"/api/complaints/{complaint['id']}", json={'title': '검증 민원 수정', 'content': '검증 계정에서 내용을 수정했습니다.'})
            updated.raise_for_status()
            detail = client.get(f"/api/complaints/{complaint['id']}").json()['complaint']
            record('explicit_edit_preserves_id', {'status_200': updated.status_code == 200,
                   'same_id': detail['id'] == complaint['id'], 'content_updated': detail['content'] == '검증 계정에서 내용을 수정했습니다.'})

            removed = client.request('DELETE', f"/api/complaints/{complaint['id']}", json={'reason': ''})
            removed.raise_for_status()
            listed = client.get('/api/complaints').json()
            with connection() as conn:
                state = conn.execute('SELECT complaint_status,deleted_at FROM complaints WHERE id=%s AND owner_user_id=%s', (complaint['id'], owner)).fetchone()
            record('explicit_delete_cancels_owned_record', {'removed_from_list': listed['total'] == 0,
                   'cancelled': state['complaint_status'] == '취소', 'soft_deleted': state['deleted_at'] is not None})

        print('START real answer intent and RAG', flush=True)
        started = time.monotonic()
        question = '119안심콜은 고령자만 가입할 수 있나요?'
        route = client.post('/api/ai-recommendations/intent', json={'message': question})
        route.raise_for_status()
        record('ordinary_question_uses_answer_route', {'intent_answer': route.json()['intent'] == 'answer'}, route.json(), started)
        started = time.monotonic()
        response = client.post('/api/ai-recommendations', json={'title': '119안심콜 가입 대상', 'content': question})
        response.raise_for_status()
        job = client.get('/api/ai-recommendations/' + response.json()['id']).json()
        result = job.get('result') or {}
        record('existing_rag_real_models', {'accepted_202': response.status_code == 202,
               'completed': job['status'] == 'completed', 'answered': result.get('answer_status') == 'answered',
               'official_source': any(c['source_id'] == 'F06' for c in result.get('citations', []))},
               {'result': result, 'error': job.get('error_message')}, started)
    finally:
        client.close()
        with connection() as conn:
            conn.execute('DELETE FROM ai_recommendations WHERE owner_user_id=%s', (owner,))
            conn.execute('DELETE FROM complaints WHERE owner_user_id=%s', (owner,))
            conn.execute('DELETE FROM app_users WHERE id=%s AND owner_id=%s', (identifier, owner))
    return report


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('../docs/ai-expected-answer/chat-tools-integration-2026-10-13.json'))
    args = parser.parse_args()
    result = evaluate(args.output.resolve())
    if not all(step['passed'] for step in result['steps']):
        raise SystemExit(1)
