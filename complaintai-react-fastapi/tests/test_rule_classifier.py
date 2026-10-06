"""Rule scoring and hybrid routing tests; no real model or application writes."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.ai import fallback, prompt
from app.main import analyze_csv_batch, insert_complaint, save_records, should_use_import_llm


class ContextRuleTests(unittest.TestCase):
    def result(self, content):
        return fallback('', content)

    def test_air_context_excludes_waiting(self):
        for text in ['예약 대기 시간이 너무 길어서 개선을 요청합니다.', '대기 순번을 확인할 수 없어 안내를 요청합니다.']:
            result = self.result(text)
            self.assertEqual(result['rule_decision']['scores']['환경·위생'], 0)
            self.assertEqual(result['category'], '기타')
        for text in ['대기오염을 줄이는 조치를 요청합니다.', '대기 질 측정 결과를 확인해 주세요.']:
            result = self.result(text)
            self.assertEqual(result['category'], '환경·위생')
            self.assertGreaterEqual(result['rule_decision']['top_score'], 3)

    def test_sidewalk_context_excludes_delivery_and_country(self):
        for text in ['상품 인도 기한을 지키지 않아 조치를 요청합니다.', '인도네시아에 대한 안내를 요청합니다.', '인도 관련 사항을 확인해 주세요.']:
            self.assertEqual(self.result(text)['rule_decision']['scores']['교통·국토'], 0)
        result = self.result('인도가 파손되어 보행자들의 통행이 불편합니다.')
        self.assertEqual(result['category'], '교통·국토')
        self.assertIn('인도', result['keywords'])

    def test_later_valid_context_is_not_lost(self):
        text = '상품 인도 기한 문의입니다. ' + '관련 설명입니다. ' * 12 + '보행자를 위한 인도의 파손을 고쳐주세요.'
        self.assertIn('인도', self.result(text)['keywords'])

    def test_public_company_is_not_construction(self):
        result = self.result('한국관광공사에 관광 안내 서비스 개선을 요청합니다.')
        self.assertEqual(result['rule_decision']['scores']['교통·국토'], 0)
        self.assertEqual(result['category'], '문화·행정·안전')

    def test_floor_noise_has_specific_housing_priority(self):
        for text in ['층간소음으로 잠을 못 자니 해결해 주세요.', '층간 소음 관련 문제를 확인하고 조치해 주세요.']:
            result = self.result(text)
            self.assertEqual(result['category'], '주택·건축')
            self.assertEqual(result['rule_decision']['scores']['주택·건축'], 3)
            self.assertEqual(result['rule_decision']['scores']['환경·위생'], 0)
            self.assertFalse(result['rule_decision']['requires_llm'])

    def test_factory_and_construction_noise_are_environment(self):
        for text in ['공사장에서 나는 소음으로 잠을 못 자니 조치해 주세요.', '공장에서 발생하는 소음에 대한 조치를 요청합니다.']:
            result = self.result(text)
            self.assertEqual(result['category'], '환경·위생')
            self.assertGreaterEqual(result['rule_decision']['top_score'], 3)

    def test_general_noise_is_weak_and_requires_llm(self):
        result = self.result('계속 발생하는 소음 때문에 생활이 불편합니다.')
        self.assertEqual(result['rule_decision']['top_score'], 1)
        self.assertTrue(result['rule_decision']['requires_llm'])
        self.assertTrue(result['needs_review'])

    def test_specific_and_general_deposits_are_different(self):
        for text in ['임대 보증금을 돌려받지 못하여 반환을 요청합니다.', '전세보증금 반환 문제를 해결해 주세요.']:
            result = self.result(text)
            self.assertEqual(result['category'], '주택·건축')
            self.assertEqual(result['rule_decision']['top_score'], 3)
            self.assertFalse(result['rule_decision']['requires_llm'])
        result = self.result('계약 보증금을 돌려받지 못하여 반환을 요청합니다.')
        self.assertTrue(result['rule_decision']['requires_llm'])
        self.assertIn('ambiguous_deposit', result['rule_decision']['reasons'])

    def test_space_variants_prefixes_and_repetition_do_not_inflate_score(self):
        result = self.result('고용 지원금 고용지원금 고용 지원금에 대한 안내를 요청합니다.')
        self.assertEqual(result['rule_decision']['top_score'], 3)
        self.assertEqual(len(result['keywords']), 1)
        repeated = self.result('버스 버스 버스 버스 관련 안내를 요청합니다.')
        self.assertEqual(repeated['rule_decision']['top_score'], 1)
        self.assertTrue(repeated['rule_decision']['requires_llm'])

    def test_positive_tie_requires_llm(self):
        result = self.result('버스 문제와 아파트 문제에 대한 조치를 요청합니다.')
        self.assertEqual(result['rule_decision']['margin'], 0)
        self.assertIn('tie', result['rule_decision']['reasons'])
        self.assertTrue(result['rule_decision']['requires_llm'])

    def test_one_point_margin_requires_llm(self):
        result = self.result('버스 택시 운전 문제와 아파트 누수 문제를 해결해 주세요.')
        self.assertEqual(result['rule_decision']['top_score'], 3)
        self.assertEqual(result['rule_decision']['margin'], 1)
        self.assertIn('small_margin', result['rule_decision']['reasons'])
        self.assertTrue(result['rule_decision']['requires_llm'])

    def test_clear_multiple_signals_skip_llm(self):
        result = self.result('버스 택시 운전 문제를 확인하고 해결해 주세요.')
        self.assertEqual(result['rule_decision']['top_score'], 3)
        self.assertFalse(result['rule_decision']['requires_llm'])

    def test_llm_prompt_uses_same_boundary_policy(self):
        text = prompt('', '안내 요청입니다.')
        self.assertIn('층간소음은 주택·건축', text)
        self.assertIn('상품 인도는 보행 공간이 아니다', text)

    def test_scoring_evidence_is_saved_as_metadata_without_schema_change(self):
        record = self.result('버스 문제와 아파트 문제에 대한 조치를 요청합니다.')
        conn = MagicMock()
        self.assertTrue(insert_complaint(conn, record, 'test.csv', 1, None))
        metadata = json.loads(conn.cursor().__enter__().execute.call_args.args[1][11])
        self.assertTrue(metadata['rule_decision']['requires_llm'])
        self.assertIn('confidence', metadata)


class HybridRoutingTests(unittest.IsolatedAsyncioTestCase):
    def record(self, content, row=1):
        return {'title': '', 'content': content, 'source_row': row}

    async def test_tie_weak_and_small_margin_are_sent_to_llm(self):
        records = [self.record(text, i) for i, text in enumerate([
            '버스 문제와 아파트 문제에 대한 조치를 요청합니다.',
            '버스 이용이 불편해서 조치를 요청합니다.',
            '버스 택시 운전 문제와 아파트 누수 문제를 해결해 주세요.',
        ], 1)]
        async def model(title, content):
            return {**fallback(title, content), 'processing_mode': 'llm', 'needs_review': False}
        with patch('app.main.LLM_IMPORT_ENABLED', True), patch('app.main.CSV_LLM_CONFIDENCE_THRESHOLD', 0), patch('app.main.analyze', new=AsyncMock(side_effect=model)) as llm:
            results = await analyze_csv_batch(records)
        self.assertEqual(llm.await_count, 3)
        self.assertTrue(all(result['processing_mode'] == 'llm' for result in results))
        self.assertEqual([result['source_row'] for result in results], [1, 2, 3])

    async def test_clear_rows_keep_rule_processing(self):
        with patch('app.main.LLM_IMPORT_ENABLED', True), patch('app.main.analyze', new=AsyncMock()) as llm:
            results = await analyze_csv_batch([self.record('버스 택시 운전 문제를 확인하고 해결해 주세요.')])
        llm.assert_not_awaited()
        self.assertEqual(results[0]['processing_mode'], 'rule')

    async def test_review_flag_does_not_block_short_signal(self):
        record = self.record('버스')
        rule = fallback('', '버스')
        self.assertTrue(rule['needs_review'])
        with patch('app.main.LLM_IMPORT_ENABLED', True):
            self.assertTrue(should_use_import_llm(record, rule, csv_mode=True))

    async def test_general_deposit_bypasses_long_other_shortcut(self):
        record = self.record('계약 보증금 반환을 요청합니다. ' + '관련 상황을 설명하고 조치를 요청합니다. ' * 10)
        with patch('app.main.LLM_IMPORT_ENABLED', True):
            self.assertTrue(should_use_import_llm(record, fallback('', record['content']), csv_mode=True))

    async def test_no_evidence_bulk_policy_is_preserved(self):
        record = self.record('관련 상황을 설명하고 조치를 요청합니다. ' * 10)
        with patch('app.main.LLM_IMPORT_ENABLED', True):
            rule = fallback('', record['content'])
            self.assertEqual(rule['category'], '기타')
            self.assertFalse(should_use_import_llm(record, rule, csv_mode=True))
            self.assertTrue(rule['needs_review'])

    async def test_unavailable_llm_keeps_review_evidence(self):
        record = self.record('버스 문제와 아파트 문제에 대한 조치를 요청합니다.')
        failed = {**fallback('', record['content']), 'reason': 'LLM 연결 실패', 'llm_attempted': True}
        with patch('app.main.LLM_IMPORT_ENABLED', True), patch('app.main.analyze', new=AsyncMock(return_value=failed)):
            result = (await analyze_csv_batch([record]))[0]
        self.assertEqual(result['processing_mode'], 'rule')
        self.assertTrue(result['needs_review'])
        self.assertTrue(result['llm_attempted'])
        self.assertEqual(result['reason'], 'LLM 연결 실패')

    async def test_disabled_llm_keeps_review_not_false_confirmation(self):
        record = self.record('버스 문제와 아파트 문제에 대한 조치를 요청합니다.')
        with patch('app.main.LLM_IMPORT_ENABLED', False), patch('app.main.analyze', new=AsyncMock()) as llm:
            result = (await analyze_csv_batch([record]))[0]
        llm.assert_not_awaited()
        self.assertTrue(result['needs_review'])

    async def test_document_import_uses_llm_and_does_not_override_its_category(self):
        for extension in ['pdf', 'hwp', 'xlsx']:
            item = self.record('버스 문제와 아파트 문제에 대한 조치를 요청합니다.')
            rule = fallback('', item['content'])
            item.update(category=rule['category'], source_file='test.' + extension)
            model = {**rule, 'category': '주택·건축', 'processing_mode': 'llm', 'needs_review': False}
            with patch('app.main.LLM_IMPORT_ENABLED', True), patch('app.main.connection', return_value=MagicMock()), patch('app.main.analyze', new=AsyncMock(return_value=model)) as llm, patch('app.main.embedding', new=AsyncMock(return_value=None)), patch('app.main.insert_complaint', return_value=True) as insert:
                self.assertEqual(await save_records([item], {'role': 'admin'}), (1, 0))
            llm.assert_awaited_once()
            self.assertEqual(insert.call_args.args[1]['category'], '주택·건축')
            self.assertEqual(insert.call_args.args[2], 'test.' + extension)

    async def test_import_concurrency_limit_is_respected(self):
        active = maximum = 0
        async def model(title, content):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0)
            active -= 1
            return {**fallback(title, content), 'processing_mode': 'llm'}
        records = [self.record('버스 이용이 불편해서 조치를 요청합니다.', i) for i in range(4)]
        with patch('app.main.LLM_IMPORT_ENABLED', True), patch('app.main.LLM_IMPORT_CONCURRENCY', 2), patch('app.main.analyze', new=model):
            await analyze_csv_batch(records)
        self.assertEqual(maximum, 2)
