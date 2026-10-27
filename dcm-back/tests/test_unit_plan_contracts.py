"""Focused unit contracts added by the unit-test plan review (no live services)."""
import asyncio
import copy
import hashlib
import importlib
import io
import json
import math
import sys
import tempfile
import types
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from fastapi import HTTPException
from app import ai, security
from app.embeddings import normalize_embedding
from app.models import PasswordBody
from app.services import rag_imports, rag_retrieval, trash, complaint
from app.routers import complaints


class RagRecordUnitTests(unittest.TestCase):
    def test_body_is_preserved_exactly_and_metadata_is_carried(self):
        text='  제1조\n"공백과 인용" 🙂\t  '
        raw={'법령명':' 규칙 ','본문':text,'시행일':'2026-01-01','개정버전':'v2'}
        before=copy.deepcopy(raw)
        row=rag_imports.reference_record(raw,rag_imports.reference_mapping(list(raw)),'rules.csv',19)
        self.assertEqual(row['content'],text);self.assertEqual(raw,before)
        self.assertEqual(row['source_row'],19)
        self.assertEqual(row['metadata'],{'effective_date':'2026-01-01','law_version':'v2'})

    def test_alias_headers_and_question_answer_format(self):
        self.assertEqual(rag_imports.reference_mapping([' TITLE ','CONTENT'])['body'],'CONTENT')
        raw={'제목':'사례','질문':'누가?','답변':'담당자입니다.'}
        row=rag_imports.reference_record(raw,rag_imports.reference_mapping(list(raw)),'cases.csv',2)
        self.assertEqual(row['content'],'질문: 누가?\n답변: 담당자입니다.')

    def test_missing_body_and_incomplete_case_are_rejected(self):
        with self.assertRaises(ValueError):rag_imports.reference_mapping(['제목','기타'])
        for value in ('','   '):
            with self.subTest(body=value),self.assertRaises(ValueError):
                rag_imports.reference_record({'본문':value},{'body':'본문'},'x.csv',2)
        with self.assertRaises(ValueError):
            rag_imports.reference_record({'질문':'질문','답변':''},{'question':'질문','answer':'답변'},'x.csv',2)

    def test_missing_title_uses_source_row_without_changing_body(self):
        row=rag_imports.reference_record({'본문':' 원문 '},{'body':'본문'},'rules.csv',19)
        self.assertEqual((row['title'],row['source_row'],row['content']),('rules 18',19,' 원문 '))


class TrashCapacityUnitTests(unittest.TestCase):
    def check_policy(self,total,incoming,expected):
        db=Mock();db.execute.return_value.fetchone.return_value={'n':total}
        actor={'sub':'unit-only'}
        with patch.object(trash,'purge_recent',side_effect=lambda db,n,actor:n) as purge:
            result=trash.reserve_capacity(db,incoming,actor)
        self.assertEqual(result,expected)
        if expected:purge.assert_called_once_with(db,expected,actor)
        else:purge.assert_not_called()

    def test_room_and_exact_capacity_do_not_purge(self):
        self.check_policy(9998,1,0);self.check_policy(9999,1,0)
    def test_full_archive_small_incoming_purges_buffer(self):
        self.check_policy(10000,1,100);self.check_policy(10000,100,100)
    def test_large_incoming_reserves_incoming_plus_buffer(self):
        self.check_policy(10000,150,250)
    def test_legacy_over_capacity_is_brought_back_under_limit(self):
        self.check_policy(10500,1,501)


