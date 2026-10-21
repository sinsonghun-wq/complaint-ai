import unittest
from unittest.mock import AsyncMock, patch

from app.services.file_rules import file_fallback
from app.services import imports


class FileRuleTests(unittest.TestCase):
    def test_context_avoids_false_keyword_matches(self):
        for text in ("예약 대기 시간이 너무 깁니다.", "상품 인도 기한을 지켜 주세요.", "인도네시아에 대해 알려 주세요."):
            result = file_fallback("", text)
            self.assertEqual(result["rule_decision"]["scores"]["교통·국토"], 0)
            self.assertEqual(result["rule_decision"]["scores"]["환경·위생"], 0)

    def test_specific_noise_and_deposit_rules(self):
        for text, expected in (("층간 소음으로 잠을 못 잡니다.", "주택·건축"),
                               ("전세 보증금을 돌려주세요.", "주택·건축"),
                               ("공장에서 발생하는 소음에 조치를 요청합니다.", "환경·위생")):
            result = file_fallback("", text)
            self.assertEqual(result["category"], expected)
            self.assertFalse(result["rule_decision"]["requires_llm"])

    def test_repetition_does_not_inflate_confidence(self):
        result = file_fallback("", "버스 버스 버스 버스가 불편합니다.")
        self.assertEqual(result["rule_decision"]["top_score"], 1)
        self.assertTrue(result["needs_review"])

    def test_ambiguous_contract_deposit_requires_review(self):
        result = file_fallback("", "계약 보증금을 반환해 주세요.")
        self.assertIn("ambiguous_deposit", result["rule_decision"]["reasons"])


class FileHybridTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.record = {"title": "", "content": "버스 문제와 아파트 문제에 조치를 요청합니다."}
        self.old_cooldown = imports._llm_unavailable_until
        imports._llm_unavailable_until = 0

    async def asyncTearDown(self):
        imports._llm_unavailable_until = self.old_cooldown

    async def test_disabled_llm_preserves_review_and_rule_evidence(self):
        with patch.object(imports, "LLM_IMPORT_ENABLED", False), patch.object(imports, "analyze", new=AsyncMock()) as model:
            result = await imports.analyze_import_record(self.record)
        model.assert_not_awaited()
        self.assertTrue(result["needs_review"])
        self.assertFalse(result["llm_attempted"])
        self.assertTrue(result["rule_decision"]["requires_llm"])

    async def test_failed_model_does_not_erase_rule_review(self):
        failed = {**file_fallback("", self.record["content"]), "processing_mode": "fallback", "reason": "연결 실패"}
        with patch.object(imports, "LLM_IMPORT_ENABLED", True), patch.object(imports, "analyze", new=AsyncMock(return_value=failed)):
            result = await imports.analyze_import_record(self.record)
        self.assertEqual(result["processing_mode"], "rule")
        self.assertTrue(result["needs_review"])
        self.assertTrue(result["llm_attempted"])
        self.assertEqual(result["reason"], "연결 실패")
