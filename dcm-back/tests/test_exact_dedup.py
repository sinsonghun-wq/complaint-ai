"""Exact duplicate protection, isolated PostgreSQL and optional real samples."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from threading import Barrier
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import dedup
from app.ai import fallback, fingerprint
from app.db import connection
from app.main import app
from app.security import actor_from_auth
from app.services import imports
from app.services.complaint import save_records, insert_complaint
from app.services.parser import parsed_record
from app.services.trash import restore_record

ROOT = Path(__file__).resolve().parents[2]
ADMIN = {'role':'admin','department_id':2,'owner_id':None,'sub':str(uuid.uuid4())}


class IdentityTests(unittest.TestCase):
    def test_semantics_punctuation_numbers_and_real_spacing_are_not_ignored(self):
        for first, second in [
            ('도로를 보수해주세요.', '도로를 수리해주세요.'),
            ('도로를 보수해주세요.', '도로를 보수해주세요!'),
            ('도로 123번지', '도로 124번지'),
            ('아버지가 방에 들어갑니다.', '아버지 가방에 들어갑니다.'),
            ('도로를 보수해주세요.', '도로를 보수해 주세요.'),
        ]:
            with self.subTest(first=first):
                self.assertFalse(dedup.equivalent(dedup.identity(first),dedup.identity(second)))

    def test_only_native_pdf_wrap_positions_can_be_reconciled(self):
        original=dedup.identity('퇴직급여를 신청했습니다.')
        pdf=dedup.identity('퇴\n직급여를 신청했습니다.',pdf_wraps=True)
        self.assertTrue(dedup.equivalent(original,pdf))
        self.assertTrue(dedup.equivalent(pdf,original))
        self.assertFalse(dedup.equivalent(original,dedup.identity('퇴 직급여를 신청했습니다.')))
        self.assertFalse(dedup.equivalent(original,dedup.identity('퇴\n직급여를 신청했습니다.')))
        self.assertFalse(dedup.equivalent(original,dedup.identity('퇴\n\n직급여를 신청했습니다.',pdf_wraps=True)))

    def test_signed_intake_preserves_pii_identity_without_returning_pii(self):
        first=parsed_record('민원','도로 보수 요청입니다. 연락처 010-1234-5678')
        second=parsed_record('민원','도로 보수 요청입니다. 연락처 010-9999-8888')
        self.assertEqual(first['content'],second['content'])
        self.assertNotIn('010-1234-5678',json.dumps(first))
        self.assertNotEqual(dedup.item_identity(first)['raw'],dedup.item_identity(second)['raw'])
        self.assertNotEqual(dedup.item_identity(first)['raw'],fingerprint(first['content']))

    def test_forged_profile_token_or_changed_preview_is_rejected(self):
        record=parsed_record('민원','도로의 보수를 요청합니다.')
        for altered in [dict(record,dedup_token=record['dedup_token']+'x'),dict(record,content='변경한 본문')]:
            with self.assertRaises(HTTPException) as error:dedup.item_identity(altered)
            self.assertEqual(error.exception.status_code,422)
        untrusted={'content':'새 원문','_raw_identity':dedup.identity('가짜 원문'),'source_file':'fake.pdf'}
        self.assertEqual(dedup.item_identity(untrusted),dedup.identity('새 원문'))


class ExactDedupDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.settings import DATABASE_URL
        cls.original=DATABASE_URL
        cls.name='complaintai_exact_test_'+uuid.uuid4().hex
        cls.url=make_conninfo(DATABASE_URL,dbname=cls.name)
        with psycopg.connect(DATABASE_URL,autocommit=True) as db:
            db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(cls.name)))
        try:
            with psycopg.connect(cls.url) as db:
                db.execute((ROOT/'dcm-back/tests/fixtures/init.sql').read_text(encoding='utf8'))
                cls.owner=str(uuid.uuid4())
                db.execute('INSERT INTO app_users(id,owner_id,email) VALUES(%s,%s,%s)',(uuid.uuid4(),cls.owner,'exact-test@example.invalid'))
        except Exception:
            with psycopg.connect(DATABASE_URL,autocommit=True) as db:
                db.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(cls.name)))
            raise
        cls.db_patch=patch('app.db.DATABASE_URL',cls.url);cls.db_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.db_patch.stop()
        assert cls.name.startswith('complaintai_exact_test_') and cls.name!=conninfo_to_dict(cls.original)['dbname']
        with psycopg.connect(cls.original,autocommit=True) as db:
            db.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(cls.name)))

    def setUp(self):
        with connection() as db:db.execute('DELETE FROM complaints')
        self.embed=patch('app.services.complaint.embeddingForSelect',new=AsyncMock(return_value=None));self.embed.start()
        self.addCleanup(self.embed.stop)

    def save(self, records, actor=ADMIN):
        return asyncio.run(save_records(records,actor))

    def count(self):
        with connection() as db:return db.execute('SELECT count(*) AS n FROM complaints').fetchone()['n']

    def test_same_body_different_title_extension_and_admin_department_is_duplicate(self):
        first={'title':'제목 A','content':'같은 도로 보수 민원입니다.','source_file':'first.xlsx'}
        self.assertEqual(self.save([first]),(1,0))
        self.assertEqual(self.save([dict(first,title='제목 B',source_file='second.pdf')],dict(ADMIN,department_id=4)),(0,1))

    def test_changed_phone_or_email_is_saved_separately(self):
        for contact in [('010-1234-5678','010-9999-8888'),('a@example.com','b@example.com')]:
            first=parsed_record('연락','도로를 보수해 주세요. 연락처 '+contact[0])
            second=parsed_record('연락','도로를 보수해 주세요. 연락처 '+contact[1])
            self.assertEqual(self.save([first]),(1,0))
            self.assertEqual(self.save([second]),(1,0))
            self.assertEqual(self.save([first]),(0,1))

    def test_similar_body_and_meaningful_spaces_are_separate(self):
        rows=[{'title':'민원','content':s} for s in ['아버지가 방에 들어갑니다.','아버지 가방에 들어갑니다.','아버지가 방에 들어가십니다.']]
        self.assertEqual(self.save(rows),(3,0))

    def test_signed_pdf_wrap_reconciliation_both_orders(self):
        for reverse in (False,True):
            with connection() as db:db.execute('DELETE FROM complaints')
            rows=[parsed_record('연금','퇴직급여를 신청했습니다.'),parsed_record('연금','퇴\n직급여를 신청했습니다.',pdf_wraps=True)]
            if reverse:rows.reverse()
            self.assertEqual(self.save([rows[0]]),(1,0));self.assertEqual(self.save([rows[1]]),(0,1))

    def test_unique_index_protects_direct_sql_and_null_admin_scope(self):
        self.save([{'title':'민원','content':'도로 보수를 요청합니다.'}])
        with self.assertRaises(psycopg.errors.UniqueViolation):
            with connection() as db:
                db.execute("INSERT INTO complaints(title,content,raw_content_fingerprint) VALUES('직접 쓰기','다른 내용',%s)",(dedup.identity('도로 보수를 요청합니다.')['raw'],))
        self.assertEqual(self.count(),1)

    def test_concurrent_exact_and_pdf_uploads_cannot_insert_twice(self):
        for pdf_case in (False,True):
            with connection() as db:db.execute('DELETE FROM complaints')
            barrier=Barrier(2)
            first=parsed_record('민원','퇴직급여를 신청했습니다.')
            second=parsed_record('민원','퇴\n직급여를 신청했습니다.',pdf_wraps=True) if pdf_case else first
            def writer(row):
                barrier.wait(timeout=10)
                return self.save([row])
            with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(writer,[first,second]))
            self.assertEqual(sum(r[0] for r in results),1);self.assertEqual(self.count(),1)

    def test_reversed_batches_do_not_deadlock(self):
        rows=[{'title':'민원','content':f'도로 보수 요청 {i}'} for i in range(4)]
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(self.save,[rows,list(reversed(rows))]))
        self.assertEqual(sum(r[0] for r in results),4);self.assertEqual(self.count(),4)

    def test_scope_and_deleted_resubmission_restore_conflict(self):
        row={'title':'민원','content':'도로의 보수가 필요합니다.'}
        self.assertEqual(self.save([row]),(1,0))
        self.assertEqual(self.save([row],{'role':'user','owner_id':self.owner}),(1,0))
        with connection() as db:
            archived=db.execute("UPDATE complaints SET deleted_at=NOW(),complaint_status='취소' WHERE owner_user_id IS NULL RETURNING id").fetchone()['id']
        self.assertEqual(self.save([row]),(1,0))
        with self.assertRaises(HTTPException) as error:restore_record(archived,ADMIN)
        self.assertEqual(error.exception.status_code,409)
        self.assertEqual(self.count(),3)

    def test_csv_identity_captured_before_masking_matches_document_intake(self):
        content='도로 보수를 요청합니다. 연락처 010-1234-5678'
        with patch.object(imports,'LLM_IMPORT_ENABLED',False):
            result=asyncio.run(imports.analyze_import_record({'title':'CSV','content':content,'source_row':2}))
        with connection() as db:self.assertTrue(insert_complaint(db,result,'same.csv',2,None))
        self.assertEqual(self.save([parsed_record('XLSX',content)]),(0,1))
        self.assertEqual(self.save([parsed_record('XLSX',content.replace('1234','9999'))]),(1,0))

    def test_legacy_migration_is_repeatable_and_preserves_duplicates_and_masked_rows(self):
        with connection() as db:
            db.execute("INSERT INTO complaints(title,content) VALUES('기존1','동일 기존 원문'),('기존2','동일 기존 원문'),('마스킹','연락처 [개인정보 제외]')")
        dedup.ensure_dedup_schema();dedup.ensure_dedup_schema()
        self.assertEqual(self.count(),3)
        with connection() as db:
            rows=db.execute('SELECT raw_content_fingerprint,dedup_profile FROM complaints ORDER BY id').fetchall()
        self.assertIsNotNone(rows[0]['raw_content_fingerprint'])
        self.assertIsNone(rows[1]['raw_content_fingerprint'])
        self.assertIn('legacy_duplicate_of',rows[1]['dedup_profile'])
        self.assertIsNone(rows[2]['raw_content_fingerprint'])
        self.assertTrue(rows[2]['dedup_profile']['legacy_original_unavailable'])
        self.assertEqual(self.save([{'title':'재업로드','content':'동일 기존 원문'}]),(0,1))

    def test_intake_token_survives_actual_batch_api(self):
        old=dict(app.dependency_overrides);app.dependency_overrides[actor_from_auth]=lambda:ADMIN
        try:
            client=TestClient(app)
            first=parsed_record('문서','퇴직급여를 신청했습니다.')
            second=parsed_record('PDF','퇴\n직급여를 신청했습니다.',pdf_wraps=True)
            self.assertEqual(client.post('/api/complaints/batch',json={'complaints':[first]}).json(),{'saved':1,'duplicates':0})
            self.assertEqual(client.post('/api/complaints/batch',json={'complaints':[second]}).json(),{'saved':0,'duplicates':1})
            second['content']='변조된 본문'
            self.assertEqual(client.post('/api/complaints/batch',json={'complaints':[second]}).status_code,422)
            client.close()
        finally:
            app.dependency_overrides.clear();app.dependency_overrides.update(old)

    def test_citizen_submit_and_edit_keep_original_identity_and_conflicts_readable(self):
        old=dict(app.dependency_overrides)
        app.dependency_overrides[actor_from_auth]=lambda:{'role':'user','owner_id':self.owner,'sub':str(uuid.uuid4())}
        try:
            client=TestClient(app)
            first={'title':'민원','content':'도로 보수 요청입니다. 010-1234-5678'}
            second=dict(first,content=first['content'].replace('1234','9999'))
            with patch('app.routers.complaints.classify_submission',new=AsyncMock()):
                result=client.post('/api/complaints',json=first)
                self.assertEqual(result.status_code,201,result.text)
                identifier=result.json()['complaint']['id']
                self.assertEqual(client.post('/api/complaints',json=first).json()['duplicates'],1)
                self.assertEqual(client.post('/api/complaints',json=second).status_code,201)
                self.assertEqual(client.patch(f'/api/complaints/{identifier}',json=second).status_code,409)
                changed=dict(first,content='완전히 새로운 보수 요청입니다. 010-1111-2222')
                self.assertEqual(client.patch(f'/api/complaints/{identifier}',json=changed).status_code,200)
            with connection() as db:
                row=db.execute('SELECT raw_content_fingerprint FROM complaints WHERE id=%s',(identifier,)).fetchone()
            self.assertEqual(row['raw_content_fingerprint'],dedup.identity(changed['content'])['raw'])
            client.close()
        finally:
            app.dependency_overrides.clear();app.dependency_overrides.update(old)

    def test_real_72_case_formats_in_both_orders(self):
        folder=Path(os.getenv('COMPLAINTAI_SAMPLES_DIR',str(ROOT.parent/'complaintAI'/'민원 원본데이터'/'민원 엑셀')))
        base='민원데이터_11개분야_72건'
        if not all((folder/(base+suffix)).exists() for suffix in ('.xlsx','.pdf','.hwp')):
            self.skipTest('Optional local 72-case sample files unavailable')
        old=dict(app.dependency_overrides);app.dependency_overrides[actor_from_auth]=lambda:ADMIN
        client=TestClient(app)
        try:
            records={}
            for suffix in ('xlsx','pdf','hwp'):
                path=folder/(base+'.'+suffix)
                with path.open('rb') as stream:
                    result=client.post('/api/intake',files={'file':(path.name,stream)})
                self.assertEqual(result.status_code,200,result.text)
                records[suffix]=result.json()['complaints']
                self.assertEqual(len(records[suffix]),72)
            for order in [('xlsx','pdf','hwp'),('pdf','xlsx','hwp'),('hwp','pdf','xlsx')]:
                with self.subTest(order=order):
                    with connection() as db:db.execute('DELETE FROM complaints')
                    for index,suffix in enumerate(order):
                        result=client.post('/api/complaints/batch',json={'complaints':records[suffix]})
                        self.assertEqual(result.status_code,201,result.text)
                        self.assertEqual(result.json(),{'saved':72 if index==0 else 0,'duplicates':0 if index==0 else 72})
                    self.assertEqual(self.count(),72)
        finally:
            client.close();app.dependency_overrides.clear();app.dependency_overrides.update(old)


if __name__=='__main__':unittest.main()