class EmbeddingVectorUnitTests(unittest.TestCase):
    def test_valid_vector_is_unit_length_without_mutating_input(self):
        values=[3.,4.]+[0.]*1022;original=values.copy()
        result=normalize_embedding(values)
        self.assertEqual(len(result),1024);self.assertEqual(result[:2],[.6,.8])
        self.assertAlmostEqual(math.sqrt(sum(x*x for x in result)),1)
        self.assertEqual(values,original)
    def test_wrong_dimensions_are_rejected_without_padding(self):
        for n in (0,1023,1025,1536):
            with self.subTest(n=n),self.assertRaises(ValueError):normalize_embedding([1.]*n)
    def test_nonfinite_zero_and_nonnumeric_vectors_are_rejected(self):
        for bad in (float('nan'),float('inf'),float('-inf'),'not-a-number'):
            with self.subTest(value=str(bad)),self.assertRaises(ValueError):
                normalize_embedding([bad]+[0.]*1023)
        with self.assertRaises(ValueError):normalize_embedding([0.]*1024)
    def test_finite_extreme_input_must_be_unit_length_or_explicitly_rejected(self):
        # Overflow must not silently turn an accepted nonzero vector into zeros.
        try:result=normalize_embedding([1e308]+[0.]*1023)
        except ValueError:return
        self.assertTrue(all(math.isfinite(x) for x in result))
        self.assertAlmostEqual(math.sqrt(sum(x*x for x in result)),1.,places=6)


class VersionSelectionUnitTests(unittest.TestCase):
    def row(self,key,day,version):return {'parent_id':key,'metadata':{'effective_date':day,'law_version':version}}
    def test_latest_effective_version_and_future_exclusion(self):
        rows=[self.row('old','2024-01-01','1'),self.row('new','2026-01-01','2')]
        self.assertEqual(rag_retrieval.choose_versions(rows,date(2025,1,1)),({'old'},None))
        self.assertEqual(rag_retrieval.choose_versions(rows,date(2026,10,26)),({'new'},None))
    def test_future_only_is_not_published(self):
        self.assertEqual(rag_retrieval.choose_versions([self.row('future','2027-01-01','3')],date(2026,10,26)),(set(),None))
    def test_ambiguous_missing_dates_and_equal_effective_dates_warn(self):
        cases=[[self.row('a','','1'),self.row('b','','2')],
               [self.row('a','2026-01-01','1'),self.row('b','2026-01-01','2')]]
        for rows in cases:
            ids,warning=rag_retrieval.choose_versions(rows,date(2026,10,26))
            self.assertEqual(ids,set());self.assertTrue(warning)
    def test_effective_date_accepts_real_calendar_dates_only(self):
        self.assertEqual(rag_retrieval.effective_date('2024년 2월 29일'),date(2024,2,29))
        self.assertIsNone(rag_retrieval.effective_date('2026-02-29'))
        self.assertIsNone(rag_retrieval.effective_date('날짜 없음'))


class SourceIntegrityUnitTests(unittest.TestCase):
    def test_changed_original_never_publishes_old_chunk_context(self):
        body='제1조(허용) 차량 통행을 허용한다.'
        source={'id':'A','title':'규정','content':body,'metadata':{},
                'structure':rag_retrieval.document_structure(body),
                'content_hash':hashlib.sha256(b'older source').hexdigest()}
        with patch.object(rag_retrieval,'load_source',return_value=source):
            result=rag_retrieval.source_context('A',[{'start_offset':0,'end_offset':1}])
        self.assertFalse(result['context_complete'])
        self.assertNotIn('차량 통행을 허용한다',result['content'])
        self.assertTrue(result['warnings'])


