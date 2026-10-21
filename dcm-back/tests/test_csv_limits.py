"""Exact field boundaries and logical-record recovery, including quoted newlines."""
import csv
import io
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import csv_reader
from app.services.csv_reader import CSV_MAX_FIELD_CHARS, CsvRecordReader, CsvRowError
from app.services.parser import csv_preview, csv_row_counts


class CsvLimitTests(unittest.TestCase):
    def read(self, rows, newline="\r\n"):
        source = io.StringIO(newline="")
        writer = csv.writer(source, lineterminator=newline)
        writer.writerow(["제목", "본문", "벡터"])
        writer.writerows(rows)
        source.seek(0)
        return list(CsvRecordReader(source))

    def test_boundary_is_characters_not_utf8_bytes(self):
        self.assertEqual(CSV_MAX_FIELD_CHARS, 2_000_000)
        for length in (100_000, 131_073, 2_000_000):
            with self.subTest(length=length):
                rows = self.read([["정상", "한" * length, "[]"]])
                self.assertNotIsInstance(rows[0], CsvRowError)
                self.assertEqual(len(rows[0]["본문"]), length)

    def test_multiline_escaped_quotes_and_following_rows_survive_overflow(self):
        large = '앞부분,"인용"\n' + "한" * 2_000_001 + '\r\n뒷부분,"다음"\n마지막'
        for newline in ("\n", "\r\n", "\r"):
            with self.subTest(newline=newline):
                rows = self.read([["제외", large, '[]'], ['정상 "다음"', '이어지는, 정상\n본문', '[]']], newline)
                self.assertEqual(len(rows), 2)
                self.assertIsInstance(rows[0], CsvRowError)
                self.assertIn("2,000,000", rows[0].reason)
                self.assertEqual(rows[1]["제목"], '정상 "다음"')
                self.assertEqual(rows[1]["본문"], '이어지는, 정상\n본문')
                self.assertLess(len(str(rows[0])), 300)

    def test_limit_applies_to_unmapped_fields_and_counts_each_record_once(self):
        rows = self.read([["벡터 초과", "정상 본문", "1" * 2_000_001],
                          ["여러 필드 초과", "가" * 2_000_001, "2" * 2_000_001],
                          ["다음", "정상 본문", "[]"]])
        self.assertEqual([isinstance(row, CsvRowError) for row in rows], [True, True, False])
        self.assertEqual(rows[2]["제목"], "다음")

    def test_eof_without_newline_and_unquoted_literal_quote(self):
        source = io.StringIO('제목,본문\n제외"인용,' + 'x' * 2_000_001 + '\n정상,본문')
        rows = list(CsvRecordReader(source))
        self.assertEqual(len(rows), 2)
        self.assertIsInstance(rows[0], CsvRowError)
        self.assertEqual(rows[1], {"제목": "정상", "본문": "본문"})

    def test_preview_ignores_unreadable_samples_and_counts_include_them(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sample.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as source:
                writer = csv.writer(source)
                writer.writerow(["제목", "본문"])
                writer.writerow(["제외", "한" * 2_000_001])
                writer.writerow(["정상", "정상적으로 읽을 수 있는 본문입니다."])
            headers, samples = csv_preview(path, "utf-8-sig")
            self.assertEqual(headers, ["제목", "본문"])
            self.assertEqual(samples, [{"제목": "정상", "본문": "정상적으로 읽을 수 있는 본문입니다."}])
            self.assertEqual(csv_row_counts(path, "utf-8-sig"), (2, 1))

    def test_recovery_matches_standard_csv_for_varied_quoted_records(self):
        randomizer = random.Random(20261020)
        original = [["".join(randomizer.choices('ab한글 ,"\r\n', k=randomizer.randrange(110))) for _ in range(3)] for _ in range(300)]
        old_limit = csv.field_size_limit(64)
        try:
            with patch.object(csv_reader, "CSV_MAX_FIELD_CHARS", 64):
                actual = self.read(original)
        finally:
            csv.field_size_limit(old_limit)
        self.assertEqual(len(actual), len(original))
        for row, values in zip(actual, original):
            if max(map(len, values)) > 64:
                self.assertIsInstance(row, CsvRowError)
            else:
                self.assertEqual(row, dict(zip(["제목", "본문", "벡터"], values)))


if __name__ == "__main__":
    unittest.main()
