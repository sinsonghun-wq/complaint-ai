"""Two-turn live HTTP RAG check, using only an isolated temporary test account."""
import argparse
import asyncio
import json
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
    username, password = 'rag-chat-eval-' + uuid.uuid4().hex[:12], uuid.uuid4().hex
    salt, digest = password_hash(password)
    results = []
    with connection() as conn:
        conn.execute('''INSERT INTO app_users
            (id,owner_id,username,email,display_name,password_salt,password_hash,account_role)
            VALUES(%s,%s,%s,%s,%s,%s,%s,'user')''',
            (identifier, owner, username, username + '@test.local', 'RAG chat evaluation', salt, digest))
    try:
        async with httpx.AsyncClient(base_url=server, timeout=30) as client:
            login = await client.post('/api/auth/login', json={'username': username, 'password': password})
            login.raise_for_status()
            client.headers['Authorization'] = 'Bearer ' + login.json()['token']
            parent = None
            for index, content in enumerate(['119안심콜은 고령자만 가입할 수 있나요?', '가입한 다음 연락처가 바뀌면 어떻게 해야 하나요?']):
                body = {'parent_recommendation_id': parent, 'content': content} if parent else {'title': '119안심콜 상담', 'content': content}
                started = time.monotonic()
                response = await client.post('/api/ai-recommendations', json=body)
                response.raise_for_status()
                job, last_stage = response.json(), None
                while job['status'] in ('queued', 'processing'):
                    if time.monotonic() - started > 900:
                        raise TimeoutError('Chat recommendation did not finish')
                    response = await client.get('/api/ai-recommendations/' + job['id'])
                    response.raise_for_status()
                    job = response.json()
                    if job['stage'] != last_stage:
                        print(f"turn {index + 1}: {job['stage']}", flush=True)
                        last_stage = job['stage']
                    if job['status'] in ('queued', 'processing'):
                        await asyncio.sleep(2)
                result = job.get('result') or {}
                text = result.get('answer', '') + '\n' + '\n'.join(result.get('next_steps', []))
                checks = {
                    'job_completed': job['status'] == 'completed',
                    'answered': result.get('answer_status') == 'answered',
                    'official_source': any(c['source_id'] == 'F06' for c in result.get('citations', [])),
                    'parent_link': index == 0 or job.get('parent_recommendation_id') == parent,
                    'followup_updates_contact': index == 0 or ('연락처' in text and '수정' in text),
                }
                row = {'turn': index + 1, 'question': content, 'result': result, 'checks': checks,
                       'seconds': round(time.monotonic() - started, 2), 'error': job.get('error_message')}
                results.append(row)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps({'evaluated_at': datetime.now(timezone.utc).isoformat(),
                    'scope': 'Two-turn live HTTP smoke check, not production accuracy',
                    'turns': results}, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps({'turn': index + 1, 'checks': checks, 'seconds': row['seconds']}, ensure_ascii=False), flush=True)
                if job['status'] != 'completed':
                    break
                parent = job['id']
    finally:
        with connection() as conn:
            conn.execute('DELETE FROM ai_recommendations WHERE owner_user_id=%s', (owner,))
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
    if len(rows) != 2 or not all(all(row['checks'].values()) for row in rows):
        raise SystemExit(1)
