import asyncio
import math
import unittest
from unittest.mock import AsyncMock, patch

from app.rag import recommend, retrieval_questions
from app.rag_ingest import confined_path, split_section
from app.rag_models import RagError, generate_answer, normalize_vector, rerank
from pathlib import Path


class RagContractTests(unittest.TestCase):
    def test_multiple_questions_retrieve_separately(self):
        questions = retrieval_questions('119안심콜', '고령자만 가입할 수 있나요? 가입 후 연락처가 바뀌면 어떻게 하나요?')
        self.assertEqual(len(questions), 2)
        self.assertIn('연락처', questions[1])

    def test_vectors_require_exact_dimension_finite_nonzero_values(self):
        for vector in ([1] * 1536, [1] * 1023, [0] * 1024, [float('nan')] * 1024):
            with self.assertRaises(RagError):
                normalize_vector(vector)
        normalized = normalize_vector([1] * 1024)
        self.assertAlmostEqual(sum(x * x for x in normalized), 1)

    def test_chunking_keeps_short_faq_and_long_paragraph_content(self):
        text = '조건과 예외를 함께 보존합니다.'
        self.assertEqual(split_section(text), [text])
        parts = split_section('가' * 1500 + '\n' + '나' * 1400)
        self.assertEqual(parts, ['가' * 1500, '나' * 1400])

    def test_manifest_path_cannot_escape_source_directory(self):
        with self.assertRaises(ValueError):
            confined_path(Path.cwd(), '../outside.txt')

    def test_reranker_rejects_wrong_model_or_invalid_indexes(self):
        for payload in ({'model': 'another-model', 'results': []}, {'model': 'Qwen/Qwen3-Reranker-0.6B', 'results': [{'index': 2, 'relevance_score': .8}]}):
            with patch('app.rag_models.post_json', new=AsyncMock(return_value=payload)):
                with self.assertRaises(RagError):
                    asyncio.run(rerank('질문', ['자료']))

    def test_empty_matching_profile_stops_before_generation(self):
        with patch('app.rag.embedding_identity', new=AsyncMock(return_value={'profile': 'changed-model'})), patch('app.rag.embed_texts', new=AsyncMock(return_value=[[1] * 1024])), patch('app.rag.retrieve_candidates', return_value=[]), patch('app.rag.generate_answer', new=AsyncMock()) as generator:
            result = asyncio.run(recommend('제목', '내용'))
            self.assertEqual(result['answer_status'], 'insufficient_evidence')
            generator.assert_not_called()

    def test_irrelevant_evidence_stops_before_generation(self):
        with patch('app.rag.embedding_identity', new=AsyncMock(return_value={'profile': 'profile'})), patch('app.rag.embed_texts', new=AsyncMock(return_value=[[1] * 1024])), patch('app.rag.retrieve_candidates', return_value=[{'heading': '관계없는 자료', 'content': '다른 주제'}]), patch('app.rag.rerank', new=AsyncMock(return_value=([{'index': 0, 'relevance_score': .01}], {'model': 'reranker'}))), patch('app.rag.generate_answer', new=AsyncMock()) as generator:
            result = asyncio.run(recommend('제목', '내용'))
            self.assertEqual(result['citations'], [])
            generator.assert_not_called()

    def test_model_outage_is_not_a_successful_answer(self):
        with patch('app.rag.embedding_identity', new=AsyncMock(side_effect=RagError('embedding_unavailable', '연결 실패'))):
            with self.assertRaises(RagError):
                asyncio.run(recommend('제목', '내용'))

    def test_followup_retrieval_uses_user_context_but_not_prior_ai_claims(self):
        history = [{'role': 'user', 'content': '아파트에서 층간소음이 납니다.'},
                   {'role': 'assistant', 'content': '이전 AI의 검증되지 않은 주장'}]
        with patch('app.rag.embedding_identity', new=AsyncMock(return_value={'profile': 'profile'})), \
             patch('app.rag.embed_texts', new=AsyncMock(return_value=[[1] * 1024])) as embedder, \
             patch('app.rag.retrieve_candidates', return_value=[{'heading': '상담 안내', 'content': '공식 상담 절차'}]), \
             patch('app.rag.rerank', new=AsyncMock(return_value=([{'index': 0, 'relevance_score': .9}], {'model': 'reranker'}))), \
             patch('app.rag.generate_answer', new=AsyncMock(side_effect=RagError('test-stop', 'test-stop'))) as generator:
            with self.assertRaises(RagError):
                asyncio.run(recommend('층간소음', '어디에 신청하나요?', conversation=history))
            query = embedder.await_args.args[0][0]
            self.assertIn('아파트에서 층간소음', query)
            self.assertIn('어디에 신청하나요?', query)
            self.assertNotIn('검증되지 않은 주장', query)
            self.assertEqual(generator.await_args.kwargs['conversation'], history)

    def test_generated_answer_rejects_invented_or_missing_citations(self):
        import json
        evidence = [{'document_title': 'FAQ', 'publisher': '공공기관', 'kind': 'FAQ', 'version': '2026', 'jurisdiction': '전국', 'retrieved_at': '2026-10-07', 'locator': 'Q1', 'content': '공식 안내'}]
        for ids in ([99], []):
            payload = {'message': {'content': json.dumps({'answer_status': 'answered', 'answer': '답변', 'checks': [], 'next_steps': [], 'citation_ids': ids})}}
            with patch('app.rag_models.post_json', new=AsyncMock(return_value=payload)):
                with self.assertRaises(RagError):
                    asyncio.run(generate_answer('제목', '내용', evidence))
