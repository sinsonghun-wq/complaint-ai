import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from fetch_rag_sources import parse_official_html


class OfficialSourceParsingTests(unittest.TestCase):
    def test_reply_excludes_question_that_is_not_a_factual_answer(self):
        html = '''<div id="contentBody">동영상도 인정되나요?<p>【질의요지】동영상도 인정되나요?</p>
        <p>【회답】증거 확보를 위해 안전신문고 앱을 통해 사진 촬영과 신고가 이루어집니다. 이 회신의 적용 조건을 확인하세요.</p>
        <p>【관련법령】도로교통법</p></div>'''.encode()
        sections = parse_official_html(html, 'interpretation')
        self.assertNotIn('동영상도 인정되나요', sections[0]['text'])
        self.assertIn('안전신문고', sections[0]['text'])
        self.assertIn('【관련법령】', sections[0]['text'])

    def test_listing_or_challenge_is_not_a_reply(self):
        with self.assertRaises(ValueError):
            parse_official_html(b'<div id="contentBody">Please verify visitor</div>', 'interpretation')
