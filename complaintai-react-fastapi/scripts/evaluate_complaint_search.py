"""Authenticated owned-complaint search with real PostgreSQL and local Qwen.

Only synthetic complaints for fresh, exact temporary owners are inserted or
removed. TestClient never enters the app lifespan or recovers existing jobs.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
import uuid

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import settings
from app.complaint_search import SEARCH_PROMPT_VERSION
from app.db import connection
from app.main import app
from app.security import password_hash

DOCUMENTS = [
    ('home-noise', '공동주택 소음 조정 요청', '윗집에서 심야에 뛰는 소리와 가구를 끄는 소리가 계속 들립니다. 이웃 간 분쟁 조정을 신청합니다.', '주택·건축'),
    ('street-noise', '심야 도로 소음', '오토바이가 밤에 큰 배기음을 내며 주택가 도로를 다닙니다. 단속을 요청합니다.', '환경·위생'),
    ('road', '도로 노면 보수 요청', '출퇴근 도로의 아스팔트가 움푹 패였습니다. 차량이 흔들리고 파손될 위험이 있어 보수를 요청합니다.', '교통·국토'),
    ('lamp', '보행로 조명 고장', '횡단보도 옆 조명등이 꺼져 야간 보행이 어렵습니다. 가로등 수리를 요청합니다.', '교통·국토'),
    ('wage', '미지급 급여 상담', '식당에서 일하다 퇴사했는데 마지막 달의 급여를 지급받지 못했습니다. 임금 체불 해결을 요청합니다.', '노동·기업'),
    ('vaccine', '예방접종 지원 문의', '보건소의 예방접종 비용 감면 대상과 신청 서류를 안내해 주세요.', '보건·복지'),
    ('tree', '가로수 가지 정비 요청', '교차로 가로수의 가지가 신호등을 가려 운전자가 신호를 확인하기 어렵습니다. 가지치기를 요청합니다.', '교통·국토'),
]
CASES = [
    ('paraphrase-noise', '윗집에서 밤마다 발 구르는 소리 때문에 낸 민원을 찾고 싶어요.', {'home-noise'}, {'road', 'lamp', 'wage', 'vaccine', 'tree'}),
    ('paraphrase-road', '출퇴근길에 바퀴가 구덩이에 빠져 차가 덜컹거리던 문제로 작성한 것', {'road'}, {'home-noise', 'street-noise', 'lamp', 'wage', 'vaccine', 'tree'}),
    ('paraphrase-wage', '알바를 그만뒀는데 마지막 월급을 아직 못 받은 민원', {'wage'}, {'home-noise', 'street-noise', 'road', 'lamp', 'vaccine', 'tree'}),
    ('paraphrase-health', '예방주사 비용을 할인받을 수 있는지 물어봤던 것', {'vaccine'}, {'home-noise', 'street-noise', 'road', 'lamp', 'wage', 'tree'}),
    ('meaning-disambiguation', '나뭇가지 때문에 교차로 신호가 안 보여 정비를 요청한 민원', {'tree'}, {'wage', 'vaccine', 'home-noise', 'street-noise', 'road'}),
    ('no-match', '승마 강습 예약을 취소했는데 환불이 안 된 사건', set(), {'home-noise', 'street-noise', 'road', 'lamp', 'wage', 'vaccine', 'tree'}),
]


def evaluate(output: Path):
    accounts = [(str(uuid.uuid4()), str(uuid.uuid4())) for _ in range(2)]
    username, password = 'lookup-eval-' + uuid.uuid4().hex[:12], uuid.uuid4().hex
    salt, digest = password_hash(password)
    report = {'evaluated_at': datetime.now(timezone.utc).isoformat(), 'model': settings.RAG_LLM_MODEL,
              'prompt_version': SEARCH_PROMPT_VERSION,
              'similarity_review_model': 'Qwen/Qwen3-Reranker-0.6B',
              'similarity_min_relevance': settings.COMPLAINT_SEARCH_MIN_RELEVANCE,
              'scope': 'Authenticated API handlers, actual PostgreSQL and local Qwen; synthetic temporary owners only; no browser; no training',
              'steps': []}
    ids = {}

    def save(name, checks, details=None, started=None):
        step = {'name': name, 'checks': checks, 'passed': all(checks.values()), 'details': details or {}}
        if started is not None:
            step['seconds'] = round(time.monotonic() - started, 2)
        report['steps'].append(step)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'step': name, 'passed': step['passed'], 'seconds': step.get('seconds')}, ensure_ascii=False), flush=True)

    client = TestClient(app)
    try:
        with connection() as conn:
            for index, (identifier, owner) in enumerate(accounts):
                conn.execute('''INSERT INTO app_users(id,owner_id,username,email,display_name,password_salt,password_hash,account_role)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,'user')''',
                    (identifier, owner, username + str(index), username + str(index) + '@test.local', '검색 검증', salt, digest))
            for label, title, content, department in DOCUMENTS:
                row = conn.execute('''INSERT INTO complaints(title,content,category,owner_user_id,complaint_status)
                    VALUES(%s,%s,%s,%s,'접수') RETURNING id''', (title, content, department, accounts[0][1])).fetchone()
                ids[label] = row['id']
            for label, owner, state, deleted in [('other-owner', accounts[1][1], '접수', False),
                                                  ('cancelled', accounts[0][1], '취소', False),
                                                  ('deleted', accounts[0][1], '접수', True)]:
                row = conn.execute('''INSERT INTO complaints(title,content,category,owner_user_id,complaint_status,deleted_at)
                    VALUES('검색에 노출되면 안 되는 미지급 급여','퇴사한 뒤 월급을 못 받았습니다.','노동·기업',%s,%s,
                    CASE WHEN %s THEN NOW() ELSE NULL END) RETURNING id''', (owner, state, deleted)).fetchone()
                ids[label] = row['id']
            conn.execute('''INSERT INTO complaint_responses(id,complaint_id,author_user_id,department,content,response_state)
                VALUES(%s,%s,%s,'노동·기업','비공개 임시 처리 검토 내용','draft')''',
                (str(uuid.uuid4()), ids['wage'], accounts[0][0]))
        login = client.post('/api/auth/login', json={'username': username + '0', 'password': password})
        login.raise_for_status()
        client.headers['Authorization'] = 'Bearer ' + login.json()['token']
        forbidden = {ids[key] for key in ('other-owner', 'cancelled', 'deleted')}
        for name, query, expected, irrelevant in CASES:
            print('START ' + name, flush=True)
            started = time.monotonic()
            response = client.post('/api/ai-recommendations/complaint-search', json={'query': query})
            if response.status_code != 200:
                save(name, {'search_available': False}, {'query': query, 'http_status': response.status_code,
                     'error': response.json().get('detail', '검색 요청 실패')}, started)
                continue
            response.raise_for_status()
            body = response.json()
            direct = {row['id'] for row in body['complaints']}
            similar = {row['id'] for row in body['similar_complaints']}
            returned = body['complaints'] + body['similar_complaints']
            save(name, {'expected_direct': {ids[key] for key in expected} <= direct,
                        'unrelated_not_returned': not ((direct | similar) & {ids[key] for key in irrelevant}),
                        'private_active_only': not ((direct | similar) & forbidden),
                        'no_duplicates_between_groups': not (direct & similar),
                        'similarity_scores_reviewed': all(row.get('search_relevance', -1) >= settings.COMPLAINT_SEARCH_MIN_RELEVANCE for row in body['similar_complaints']),
                        'draft_not_disclosed': all(row.get('latest_response_state') != 'draft' and '비공개 임시' not in row.get('latest_response', '') for row in returned),
                        'totals_correct': body['total'] == len(direct) and body['similar_total'] == len(similar)},
                 {'query': query, 'direct': [key for key, identifier in ids.items() if identifier in direct],
                  'similar': [key for key, identifier in ids.items() if identifier in similar]}, started)
        detail = client.get(f"/api/complaints/{ids['wage']}")
        save('same_owned_detail_actions', {'detail_access': detail.status_code == 200,
             'editable_state': detail.json()['complaint']['complaint_status'] == '접수',
             'foreign_detail_denied': client.get(f"/api/complaints/{ids['other-owner']}").status_code == 404})
    finally:
        client.close()
        with connection() as conn:
            owners = [owner for _, owner in accounts]
            conn.execute('DELETE FROM complaints WHERE owner_user_id=ANY(%s::uuid[])', (owners,))
            conn.execute('DELETE FROM app_users WHERE id=ANY(%s::uuid[]) AND owner_id=ANY(%s::uuid[])',
                         ([identifier for identifier, _ in accounts], owners))
            remaining = conn.execute('SELECT count(*) AS count FROM complaints WHERE owner_user_id=ANY(%s::uuid[])', (owners,)).fetchone()['count']
        save('temporary_data_cleanup', {'temporary_complaints_removed': remaining == 0})
    return all(step['passed'] for step in report['steps'])


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('../docs/ai-expected-answer/complaint-search-evaluation-2026-10-14.json'))
    args = parser.parse_args()
    raise SystemExit(0 if evaluate(args.output.resolve()) else 1)
