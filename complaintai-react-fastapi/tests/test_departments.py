import unittest
from unittest.mock import AsyncMock, patch

from app.ai import CATEGORIES, analyze, fallback, prompt
from app.department_migration import migrate_category


class DepartmentClassificationTests(unittest.TestCase):
    def test_nine_department_rules(self):
        examples = [
            ('노동', '근로계약을 체결했지만 임금 체불과 부당 해고가 발생했습니다.'),
            ('기업', '사업자 판매 상품의 환불을 거부한 소비자 분쟁입니다.'),
            ('교통', '버스 배차와 교통신호 개선 및 불법 주차 단속을 요청합니다.'),
            ('주택·건축', '아파트 주택의 건물 누수와 건축 하자를 처리해 주세요.'),
            ('환경·위생', '쓰레기 폐기물 악취와 환경 오염을 처리해 주세요.'),
            ('건설·국토', '도로 포트홀과 교량 보수 토목 공사를 요청합니다.'),
            ('문화·행정·안전', '축제 문화 행사와 소방 화재 안전사고 대책을 요청합니다.'),
            ('보건·복지', '노인 돌봄 복지급여와 의료 보건 지원을 요청합니다.'),
            ('기타', '안녕하세요. 문의합니다.'),
        ]
        self.assertEqual(len(CATEGORIES), 9)
        for expected, content in examples:
            with self.subTest(department=expected):
                self.assertEqual(fallback('', content)['category'], expected)
                self.assertIn(expected, prompt('', content))

    def test_old_department_handover(self):
        self.assertEqual(migrate_category('국토·교통', '', '버스 주차 교통신호'), '교통')
        self.assertEqual(migrate_category('국토·교통', '', '도로 포트홀 교량 공사'), '건설·국토')
        self.assertEqual(migrate_category('소방'), '문화·행정·안전')
        self.assertEqual(migrate_category('행정·안전'), '문화·행정·안전')
        self.assertEqual(migrate_category('주택건축'), '주택·건축')
        self.assertEqual(migrate_category('보건복지'), '보건·복지')
        self.assertEqual(migrate_category('기타', '', '임금 체불'), '노동')
        self.assertIsNone(migrate_category(None))

    async def _model_result(self, category):
        import httpx
        response = httpx.Response(200, json={'message': {'content': __import__('json').dumps({'category': category, 'summary': '민원 요약입니다.'})}}, request=httpx.Request('POST', 'http://test.local'))
        client = AsyncMock()
        client.post.return_value = response
        with patch('app.ai.httpx.AsyncClient') as factory:
            factory.return_value.__aenter__ = AsyncMock(return_value=client)
            factory.return_value.__aexit__ = AsyncMock(return_value=False)
            return await analyze('', '버스 배차와 교통신호를 개선해 주세요.')

    def test_model_accepts_new_and_rejects_old_departments(self):
        import asyncio
        self.assertEqual(asyncio.run(self._model_result('교통'))['processing_mode'], 'llm')
        old = asyncio.run(self._model_result('국토·교통'))
        self.assertEqual(old['processing_mode'], 'fallback')
        self.assertEqual(old['category'], '교통')
