import hashlib
import unittest
from unittest.mock import patch

from app.services import rag_retrieval as retrieval
from app.services.rag_splitting import document_structure


class ReferenceGraphTests(unittest.TestCase):
    def source(self,name,body):
        return dict(id=name,title=name,content=body,structure=document_structure(body),metadata={},
                    content_hash=hashlib.sha256(body.encode()).hexdigest())

    def test_shared_external_branch_is_loaded_once_and_not_rendered_repeatedly(self):
        sources={name:self.source(name,body) for name,body in {
            'A':'제1조(기준) 「B」 제1조와 「C」 제1조를 따른다.',
            'B':'제1조(기준) 「D」 제1조를 따른다.',
            'C':'제1조(기준) 「D」 제1조를 따른다.',
            'D':'제1조(최종 기준) 동일한 최종 근거이다.'}.items()}
        def fetch(sql,params):return [dict(parent_id=params[0],title=params[0],metadata={})]
        with patch.object(retrieval,'load_source',side_effect=lambda identifier:sources[identifier]) as load,patch.object(retrieval,'fetch_all',side_effect=fetch) as lookup:
            result=retrieval.source_context('A',[{'start_offset':0,'end_offset':1}])
        self.assertTrue(result['context_complete'],result)
        self.assertEqual(load.call_count,4);self.assertEqual(lookup.call_count,3)
        self.assertEqual(result['content'].count('동일한 최종 근거이다'),1)

    def test_supporting_definition_does_not_crawl_unrelated_laws_but_direct_reference_does(self):
        body='제2조(정의) "민원"은 「없는 법률」 제99조를 말한다. 제35조(이의신청) 민원 거부처분에 60일 이내 이의신청할 수 있다.'
        source=self.source('A',body);rule=source['structure'][1]
        with patch.object(retrieval,'load_source',return_value=source),patch.object(retrieval,'fetch_all',return_value=[]) as fetch:
            result=retrieval.source_context('A',[{'start_offset':rule['start'],'end_offset':rule['end']}])
            self.assertTrue(result['context_complete'],result);fetch.assert_not_called()
            result=retrieval.source_context('A',[{'start_offset':0,'end_offset':1}])
            self.assertFalse(result['context_complete']);self.assertIn('없는 법률',result['content'])

    def test_cancelled_traversal_does_not_issue_more_database_queries(self):
        context=retrieval.ReferenceTraversal();context.cancelled.set()
        with patch.object(retrieval,'load_source') as load:
            result=retrieval.source_context('A',[{'start_offset':0,'end_offset':1}],_context=context)
        load.assert_not_called();self.assertFalse(result['context_complete'])

    def test_mismatched_definition_quotes_do_not_swallow_a_law_name(self):
        text='제2조(정의) "민원"이란 신청을 말한다. "행정기관"이란 「행정절차법」 제2조를 따른다.'
        units=document_structure(text)
        self.assertEqual(units[0]['external'],[{'law':'행정절차법','key':'제2조'}])
