"""Real tokenizer + disposable PostgreSQL tests; embeddings are isolated stubs."""
import asyncio
import csv
import io
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from fastapi.testclient import TestClient

from app.db import connection
from app.main import app
from app.routers import rag_imports as routes
from app.security import actor_from_auth
from app.settings import DATABASE_URL
from app.services import rag_imports as importer, rag_long_imports as long, rag_retrieval as retrieval
from app.services.rag_splitting import document_structure, split_document, token_count
from app.utils import util

VECTOR = [1.0] + [0.0] * 1023


def law(suffix=''):
    return ('제1조(원칙)\n차량의 통행을 허용한다.\n'
            '제2조(세부기준)\n' + '도로의 관리 기준을 정한다. ' * 1800 +
            '\n제3조(예외)\n제1조에도 불구하고 긴급차량은 제외한다. 별표 2를 따른다.\n'
            '[별표 2]\n제외 차량의 세부 목록이다.\n' + suffix)


class SplittingTests(unittest.TestCase):
    def test_original_spans_are_lossless_and_each_input_fits(self):
        original = '  ' + law() + '  \n'
        self.assertGreater(token_count(original), 8192)
        chunks = list(split_document(original))
        self.assertEqual(''.join(chunk['content'] for chunk in chunks), original)
        self.assertEqual(chunks[0]['start_offset'], 0)
        self.assertEqual(chunks[-1]['end_offset'], len(original))
        for index, chunk in enumerate(chunks):
            self.assertEqual(original[chunk['start_offset']:chunk['end_offset']], chunk['content'])
            self.assertLessEqual(token_count(chunk['content']), 8192)
            if index: self.assertEqual(chunks[index-1]['end_offset'], chunk['start_offset'])

    def test_unstructured_text_and_unicode_keep_all_original_characters(self):
        original = ('한글🙂 공백\t보존한다. ' * 4000) + '\n  '
        chunks = list(split_document(original))
        self.assertGreater(len(chunks), 2)
        self.assertEqual(''.join(row['content'] for row in chunks), original)
        self.assertTrue(all(0 < token_count(row['content']) <= 8192 for row in chunks))

    def test_external_law_reference_is_not_an_internal_reference(self):
        units = document_structure('제1조(준용)\n「다른 법률」 제3조를 따른다.\n제3조(별개)\n다른 내용이다.')
        self.assertEqual(units[0]['external'], [{'law':'다른 법률','key':'제3조'}])
        self.assertNotIn('제3조', units[0]['references'])

    def test_article_reference_at_start_of_paragraph_is_not_a_heading(self):
        self.assertEqual([row['key'] for row in document_structure(law())],['제1조','제2조','제3조','별표2'])

    def test_inline_statute_without_newlines_keeps_real_article_boundaries(self):
        text='제1조(원칙) 통행을 허용한다. 제2조(예외) 제1조에도 불구하고 긴급차량은 제외한다. [별표 2] 제외 목록이다.'
        units=document_structure(text)
        self.assertEqual([row['key'] for row in units],['제1조','제2조','별표2'])
        self.assertEqual(''.join(text[row['start']:row['end']] for row in units),text)
        self.assertIn('제1조',units[1]['references'])


class LongImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = 'complaintai_long_rag_test_' + uuid.uuid4().hex
        cls.url = make_conninfo(DATABASE_URL, dbname=cls.name)
        with psycopg.connect(DATABASE_URL, autocommit=True) as db:
            db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(cls.name)))
        cls.addClassCleanup(cls.drop_database)
        fixtures = Path(__file__).with_name('fixtures')
        with psycopg.connect(cls.url) as db:
            db.execute((fixtures/'init.sql').read_text(encoding='utf-8'))
            db.execute((fixtures/'seed_demo_accounts.sql').read_text(encoding='utf-8'))
        db_patch = patch('app.db.DATABASE_URL', cls.url); db_patch.start(); cls.addClassCleanup(db_patch.stop)
        importer.ensure_rag_import_schema()
        with connection() as db:
            cls.admin = dict(db.execute("SELECT id,owner_id FROM app_users WHERE username='admin-land-transport'").fetchone(), role='admin',department_id=2)
        cls.admin['sub'] = str(cls.admin['id'])
        cls.client = TestClient(app); cls.addClassCleanup(cls.client.close)

    @classmethod
    def drop_database(cls):
        assert cls.name.startswith('complaintai_long_rag_test_')
        assert cls.name != conninfo_to_dict(DATABASE_URL).get('dbname')
        with psycopg.connect(DATABASE_URL, autocommit=True) as db:
            db.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(cls.name)))

    def setUp(self):
        with connection() as db:
            db.execute('DELETE FROM department_documents'); db.execute('DELETE FROM rag_import_jobs')
        app.dependency_overrides[actor_from_auth] = lambda:self.admin
        self.addCleanup(app.dependency_overrides.pop, actor_from_auth, None)
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        storage = patch.object(util,'FILE_STORAGE_DIR',Path(folder.name)); storage.start(); self.addCleanup(storage.stop)
        worker = patch.object(routes,'start_worker'); worker.start(); self.addCleanup(worker.stop)

    def upload(self, rows, headers=('제목','본문')):
        source = io.StringIO(newline=''); writer = csv.writer(source); writer.writerow(headers); writer.writerows(rows)
        response = self.client.post('/api/rag-imports',files={'file':('rules.csv',source.getvalue().encode('utf-8-sig'),'text/csv')})
        self.assertEqual(response.status_code,202,response.text)
        return response.json()

    def claim(self, job):
        with connection() as db:
            return db.execute("UPDATE rag_import_jobs SET status='processing',stage=CASE WHEN stage='chunking' THEN stage ELSE 'counting' END WHERE id=%s RETURNING *",(job['id'],)).fetchone()

    def state(self, job):
        return self.client.get(f"/api/rag-imports/{job['id']}").json()

    def run_job(self, job, embed=None):
        with patch.object(importer,'embeddingForSelect',new=AsyncMock(side_effect=embed,return_value=VECTOR)) as mock:
            importer.run_job(self.claim(job))
        return mock

    def test_short_rows_finish_before_any_long_embedding_and_original_is_preserved(self):
        original = law()
        job = self.upload([['앞','짧은 원문 앞'],['긴 법령',original],['뒤','짧은 원문 뒤']])
        calls=[]
        async def embed(content):
            calls.append(content)
            self.assertNotEqual(content,original)
            if content not in ('짧은 원문 앞','짧은 원문 뒤'):
                with connection() as db:
                    self.assertEqual(db.execute('SELECT count(*) AS n FROM department_documents WHERE embedding IS NOT NULL').fetchone()['n'],2)
            return VECTOR
        self.run_job(job,embed)
        self.assertEqual(calls[:2],['짧은 원문 앞','짧은 원문 뒤'])
        state=self.state(job)
        self.assertEqual((state['saved_rows'],state['completed_rows'],state['deferred_rows'],state['long_processed_rows']),(3,3,1,1))
        with connection() as db:
            parent=db.execute("SELECT p.*,s.status FROM department_documents p JOIN department_documents_chunk_state s ON s.parent_id=p.id").fetchone()
            pieces=db.execute('SELECT content,vector_dims(embedding) AS dimensions FROM department_documents_chunks ORDER BY chunk_index').fetchall()
        self.assertEqual(parent['content'],original);self.assertIsNone(parent['embedding']);self.assertEqual(parent['status'],'ready')
        self.assertEqual(''.join(piece['content'] for piece in pieces),original)
        self.assertTrue(all(piece['dimensions']==1024 for piece in pieces))

    def test_cancel_keeps_checkpoint_and_resume_does_not_reembed_saved_chunks(self):
        job=self.upload([['법령',law()]])
        save=long.save_chunk; cancelled=[]
        def checkpoint(*args):
            result=save(*args)
            if not cancelled:
                cancelled.append(True)
                self.assertEqual(self.client.post(f"/api/rag-imports/{job['id']}/cancel").status_code,200)
            return result
        with patch.object(long,'save_chunk',side_effect=checkpoint): self.run_job(job)
        self.assertEqual(self.state(job)['status'],'cancelled')
        self.assertEqual(asyncio.run(retrieval.retrieve(VECTOR)),[])
        with connection() as db:
            saved=db.execute('SELECT count(*) AS n FROM department_documents_chunks WHERE embedding IS NOT NULL').fetchone()['n']
            total=db.execute('SELECT count(*) AS n FROM department_documents_chunks').fetchone()['n']
            path=db.execute('SELECT storage_path FROM rag_import_jobs WHERE id=%s',(job['id'],)).fetchone()['storage_path']
        self.assertEqual(saved,1);Path(path).unlink()
        response=self.client.post(f"/api/rag-imports/{job['id']}/resume")
        self.assertEqual(response.status_code,200,response.text)
        embed=self.run_job(job)
        self.assertEqual(embed.await_count,total-1)
        self.assertEqual((self.state(job)['saved_rows'],self.state(job)['completed_rows']),(1,1))

    def test_failed_long_source_can_resume_without_losing_successful_vectors(self):
        job=self.upload([['법령',law()],['짧은 자료','정상 짧은 본문']])
        async def embed(content):
            if '제3조(예외)' in content: raise RuntimeError('테스트 서버 오류')
            return VECTOR
        self.run_job(job,embed)
        state=self.state(job)
        self.assertEqual((state['status'],state['saved_rows'],state['failed_rows'],state['resumable_rows']),('completed',1,1,1))
        self.assertEqual(self.client.post(f"/api/rag-imports/{job['id']}/resume").status_code,200)
        self.run_job(job)
        state=self.state(job)
        self.assertEqual((state['saved_rows'],state['failed_rows'],state['completed_rows'],state['resumable_rows']),(2,0,2,0))

    def test_new_short_job_preempts_long_work_at_a_chunk_checkpoint(self):
        job=self.upload([['법령',law()]])
        following=[]
        save=long.save_chunk
        def checkpoint(*args):
            result=save(*args)
            if not following: following.append(self.upload([['다른 파일','다른 파일의 짧은 본문']]))
            return result
        with patch.object(long,'save_chunk',side_effect=checkpoint): self.run_job(job)
        self.assertEqual((self.state(job)['status'],self.state(job)['stage']),('queued','chunking'))
        self.run_job(following[0]);self.run_job(job)
        self.assertEqual(self.state(job)['status'],'completed')

    def test_far_exception_and_annex_are_linked_only_within_the_same_original(self):
        job=self.upload([['법령 A',law()],['법령 B',law('법령 B에만 있는 내용')]])
        self.run_job(job)
        with connection() as db:
            parent=db.execute("SELECT id FROM department_documents WHERE title='법령 A'").fetchone()['id']
            seed=db.execute('SELECT start_offset,end_offset FROM department_documents_chunks WHERE parent_id=%s ORDER BY chunk_index LIMIT 1',(parent,)).fetchone()
        context=retrieval.source_context(parent,[seed])
        self.assertTrue(context['context_complete'],context)
        self.assertIn('긴급차량은 제외',context['content']);self.assertIn('제외 차량의 세부 목록',context['content'])
        self.assertNotIn('법령 B에만 있는 내용',context['content']);self.assertNotIn('도로의 관리 기준',context['content'])

    def test_missing_reference_is_explicit_and_does_not_publish_partial_legal_rules(self):
        original=law().replace('차량의 통행을 허용한다.','제999조에 따라 차량의 통행을 허용한다.')
        job=self.upload([['법령',original]]);self.run_job(job)
        with connection() as db:
            seed=db.execute('SELECT parent_id,start_offset,end_offset FROM department_documents_chunks ORDER BY chunk_index LIMIT 1').fetchone()
        context=retrieval.source_context(seed['parent_id'],[seed])
        self.assertFalse(context['context_complete']);self.assertIn('제999조',context['content'])
        self.assertNotIn('차량의 통행을 허용한다',context['content'])

    def test_versions_follow_supplied_effective_dates_without_mixing(self):
        job=self.upload([['같은 법령',law('구 개정본'),'2024-01-01','2024'],['같은 법령',law('새 개정본'),'2026-01-01','2026']],
                        headers=('제목','본문','시행일','개정버전'))
        self.run_job(job)
        with connection() as db:
            candidates=db.execute('SELECT s.parent_id,p.title,s.metadata FROM department_documents_chunk_state s JOIN department_documents p ON p.id=s.parent_id ORDER BY s.source_row').fetchall()
        from datetime import date
        self.assertEqual([retrieval.version_eligible(row,date(2025,1,1))[0] for row in candidates],[True,False])
        self.assertEqual([retrieval.version_eligible(row,date(2026,10,21))[0] for row in candidates],[False,True])

    def test_external_law_lookup_is_explicit_and_keeps_its_source_identity(self):
        original=law().replace('차량의 통행을 허용한다.','「참조 법률」 제3조를 따른다.')
        target=law().replace('긴급차량은 제외한다.','참조 법률의 긴급차량은 제외한다.')
        job=self.upload([['원문 법률',original],['참조 법률',target]])
        self.run_job(job)
        with connection() as db:
            source=db.execute("SELECT id FROM department_documents WHERE title='원문 법률'").fetchone()['id']
            target_id=db.execute("SELECT id FROM department_documents WHERE title='참조 법률'").fetchone()['id']
            seed=db.execute('SELECT start_offset,end_offset FROM department_documents_chunks WHERE parent_id=%s ORDER BY chunk_index LIMIT 1',(source,)).fetchone()
        result=retrieval.source_context(source,[seed])
        self.assertTrue(result['context_complete'],result)
        self.assertIn(str(target_id),result['content']);self.assertIn('참조 법률의 긴급차량',result['content'])
        self.assertIn('명시적으로 참조된 다른 법령의 근거',result['content'])

    def test_version_filter_runs_before_vector_candidate_limit(self):
        old=law()+''.join(f'\n제{10+i}조(추가 기준)\n도로의 관리 기준이다.' for i in range(30))
        job=self.upload([['같은 법령',old,'2024-01-01','2024'],['같은 법령',law('최신 개정'),'2026-01-01','2026']],headers=('제목','본문','시행일','개정버전'))
        self.run_job(job)
        with connection() as db:
            latest=db.execute("SELECT parent_id FROM department_documents_chunk_state WHERE metadata->>'law_version'='2026'").fetchone()['parent_id']
        from datetime import date
        _,chunks,_=retrieval.lookup(VECTOR,date(2026,10,21))
        self.assertTrue(chunks)
        self.assertTrue(all(row['parent_id']==latest for row in chunks))

    def test_long_source_can_follow_an_explicit_reference_to_an_existing_short_source(self):
        original=law().replace('차량의 통행을 허용한다.','「짧은 참조 법률」 제3조를 따른다.')
        target='제1조(목적)\n교통안전을 목적으로 한다.\n제3조(추가 예외)\n응급 복구 차량도 제외한다.'
        job=self.upload([['원문 법률',original],['짧은 참조 법률',target]])
        self.run_job(job)
        with connection() as db:
            source=db.execute("SELECT id FROM department_documents WHERE title='원문 법률'").fetchone()['id']
            target_id=db.execute("SELECT id FROM department_documents WHERE title='짧은 참조 법률'").fetchone()['id']
            self.assertIsNone(db.execute('SELECT parent_id FROM department_documents_chunk_state WHERE parent_id=%s',(target_id,)).fetchone())
            seed=db.execute('SELECT start_offset,end_offset FROM department_documents_chunks WHERE parent_id=%s ORDER BY chunk_index LIMIT 1',(source,)).fetchone()
        result=retrieval.source_context(source,[seed])
        self.assertTrue(result['context_complete'],result)
        self.assertIn(str(target_id),result['content'])
        self.assertIn('응급 복구 차량도 제외한다',result['content'])

    def test_required_evidence_is_not_silently_truncated_to_fit_context(self):
        job=self.upload([['법령',law()]]);self.run_job(job)
        with patch.object(retrieval,'chat_tokens',new=AsyncMock(return_value=100000)):
            result=asyncio.run(retrieval.retrieve(VECTOR))
        self.assertEqual(len(result),1)
        self.assertFalse(result[0]['context_complete'])
        self.assertIn('근거 한도를 초과',result[0]['content'])
        self.assertNotIn('차량의 통행을 허용한다',result[0]['content'])

    def test_deleted_parent_is_not_searchable_and_permanent_delete_cascades(self):
        job=self.upload([['법령',law()]]);self.run_job(job)
        with connection() as db: db.execute('UPDATE department_documents SET deleted_at=NOW()')
        self.assertEqual(asyncio.run(retrieval.retrieve(VECTOR)),[])
        with connection() as db:
            db.execute('DELETE FROM department_documents')
            self.assertEqual(db.execute('SELECT count(*) AS n FROM department_documents_chunks').fetchone()['n'],0)

    def test_short_only_lookup_and_boundary_inputs_keep_original_behavior(self):
        job=self.upload([['경계','경계 원문'],['짧음','짧은 본문']])
        with patch.object(importer,'token_count',return_value=8192): self.run_job(job)
        state=self.state(job);self.assertEqual((state['saved_rows'],state['deferred_rows']),(2,0))
        with patch.object(retrieval,'legacy_lookup',return_value=[{'content':'기존 검색 결과'}]) as legacy:
            self.assertEqual(asyncio.run(retrieval.retrieve(VECTOR)),[{'content':'기존 검색 결과'}])
            legacy.assert_called_once_with(VECTOR)

    def test_resume_is_owner_only_and_ready_document_is_not_reinserted(self):
        job=self.upload([['법령',law()]]);self.run_job(job)
        duplicate=self.upload([['법령',law()]])
        embed=self.run_job(duplicate)
        self.assertEqual((embed.await_count,self.state(duplicate)['duplicate_rows']),(0,1))
        app.dependency_overrides[actor_from_auth]=lambda:dict(self.admin,sub=str(uuid.uuid4()))
        self.assertEqual(self.client.post(f"/api/rag-imports/{job['id']}/resume").status_code,403)
