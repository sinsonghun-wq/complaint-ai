import asyncio
import hashlib
import unittest
from unittest.mock import AsyncMock, patch

from app.services import rag_retrieval as retrieval
from app.services.rag_splitting import document_structure


def source(identifier, body, title=None):
    return dict(id=identifier,title=title or identifier,content=body,
                structure=document_structure(body),metadata={},
                content_hash=hashlib.sha256(body.encode('utf-8')).hexdigest())


class EvidenceBudgetTests(unittest.TestCase):
    seed = [{'start_offset':0,'end_offset':1}]

    def test_exact_5000_character_boundary_includes_headers_and_keeps_whole_clause(self):
        original = '제1조(기준) 한글🙂을 보존한다. '
        initial = source('A',original)
        overhead = len(retrieval.source_header(initial))+1+len('[제1조(기준)]\n')
        body = original+'가'*(5000-overhead-len(original))
        for extra in ('','나'):
            document = source('A',body+extra)
            with patch.object(retrieval,'load_source',return_value=document):
                result = retrieval.source_context('A',self.seed)
            self.assertLessEqual(len(result['content']),5000)
            self.assertEqual(result['context_complete'],not bool(extra))
            if not extra:
                self.assertEqual(len(result['content']),5000)
                self.assertIn(body,result['content'])
            else:
                self.assertIn('근거 한도를 초과',result['content'])
                self.assertNotIn('한글🙂을 보존한다',result['content'])
            self.assertEqual(document['content'],body+extra)

    def test_oversized_seed_stops_before_outgoing_reference_queries(self):
        document = source('A','제1조(기준) 「B」 제1조를 따른다. '+'원문보존 '*2000)
        with patch.object(retrieval,'load_source',return_value=document),patch.object(retrieval,'fetch_all') as lookup:
            result = retrieval.source_context('A',self.seed)
        lookup.assert_not_called()
        self.assertFalse(result['context_complete'])
        self.assertIn('5,000자',result['content'])
        self.assertNotIn('원문보존',result['content'])

    def test_required_external_overflow_does_not_publish_rule_or_visit_later_laws(self):
        documents = {
            'A':source('A','제1조(기준) 통행을 허용한다. 「B」 제1조와 「C」 제1조를 따른다.'),
            'B':source('B','제1조(예외) 다만 '+'필수 예외 '*1500),
            'C':source('C','제1조(추가 기준) 확인이 필요하다.'),
        }
        with patch.object(retrieval,'load_source',side_effect=lambda identifier:documents[identifier]) as load, \
             patch.object(retrieval,'fetch_all',side_effect=lambda sql,params:[dict(parent_id=params[0],metadata={})]) as lookup:
            result = retrieval.source_context('A',self.seed)
        self.assertFalse(result['context_complete'])
        self.assertNotIn('통행을 허용한다',result['content'])
        self.assertEqual(load.call_count,2)
        self.assertEqual(lookup.call_count,1)
        self.assertLessEqual(len(result['content']),5000)

    def test_duplicate_shared_reference_is_charged_only_once(self):
        documents = {
            'A':source('A','제1조(기준) 「B」 제1조와 「C」 제1조를 따른다.'),
            'B':source('B','제1조(기준) 「D」 제1조를 따른다.'),
            'C':source('C','제1조(기준) 「D」 제1조를 따른다.'),
            'D':source('D','제1조(최종 기준) 모든 필수 조건이다.'),
        }
        with patch.object(retrieval,'load_source',side_effect=lambda identifier:documents[identifier]), \
             patch.object(retrieval,'fetch_all',side_effect=lambda sql,params:[dict(parent_id=params[0],metadata={})]):
            first = retrieval.source_context('A',self.seed)
            exact = retrieval.source_context('A',self.seed,max_chars=len(first['content']))
        self.assertTrue(exact['context_complete'],exact)
        self.assertEqual(exact['content'],first['content'])
        self.assertEqual(exact['content'].count('모든 필수 조건이다'),1)

    def test_multiple_long_sources_and_notices_share_one_5000_character_budget(self):
        documents = {name:source(name,'제1조(기준) '+name*2400) for name in ('A','B','C')}
        candidates = [dict(parent_id=name,title=name,kind='long',distance=index,
                           start_offset=0,end_offset=1) for index,name in enumerate(documents)]
        with patch.object(retrieval,'lookup',return_value=([],candidates,['개정본 확인이 필요한 자료가 있습니다.'])), \
             patch.object(retrieval,'load_source',side_effect=lambda identifier:documents[identifier]), \
             patch.object(retrieval,'chat_tokens',new=AsyncMock(return_value=1)):
            results = asyncio.run(retrieval.retrieve([1.0]))
        self.assertEqual(len(results),4)
        self.assertTrue(results[0]['context_complete'])
        self.assertTrue(any(not item['context_complete'] for item in results))
        self.assertLessEqual(sum(len(item['content']) for item in results),5000)
        self.assertTrue(all(item['content'] for item in results))

    def test_short_result_policy_is_preserved_in_a_mixed_search(self):
        short = dict(parent_id='short',title='짧은 자료',content='짧'*5000,kind='short',distance=0)
        chunk = dict(parent_id='A',title='A',kind='long',distance=1,start_offset=0,end_offset=1)
        document = source('A','제1조(기준) 필요한 전체 조항이다.')
        with patch.object(retrieval,'lookup',return_value=([short],[chunk],[])), \
             patch.object(retrieval,'load_source',return_value=document), \
             patch.object(retrieval,'chat_tokens',new=AsyncMock(return_value=1)):
            results = asyncio.run(retrieval.retrieve([1.0]))
        self.assertEqual(results[0],{'content':short['content']})
        self.assertTrue(results[1]['context_complete'])
        self.assertIn('필요한 전체 조항이다',results[1]['content'])

    def test_long_titles_and_many_missing_references_still_produce_bounded_notice(self):
        document = source('A','제1조(기준) 원문 규정이다.',title='긴 자료명'*2000)
        with patch.object(retrieval,'load_source',return_value=document):
            result = retrieval.source_context('A',self.seed,max_chars=160)
        self.assertFalse(result['context_complete'])
        self.assertLessEqual(len(result['content']),160)
        self.assertIn('단정하지',result['content'])
        self.assertNotIn('원문 규정이다',result['content'])

    def test_retrieval_deadline_still_cancels_background_reference_work(self):
        contexts = []
        async def stuck(vector,query,context):
            contexts.append(context)
            await asyncio.Event().wait()
        with patch.object(retrieval,'RETRIEVAL_SECONDS',0.01), \
             patch.object(retrieval,'_retrieve',side_effect=stuck):
            result = asyncio.run(retrieval.retrieve([1.0]))
        self.assertTrue(contexts[0].cancelled.is_set())
        self.assertFalse(result[0]['context_complete'])
        self.assertIn('제한 시간을 초과',result[0]['content'])
