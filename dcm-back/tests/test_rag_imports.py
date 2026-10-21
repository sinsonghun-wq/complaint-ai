"""Reference ingestion uses a disposable DB, never the live RAG corpus."""
import asyncio
import csv
import io
import json
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from fastapi.testclient import TestClient

from app.main import app
from app.db import connection
from app.security import actor_from_auth
from app.settings import DATABASE_URL
from app.routers import rag_imports as routes
from app.services import rag_imports as service
from app.ai import EmbeddingError
from app.utils import util

VECTOR = [1.0] + [0.0] * 1023


class RagImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = 'complaintai_rag_import_test_' + uuid.uuid4().hex
        cls.url = make_conninfo(DATABASE_URL, dbname=cls.name)
        with psycopg.connect(DATABASE_URL, autocommit=True) as db:
            db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(cls.name)))
        fixtures = Path(__file__).with_name('fixtures')
        with psycopg.connect(cls.url) as db:
            db.execute((fixtures / 'init.sql').read_text(encoding='utf8'))
            db.execute((fixtures / 'seed_demo_accounts.sql').read_text(encoding='utf8'))
            # Match the actual doc-main table: department ID, no legacy text column.
            db.execute('DROP TRIGGER sync_department ON department_documents')
            db.execute('ALTER TABLE department_documents DROP COLUMN department')
        cls.db_patch = patch('app.db.DATABASE_URL', cls.url)
        cls.db_patch.start()
        service.ensure_rag_import_schema()
        with connection() as db:
            cls.admin = dict(db.execute("SELECT id,owner_id FROM app_users WHERE username='admin-land-transport'").fetchone(), role='admin', department_id=2)
            cls.other = dict(db.execute("SELECT id,owner_id FROM app_users WHERE username='admin-welfare'").fetchone(), role='admin', department_id=6)
        for actor in (cls.admin, cls.other):
            actor['sub'] = str(actor['id'])
        cls.client = TestClient(app)
        cls.folder = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.client.close(); cls.folder.cleanup(); cls.db_patch.stop()
        assert cls.name.startswith('complaintai_rag_import_test_')
        with psycopg.connect(DATABASE_URL, autocommit=True) as db:
            db.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(cls.name)))

    def setUp(self):
        with connection() as db:
            db.execute('DELETE FROM rag_import_jobs'); db.execute('DELETE FROM department_documents')
        app.dependency_overrides[actor_from_auth] = lambda: self.admin
        self.storage = patch.object(util, 'FILE_STORAGE_DIR', Path(self.folder.name)); self.storage.start()
        self.worker = patch.object(routes, 'start_worker'); self.worker.start()

    def tearDown(self):
        app.dependency_overrides.pop(actor_from_auth, None)
        self.storage.stop(); self.worker.stop()

    def upload(self, rows, headers=('제목', '본문'), filename='rules.csv'):
        source = io.StringIO(newline='')
        writer = csv.writer(source); writer.writerow(headers); writer.writerows(rows)
        result = self.client.post('/api/rag-imports', files={'file': (filename, source.getvalue().encode('utf-8-sig'), 'text/csv')})
        self.assertEqual(result.status_code, 202, result.text)
        return result.json()

    def claim(self, identifier):
        with connection() as db:
            return db.execute("UPDATE rag_import_jobs SET status='processing',stage='counting' WHERE id=%s RETURNING *", (identifier,)).fetchone()

    def state(self, identifier):
        result = self.client.get(f'/api/rag-imports/{identifier}')
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def test_100_row_commits_preserve_content_and_searchable_vectors(self):
        content = '  조문 제0조 원문\n공백과 "인용"을 그대로 보존합니다.  '
        job = self.upload([[f'법령 {i}', content if i == 0 else f'도로 관련 규칙 제{i}조 본문입니다.'] for i in range(205)])
        snapshots = []
        actual_commit = service.commit_batch
        def commit(*args):
            result = actual_commit(*args)
            with connection() as db:
                snapshots.append((db.execute('SELECT count(*) n FROM department_documents').fetchone()['n'],
                                  db.execute('SELECT completed_rows FROM rag_import_jobs WHERE id=%s', (job['id'],)).fetchone()['completed_rows']))
            return result
        with patch.object(service, 'embeddingForSelect', new=AsyncMock(return_value=VECTOR)) as embed, patch.object(service, 'commit_batch', side_effect=commit):
            service.run_job(self.claim(job['id']))
        state = self.state(job['id'])
        self.assertEqual((state['status'], state['saved_rows'], state['completed_rows']), ('completed', 205, 205))
        self.assertEqual(snapshots, [(100, 100), (200, 200), (205, 205)])
        self.assertEqual(embed.await_args_list[0].args[0], content)
        with connection() as db:
            row = db.execute('SELECT content,vector_dims(embedding) dims,department_id,original_name FROM department_documents ORDER BY version LIMIT 1').fetchone()
            self.assertEqual(row, dict(content=content, dims=1024, department_id=2, original_name='rules.csv'))
        from app.agent import aiFAQ
        with patch('app.agent.embeddingForSelect', new=AsyncMock(return_value=VECTOR)):
            retrieved = asyncio.run(aiFAQ.ainvoke({'content': '도로 관련 규칙', 'is_described': True}))
        self.assertTrue(retrieved['result'])

    def test_exact_body_dedup_across_rows_uploads_and_departments(self):
        first = self.upload([['첫 제목', '같은 규칙 본문'], ['다른 제목', '같은 규칙 본문'], ['별개', '같은 규칙 본문 ']])
        with patch.object(service, 'embeddingForSelect', new=AsyncMock(return_value=VECTOR)) as embed:
            service.run_job(self.claim(first['id']))
        self.assertEqual(embed.await_count, 2)
        self.assertEqual((self.state(first['id'])['saved_rows'], self.state(first['id'])['duplicate_rows']), (2, 1))
        second = self.upload([['새 제목', '같은 규칙 본문']])
        with connection() as db:
            db.execute('UPDATE rag_import_jobs SET department_id=4 WHERE id=%s', (second['id'],))
        with patch.object(service, 'embeddingForSelect', new=AsyncMock(return_value=VECTOR)) as embed:
            service.run_job(self.claim(second['id']))
        embed.assert_not_awaited()
        self.assertEqual((self.state(second['id'])['saved_rows'], self.state(second['id'])['duplicate_rows']), (0, 1))

    def test_cancel_after_100_preserves_committed_rows_and_discards_remainder(self):
        job = self.upload([[f'법령 {i}', f'서로 다른 법령 원문 {i}'] for i in range(205)])
        actual_commit = service.commit_batch
        def commit(*args):
            result = actual_commit(*args)
            if args[2] == 100:
                cancelled = self.client.post(f"/api/rag-imports/{job['id']}/cancel")
                self.assertEqual(cancelled.status_code, 200)
                self.assertEqual(cancelled.json()['status'], 'cancelled')
            return result
        with patch.object(service, 'embeddingForSelect', new=AsyncMock(return_value=VECTOR)) as embed, patch.object(service, 'commit_batch', side_effect=commit):
            service.run_job(self.claim(job['id']))
        self.assertEqual(embed.await_count, 100)
        state = self.state(job['id'])
        self.assertEqual((state['status'], state['saved_rows'], state['completed_rows']), ('cancelled', 100, 100))
        with connection() as db:
            self.assertEqual(db.execute('SELECT count(*) n FROM department_documents').fetchone()['n'], 100)
        # Even an already-prepared batch cannot insert after cancellation returns.
        pending = [dict(content='늦은 원문', title='늦은 자료', source_row=102, embedding=VECTOR)]
        self.assertFalse(service.commit_batch(self.claimless_job(job['id']), pending, 101))

    def claimless_job(self, identifier):
        with connection() as db:
            return db.execute('SELECT * FROM rag_import_jobs WHERE id=%s', (identifier,)).fetchone()

    def test_cancel_during_embedding_discards_uncommitted_batch(self):
        job = self.upload([['첫 자료', '첫 원문'], ['다음 자료', '다음 원문']])
        calls = 0
        async def embed(_):
            nonlocal calls
            calls += 1
            if calls == 2:
                await asyncio.to_thread(self.client.post, f"/api/rag-imports/{job['id']}/cancel")
                await asyncio.sleep(0.6)
            return VECTOR
        with patch.object(service, 'embeddingForSelect', side_effect=embed):
            service.run_job(self.claim(job['id']))
        self.assertEqual((self.state(job['id'])['status'], self.state(job['id'])['saved_rows']), ('cancelled', 0))
        with connection() as db:
            self.assertEqual(db.execute('SELECT count(*) n FROM department_documents').fetchone()['n'], 0)

    def test_embedding_failure_never_stores_a_document_without_valid_vector(self):
        for vector in (None, [0.1] * 1536, [0.0] * 1024):
            job = self.upload([['자료', '임베딩이 필요한 규칙 본문']])
            with patch.object(service, 'embeddingForSelect', new=AsyncMock(return_value=vector)):
                service.run_job(self.claim(job['id']))
            self.assertEqual(self.state(job['id'])['status'], 'completed')
            self.assertEqual(self.state(job['id'])['failed_rows'], 1)
            failures = self.client.get(f"/api/rag-imports/{job['id']}/failures").json()
            self.assertIn('임베딩', failures['failures'][0]['reason'])
        with connection() as db:
            self.assertEqual(db.execute('SELECT count(*) n FROM department_documents').fetchone()['n'], 0)

    def test_csv_only_authorization_and_cancellation_ownership(self):
        for filename in ('rules.pdf', 'rules.xlsx', 'rules.hwp'):
            result = self.client.post('/api/rag-imports', files={'file': (filename, b'bad', 'application/octet-stream')})
            self.assertEqual(result.status_code, 400)
            self.assertIn('CSV', result.json()['detail'])
        job = self.upload([['자료', '본문']])
        app.dependency_overrides[actor_from_auth] = lambda: self.other
        self.assertEqual(self.client.post(f"/api/rag-imports/{job['id']}/cancel").status_code, 403)
        self.assertEqual(self.client.get(f"/api/rag-imports/{job['id']}").status_code, 403)
        self.assertIsNone(self.client.get('/api/rag-imports/latest').json()['job'])
        app.dependency_overrides[actor_from_auth] = lambda: dict(self.admin, role='user')
        self.assertEqual(self.client.post('/api/rag-imports', files={'file': ('rules.csv', b'content\nbody', 'text/csv')}).status_code, 403)

    def test_oversized_record_is_excluded_and_later_content_still_saved(self):
        job = self.upload([['초과', '한' * 2_000_001], ['정상', '보존할 정상적인 법령 본문']])
        with patch.object(service, 'embeddingForSelect', new=AsyncMock(return_value=VECTOR)):
            service.run_job(self.claim(job['id']))
        state = self.state(job['id'])
        self.assertEqual((state['status'], state['saved_rows'], state['failed_rows'], state['total_rows']), ('completed', 1, 1, 2))

    def test_question_answer_cases_and_admin_without_assigned_department(self):
        app.dependency_overrides[actor_from_auth] = lambda: dict(self.admin, department_id=None)
        job = self.upload([['주거 사례', '누가 신청하나요?', '임차인이 신청합니다.']], headers=('제목', '질문', '답변'), filename='CASE.CSV')
        self.assertEqual(job['department_id'], 7)
        with patch.object(service, 'embeddingForSelect', new=AsyncMock(return_value=VECTOR)):
            service.run_job(self.claim(job['id']))
        with connection() as db:
            self.assertEqual(db.execute('SELECT content FROM department_documents').fetchone()['content'], '질문: 누가 신청하나요?\n답변: 임차인이 신청합니다.')

    def test_simultaneous_batches_do_not_insert_duplicate_content(self):
        jobs = [self.claim(self.upload([['자료', '동시 삽입 중복 본문']])['id']) for _ in range(2)]
        item = dict(title='자료', content='동시 삽입 중복 본문', source_row=2, embedding=VECTOR)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda job: service.commit_batch(job, [item], 1), jobs))
        self.assertEqual(results, [True, True])
        with connection() as db:
            self.assertEqual(db.execute('SELECT count(*) n FROM department_documents').fetchone()['n'], 1)
            self.assertEqual(db.execute('SELECT sum(saved_rows)::int saved,sum(duplicate_rows)::int duplicates FROM rag_import_jobs').fetchone(), dict(saved=1, duplicates=1))

    def test_row_19_embedding_error_does_not_stop_later_batches(self):
        job = self.upload([[f'규칙 {i}', f'원문 {i}'] for i in range(205)])
        async def embed(content):
            if content == '원문 17':
                raise EmbeddingError('입력이 8,192토큰을 초과했습니다. (HTTP 413)')
            if content == '원문 100':
                raise RuntimeError('서버 응답을 읽지 못했습니다.')
            return VECTOR
        snapshots = []
        actual_commit = service.commit_batch
        def commit(*args):
            result = actual_commit(*args)
            state = self.state(job['id'])
            snapshots.append((state['completed_rows'], state['saved_rows'], state['failed_rows']))
            return result
        with patch.object(service, 'embeddingForSelect', side_effect=embed) as embedding, patch.object(service, 'commit_batch', side_effect=commit):
            service.run_job(self.claim(job['id']))
        self.assertEqual(embedding.await_count, 205)
        state = self.state(job['id'])
        self.assertEqual((state['status'], state['completed_rows'], state['saved_rows'], state['failed_rows']), ('completed', 205, 203, 2))
        self.assertEqual(snapshots, [(100, 99, 1), (200, 198, 2), (205, 203, 2)])
        failures = self.client.get(f"/api/rag-imports/{job['id']}/failures").json()
        self.assertEqual([item['source_row'] for item in failures['failures']], [19, 102])
        self.assertIn('HTTP 413', failures['failures'][0]['reason'])
        self.assertIsNone(state['last_error'])

    def test_failure_details_are_paginated_and_owner_only(self):
        job = self.upload([[f'항목 {i}', ''] for i in range(23)] + [['정상', '저장할 원문']])
        with patch.object(service, 'embeddingForSelect', new=AsyncMock(return_value=VECTOR)):
            service.run_job(self.claim(job['id']))
        self.assertEqual(self.state(job['id'])['saved_rows'], 1)
        result = self.client.get(f"/api/rag-imports/{job['id']}/failures?offset=10&limit=10").json()
        self.assertEqual(result['total'], 23)
        self.assertEqual([item['source_row'] for item in result['failures']], list(range(12, 22)))
        self.assertEqual(self.client.get(f"/api/rag-imports/{job['id']}/failures?limit=101").status_code, 422)
        app.dependency_overrides[actor_from_auth] = lambda: self.other
        self.assertEqual(self.client.get(f"/api/rag-imports/{job['id']}/failures").status_code, 403)

    def test_invalid_db_row_is_isolated_and_same_body_can_still_be_saved(self):
        with connection() as db:
            db.execute("ALTER TABLE department_documents ADD CONSTRAINT rag_test_title CHECK(title <> 'reject-this-title')")
        try:
            job = self.upload([['reject-this-title', '같은 본문'], ['정상 제목', '같은 본문'], ['NUL', '잘못된\x00문자'], ['다음', '계속 저장할 본문']])
            with patch.object(service, 'embeddingForSelect', new=AsyncMock(return_value=VECTOR)):
                service.run_job(self.claim(job['id']))
            state = self.state(job['id'])
            self.assertEqual((state['status'], state['saved_rows'], state['duplicate_rows'], state['failed_rows']), ('completed', 2, 0, 2))
            failures = self.client.get(f"/api/rag-imports/{job['id']}/failures").json()['failures']
            self.assertEqual([item['source_row'] for item in failures], [2, 4])
        finally:
            with connection() as db:
                db.execute('ALTER TABLE department_documents DROP CONSTRAINT rag_test_title')


if __name__ == '__main__':
    unittest.main()