class ExistingContractUnitTests(unittest.TestCase):
    def test_password_salts_and_verification(self):
        salt1,h1=security.password_hash('UnitOnly!2026')
        salt2,h2=security.password_hash('UnitOnly!2026')
        self.assertNotEqual(salt1,salt2);self.assertNotEqual(h1,h2)
        self.assertTrue(security.verify_password('UnitOnly!2026',salt1,h1))
        self.assertFalse(security.verify_password('wrong',salt1,h1))

    def test_missing_expired_tampered_and_malformed_tokens_are_rejected(self):
        actor={'id':'unit-user','owner_id':'unit-owner','account_role':'user','department_id':None}
        with patch.object(security.time,'time',return_value=1000):
            token=security.issue_token(actor);self.assertIsNotNone(security.read_token(token))
        with patch.object(security.time,'time',return_value=1000+8*3600+1):
            self.assertIsNone(security.read_token(token))
        with patch.object(security,'fetch_one') as db:
            for value in (None,'bad','Bearer broken.token','Bearer '+token+'x'):
                self.assertIsNone(security.read_token(value))
                with self.assertRaises(HTTPException) as error:security.actor_from_auth(value)
                self.assertEqual(error.exception.status_code,401)
            db.assert_not_called()

    def test_disabled_global_delete_handlers_do_not_touch_database(self):
        actor={'role':'admin','department_id':2}
        with patch.object(complaints,'connection') as db:
            for method,args in ((complaints.permanent_all,(PasswordBody(password='unit-only'),actor)),
                                (complaints.permanent_department_all,(actor,))):
                with self.assertRaises(HTTPException) as error:method(*args)
                self.assertEqual(error.exception.status_code,403)
            db.assert_not_called()

    def test_personal_identifiers_are_completely_removed(self):
        value='연락처 010-1234-5678, a.b@test.com, 900101-1234567'
        self.assertEqual(ai.redact(value),'연락처 [개인정보 제외], [개인정보 제외], [개인정보 제외]')

    def test_fallback_keywords_and_urgency(self):
        result=ai.fallback('신호등 고장','횡단보도 신호등 고장으로 보행 신호가 안 들어옵니다.')
        self.assertEqual(result['category'],'교통·국토');self.assertEqual(result['processing_mode'],'fallback')
        self.assertTrue(all(k in '신호등 고장 횡단보도 신호등 고장으로 보행 신호가 안 들어옵니다.' for k in result['keywords']))
        self.assertEqual(ai.fallback('','화재 위험이 있습니다.')['urgency'],'high')
        self.assertEqual(ai.fallback('','일반적인 문의입니다.')['urgency'],'medium')

    def test_complaint_number_and_intent_validation(self):
        from app.chat_intent import explicit_complaint_ids,validate_model_intent,ChatIntentBody
        self.assertEqual(explicit_complaint_ids('12번 민원 삭제해줘'),{12})
        self.assertEqual(explicit_complaint_ids('#7 확인'),{7})
        self.assertEqual(explicit_complaint_ids('3번지 2층, 5000원 냈어요'),set())
        with self.assertRaises(ValueError):
            validate_model_intent({'intent':'edit','complaint_id':99,'draft':None},ChatIntentBody(message='내 민원 수정할래'))
        result=validate_model_intent({'intent':'search','complaint_id':99,'draft':{'title':'x','content':'y'}},ChatIntentBody(message='조회할래'))
        self.assertIsNone(result.draft);self.assertIsNone(result.complaint_id)

    def test_saved_record_fields_and_model_versions_are_bound(self):
        conn=MagicMock();cur=conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value={'id':1}
        record={'title':'제목','content':'보존 본문','summary':'요약','category':'교통·국토',
                'processing_mode':'llm','model':'unit-model','prompt_version':'unit-prompt'}
        with patch.object(complaint,'lock_identities'),patch.object(complaint,'find_duplicate',return_value=None),patch.object(complaint,'resolve_department',return_value={'id':2}):
            self.assertTrue(complaint.insert_complaint(conn,record,'unit.csv',19,'unit-owner'))
        query,params=cur.execute.call_args.args
        columns=[x.strip() for x in query.split('(',1)[1].split(')',1)[0].split(',')]
        values=dict(zip(columns,params))
        for name,expected in [('title','제목'),('content','보존 본문'),('summary','요약'),('llm_model','unit-model'),('prompt_version','unit-prompt'),('department_id',2)]:
            self.assertEqual(values[name],expected)


class AnalysisOutputUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_category_and_empty_summary_fall_back(self):
        for raw in ({'category':'없는 분야','summary':'요약'},{'category':'교통·국토','summary':''}):
            answer=types.SimpleNamespace(content=json.dumps(raw),model='unit-model',finish_reason='stop',prompt_tokens=1,output_tokens=1)
            with patch.object(ai.llm,'chat_json',new=AsyncMock(return_value=answer)),patch.object(ai.llm,'log_usage'):
                result=await ai.analyze('제목','도로의 신호등이 고장났습니다.')
            self.assertEqual(result['processing_mode'],'fallback');self.assertTrue(result['reason'])
    async def test_model_connection_failure_falls_back_without_network(self):
        with patch.object(ai.llm,'chat_json',new=AsyncMock(side_effect=ConnectionError('unit injected failure'))):
            result=await ai.analyze('제목','도로의 신호등이 고장났습니다.')
        self.assertEqual(result['processing_mode'],'fallback')


class TrainingHelperUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder=Path(__file__).resolve().parents[2]/'model-training'
        sys.path.insert(0,str(folder))
        cls.records=importlib.import_module('records')
        cls.evaluate=importlib.import_module('evaluate')
        cls.export=importlib.import_module('export')

    def valid(self):
        return {'id':'unit','group':'unit','title':'신호등 고장','content':'3일째 신호등이 고장났습니다.',
                'output':{'title':'신호등 고장','summary':'3일째 신호등이 고장났다.','key_points':['신호등 고장'],
                          'category':'교통·국토','keywords':['신호등'],'urgency':'medium','needs_review':False,'review_reason':None}}

    def test_analysis_validation_codes(self):
        self.assertEqual(self.records.check_analysis(self.valid()),[])
        changes=[('keywords',['없는단어'],'C6'),('category','교통','C5'),('summary','5일째 고장났다.','C3')]
        for field,value,code in changes:
            row=self.valid();row['output'][field]=value
            self.assertTrue(any(code in error for error in self.records.check_analysis(row)))
        for needs,reason in ((True,''),(False,'이유')):
            row=self.valid();row['output'].update(needs_review=needs,review_reason=reason)
            self.assertTrue(any('C8' in error for error in self.records.check_analysis(row)))

    def test_chat_tool_source_must_match(self):
        errors=self.records.check_chat({'id':'unit','group':'unit','text':'원래 본문','tool':'insertRequestT','arguments':{'title':'제목','content':'다른 본문'}})
        self.assertTrue(any('A7' in error for error in errors))

    def test_split_and_balanced_sampling(self):
        assigned=[self.records.assign_split({'id':str(i),'group':'same'}) for i in range(5)]
        self.assertEqual(len(set(assigned)),1)
        self.assertEqual(assigned,[self.records.assign_split({'id':str(i),'group':'same'}) for i in range(5)])
        quota=self.records.balanced_quota({str(i):100+i for i in range(7)},70)
        self.assertEqual(sum(quota.values()),70);self.assertLessEqual(max(quota.values())-min(quota.values()),1)

    def test_json_parser_normal_and_broken(self):
        self.assertEqual(self.evaluate.parse_json_output('```json\n{"x":1}\n```'),{'x':1})
        self.assertIsNone(self.evaluate.parse_json_output('{"x":1'))

    def test_corrupt_header_and_truncated_gguf_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'unit.gguf';path.write_bytes(b'bad header')
            with self.assertRaises(RuntimeError):self.export.verify_gguf(path)
            path.write_bytes(b'GGUF'+b'\0'*28)
            fake=types.SimpleNamespace(GGUFReader=lambda _:types.SimpleNamespace(tensors=[types.SimpleNamespace(data_offset=32,n_bytes=1024)]))
            with patch.dict(sys.modules,{'gguf':fake}),self.assertRaises(RuntimeError):
                self.export.verify_gguf(path,min_gb=0)


if __name__=='__main__':unittest.main()
