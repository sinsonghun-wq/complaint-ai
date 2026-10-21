"""Input errors must not trip the circuit breaker for subsequent RAG rows."""
import json
import unittest
from unittest.mock import patch

import httpx

from app import ai


class EmbeddingErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_overlong_row_then_valid_row_preserves_the_real_error_and_continues(self):
        calls = []
        def handler(request):
            payload = json.loads(request.content)
            calls.append(payload)
            if payload['input'] == 'overlong':
                return httpx.Response(413, json={'error': 'the input length exceeds the context length'})
            return httpx.Response(200, json={'embeddings': [[1.0] + [0.0] * 1023]})
        client = httpx.AsyncClient
        def factory(**kwargs):
            return client(transport=httpx.MockTransport(handler), **kwargs)
        with patch.object(ai.httpx, 'AsyncClient', side_effect=factory), patch.object(ai, 'EMBEDDING_PROVIDER', 'ollama'), patch.object(ai, '_embedding_unavailable_until', float('inf')):
            with self.assertRaisesRegex(ai.EmbeddingError, 'HTTP 413'):
                await ai.embeddingForReference('overlong')
            vector = await ai.embeddingForReference('valid row after failure')
        self.assertEqual(len(vector), 1024)
        self.assertEqual(len(calls), 2)
        self.assertFalse(calls[0]['truncate'])
        self.assertEqual(calls[0]['options']['num_ctx'], 8192)
        self.assertEqual(calls[0]['options']['num_batch'], 8192)

    async def test_bad_response_is_not_misreported_as_a_dimension_change(self):
        client = httpx.AsyncClient
        def factory(**kwargs):
            return client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'embeddings': [None]})), **kwargs)
        with patch.object(ai.httpx, 'AsyncClient', side_effect=factory), patch.object(ai, 'EMBEDDING_PROVIDER', 'ollama'):
            with self.assertRaisesRegex(ai.EmbeddingError, '벡터를 반환하지 않았습니다'):
                await ai.embeddingForReference('body')


if __name__ == '__main__':
    unittest.main()
