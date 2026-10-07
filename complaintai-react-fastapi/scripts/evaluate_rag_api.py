"""Live HTTP evaluation of both citizen flows with real models and temporary own data."""
import argparse
import asyncio
import json
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import connection
from app.security import password_hash


async def main(server, output):
    owner, identifier = str(uuid.uuid4()), str(uuid.uuid4())
    username = 'rag-live-eval-' + uuid.uuid4().hex[:12]
    password = uuid.uuid4().hex
    salt, digest = password_hash(password)
    complaint_ids, results = [], []
    cases = json.loads((Path(__file__).resolve().parents[1] / 'tests/fixtures/rag-evaluation.json').read_text(encoding='utf-8'))
    output.parent.mkdir(parents=True, exist_ok=True)
    with connection() as conn:
        conn.execute('INSERT INTO app_users(id,owner_id,username,email,display_name,password_salt,password_hash,account_role) VALUES(%s,%s,%s,%s,%s,%s,%s,\'user\')', (identifier, owner, username, username + '@test.local', 'RAG live evaluation', salt, digest))
    try:
        async with httpx.AsyncClient(base_url=server, timeout=30) as client:
            login = await client.post('/api/auth/login', json={'username': username, 'password': password})
            login.raise_for_status()
            client.headers['Authorization'] = 'Bearer ' + login.json()['token']
            for case in cases:
                started = time.monotonic()
                existing = case['id'] in ('floor-noise', 'fire')
                if existing:
                    with connection() as conn:
                        complaint = conn.execute('INSERT INTO complaints(title,content,summary,category,owner_user_id) VALUES(%s,%s,\'\',\'기타\',%s) RETURNING id', (case['title'], case['content'], owner)).fetchone()
                    complaint_ids.append(complaint['id'])
                    body = {'complaint_id': complaint['id']}
                else:
                    body = {'title': case['title'], 'content': case['content']}
                print('RUN ' + case['id'] + (' (existing complaint)' if existing else ' (new input)'), flush=True)
                response = await client.post('/api/ai-recommendations', json=body)
                response.raise_for_status()
                job = response.json()
                last_stage = None
                while job['status'] in ('queued', 'processing'):
                    if time.monotonic() - started > 1800:
                        raise TimeoutError('Live recommendation did not finish')
                    response = await client.get('/api/ai-recommendations/' + job['id'])
                    response.raise_for_status(); job = response.json()
                    if job['stage'] != last_stage:
                        print(case['id'] + ': ' + job['stage'], flush=True); last_stage = job['stage']
                    if job['status'] in ('queued', 'processing'):
                        await asyncio.sleep(2)
                answer = job.get('result')
                checks = {}
                if answer:
                    sources = {citation['source_id'] for citation in answer['citations']}
                    combined = answer['answer'] + '\n' + '\n'.join(answer['next_steps'])
                    checks = {
                        'status_matches': answer['answer_status'] == case['expected_status'],
                        'expected_source_found': not case['expected_sources'] or bool(set(case['expected_sources']) & sources),
                        'keywords_present': all(keyword in combined for keyword in case['keywords']),
                        'answered_has_citations': answer['answer_status'] != 'answered' or bool(answer['citations']),
                        'no_known_unsupported_claim': not any(re.search(pattern, combined) for pattern in case.get('forbidden_patterns', [])),
                        'correct_building_scope': case['id'] != 'floor-noise' or all(c.get('applicability', {}).get('building_scope') != 'non_shared_housing' for c in answer['citations']),
                    }
                row = {'case': case, 'flow': 'existing' if existing else 'new', 'http_job_status': job['status'], 'result': answer, 'error': job.get('error_message'), 'automated_checks': checks, 'automated_pass': bool(checks) and all(checks.values()), 'seconds': round(time.monotonic() - started, 2), 'human_review': 'pending'}
                results.append(row)
                output.write_text(json.dumps({'evaluated_at': datetime.now(timezone.utc).isoformat(), 'scope': 'Live HTTP pilot smoke tests; not production accuracy', 'cases': results}, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps({'id': case['id'], 'pass': row['automated_pass'], 'seconds': row['seconds'], 'error': row['error']}, ensure_ascii=False), flush=True)
    finally:
        # Exact temporary owner/account only; no citizen or existing project data is removed.
        with connection() as conn:
            conn.execute('DELETE FROM ai_recommendations WHERE owner_user_id=%s', (owner,))
            conn.execute('DELETE FROM complaints WHERE id=ANY(%s::bigint[]) AND owner_user_id=%s', (complaint_ids, owner))
            conn.execute('DELETE FROM app_users WHERE id=%s AND owner_id=%s', (identifier, owner))
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--server', default='http://127.0.0.1:8000')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    rows = asyncio.run(main(args.server, args.output.resolve()))
    if not all(row['automated_pass'] for row in rows):
        raise SystemExit(1)
