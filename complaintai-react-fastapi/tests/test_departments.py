import unittest
from unittest.mock import AsyncMock, patch

from app.ai import CATEGORIES, analyze, fallback, prompt
from app.department_merge import DEPARTMENT_MERGES, merge_category


class DepartmentClassificationTests(unittest.TestCase):
    def test_added_department_keywords(self):
        examples = {
            '노동·기업': ['고용 지원금', '고용지원금', '취업', '휴직', '실업', '창업', '회계', '세무', '경영전략', '경영 전략'],
            '교통·국토': ['자동차', '지하철', '항공', '부동산', '건설현장 품질', '건설현장 안전', '도시계획', '개발행위', '건설업등록'],
            '주택·건축': ['건축 방화', '건축 피난', '건축 구조', '공동주택'],
            '환경·위생': ['공원', '녹지', '가로수', '대기질', '동물', '반려', '상하수도', '하천'],
            '문화·행정·안전': ['체육', '시설이용', '교육', '청소년', '세금', '지역화폐', '관광'],
            '보건·복지': ['아동', '보육', '여성가족', '감염병', '방역'],
        }
        for department, terms in examples.items():
            for term in terms:
                with self.subTest(department=department, keyword=term):
                    self.assertEqual(fallback('', f'{term} 관련 사항을 확인하고 조치해 주세요.')['category'], department)

    def test_seven_department_rules(self):
        examples = [
            ('노동·기업', '근로계약을 체결했지만 임금 체불과 부당 해고가 발생했습니다.'),
            ('노동·기업', '사업자 판매 상품의 환불을 거부한 소비자 분쟁입니다.'),
            ('교통·국토', '버스 배차와 교통신호 개선 및 불법 주차 단속을 요청합니다.'),
            ('주택·건축', '아파트 주택의 건물 누수와 건축 하자를 처리해 주세요.'),
            ('환경·위생', '쓰레기 폐기물 악취와 환경 오염을 처리해 주세요.'),
            ('교통·국토', '도로 포트홀과 교량 보수 토목 공사를 요청합니다.'),
            ('문화·행정·안전', '축제 문화 행사와 소방 화재 안전사고 대책을 요청합니다.'),
            ('보건·복지', '노인 돌봄 복지급여와 의료 보건 지원을 요청합니다.'),
            ('기타', '안녕하세요. 문의합니다.'),
        ]
        self.assertEqual(len(CATEGORIES), 7)
        for expected, content in examples:
            with self.subTest(department=expected):
                self.assertEqual(fallback('', content)['category'], expected)
                self.assertIn(expected, prompt('', content))

    def test_old_department_handover(self):
        for old, new in DEPARTMENT_MERGES.items():
            self.assertEqual(merge_category(old), new)
        for category in CATEGORIES:
            self.assertEqual(merge_category(category), category)
        self.assertIsNone(merge_category(None))

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
        for department in CATEGORIES:
            self.assertEqual(asyncio.run(self._model_result(department))['processing_mode'], 'llm')
        for category in DEPARTMENT_MERGES:
            old = asyncio.run(self._model_result(category))
            self.assertEqual(old['processing_mode'], 'fallback')
            self.assertEqual(old['category'], '교통·국토')
