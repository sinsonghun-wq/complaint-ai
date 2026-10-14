"""Long search context regression; no model loading, networking or database."""
import math
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import torch
from fastapi import HTTPException

from rag_service import reranker


class Tokenizer:
    def encode(self, text, **kwargs):
        return [ord(char) for char in text]

    def __call__(self, texts, max_length, **kwargs):
        return {'input_ids': [self.encode(text)[:max_length] for text in texts]}

    def pad(self, inputs, **kwargs):
        return {'input_ids': torch.tensor(inputs['input_ids'])}


class RerankerContextTests(unittest.TestCase):
    def test_search_covers_trailing_relevance_and_merges_segments_by_original_document(self):
        def model(input_ids, **kwargs):
            texts = [''.join(chr(token) for token in row) for row in input_ids.tolist()]
            logits = [[[0.0, math.log(9) if 'UNIQUE_MATCH' in text else -math.log(19)]] for text in texts]
            return SimpleNamespace(logits=torch.tensor(logits))

        body = reranker.Body(query='query', documents=['irrelevant ' * 50 + 'UNIQUE_MATCH', 'another subject'],
                             instruction='same problem', strict_context=True)
        fake = {'tokenizer': Tokenizer(), 'model': model, 'prefix': [], 'suffix': [], 'yes': 1, 'no': 0, 'revision': 'test'}
        with patch.object(reranker, 'MAX_LENGTH', 128), patch.object(reranker, 'BATCH_SIZE', 1), patch.dict(reranker.state, fake, clear=True):
            rows, owners = reranker.document_inputs(body, fake['tokenizer'], [], [])
            self.assertGreater(owners.count(0), 1)
            self.assertTrue(all(len(row) <= 128 for row in rows))
            result = reranker.rerank(body)
        self.assertEqual([item['index'] for item in result['results']], [0, 1])
        self.assertAlmostEqual(result['results'][0]['relevance_score'], .9, places=5)
        self.assertAlmostEqual(result['results'][1]['relevance_score'], .05, places=5)

    def test_query_too_long_is_rejected_instead_of_silently_truncated(self):
        body = reranker.Body(query='q' * 300, documents=['relevant'], instruction='same problem', strict_context=True)
        with patch.object(reranker, 'MAX_LENGTH', 128):
            with self.assertRaises(HTTPException) as error:
                reranker.document_inputs(body, Tokenizer(), [], [])
        self.assertEqual(error.exception.status_code, 400)

    def test_default_official_request_keeps_its_existing_instruction_and_token_recipe(self):
        body = reranker.Body(query='question', documents=['official guidance'])
        tokenizer = Tokenizer()
        expected = tokenizer.encode(f'<Instruct>: {reranker.OFFICIAL_INSTRUCTION}\n<Query>: question\n<Document>: official guidance')
        rows, owners = reranker.document_inputs(body, tokenizer, [1], [2])
        self.assertEqual(rows, [[1] + expected + [2]])
        self.assertEqual(owners, [0])


if __name__ == '__main__':
    unittest.main()
