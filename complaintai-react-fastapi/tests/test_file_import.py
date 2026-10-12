"""Regression tests for encoding detection and imports larger than one batch."""
import codecs
from contextlib import nullcontext
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from fastapi import HTTPException
from openpyxl import Workbook

from app.main import app, csv_encoding, csv_rows, spreadsheet_records, _records_from_case_pages, _records_from_hwp_text
from app.security import actor_from_auth
from app.db import connection


class FileImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="file-import-test-", dir=Path(__file__).resolve().parents[1] / "data")
        self.path = Path(self.temp.name)
        self.previous = dict(app.dependency_overrides)
        app.dependency_overrides[actor_from_auth] = lambda: {"role": "admin", "department": "교통·국토"}
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self.previous)
        self.temp.cleanup()

    def test_utf8_sample_boundary_and_bom(self):
        for prefix in (b"", codecs.BOM_UTF8):
            path = self.path / "boundary.csv"
            path.write_bytes(prefix + b"a" * (65535 - len(prefix)) + "한글".encode("utf-8"))
            self.assertEqual(csv_encoding(path), "utf-8-sig")
            self.assertEqual(len(list(csv_rows(path, csv_encoding(path)))), 0)

    def test_cp949(self):
        path = self.path / "cp949.csv"
        path.write_bytes("제목,내용\n도로,도로 보수 요청\n".encode("cp949"))
        self.assertEqual(csv_encoding(path), "cp949")
        self.assertEqual(len(list(csv_rows(path, csv_encoding(path)))), 1)

    def test_invalid_csv_returns_readable_422(self):
        path = self.path / "invalid.csv"
        path.write_bytes(codecs.BOM_UTF8 + b"title,content\n\xff,broken")
        with patch("app.main.save_upload", new=AsyncMock(return_value=(path, ".csv"))):
            result = self.client.post("/api/imports", files={"file": ("invalid.csv", b"invalid", "text/csv")})
        self.assertEqual(result.status_code, 422)
        self.assertIn("UTF-8", result.json()["detail"])
        self.assertFalse(path.exists())

    def test_xlsx_reads_all_sheets_and_intake_preserves_source(self):
        path = self.path / "many.xlsx"
        workbook = Workbook()
        for index in range(2):
            sheet = workbook.active if index == 0 else workbook.create_sheet()
            sheet.append(["민원 제목", "민원 내용"])
            for number in range(600):
                sheet.append([f"도로 {index}-{number}", "도로에 구멍이 있습니다. 보수가 필요합니다."])
        workbook.save(path)
        self.assertEqual(len(spreadsheet_records(path, ".xlsx")), 1200)
        with patch("app.main.save_upload", new=AsyncMock(return_value=(path, ".xlsx"))):
            result = self.client.post("/api/intake", files={"file": ("many.xlsx", b"test")})
        self.assertEqual(result.status_code, 200)
        records = result.json()["complaints"]
        self.assertEqual(len(records), 1200)
        self.assertTrue(all(record["source_file"] == "many.xlsx" for record in records))

    def test_pdf_and_hwp_split_more_than_500_cases(self):
        pages = [(f"사례 {i:03d}\n도로 민원 {i}\n신청원인\n도로를 보수해 주세요.", 1.0, 0.0) for i in range(1, 602)]
        self.assertEqual(len(_records_from_case_pages(pages, "many.pdf")), 601)
        text = "\n".join(f"02 국토 / 사례 {i:03d}\n도로 민원 {i}\n신청원인\n도로를 보수해 주세요." for i in range(1, 602))
        self.assertEqual(len(_records_from_hwp_text(text, "many.hwp")), 601)

    def test_batch_saves_every_record_in_500_row_chunks(self):
        save = AsyncMock(side_effect=lambda records, actor: (len(records), 0))
        with patch("app.main.save_records", new=save):
            result = self.client.post("/api/complaints/batch", json={"complaints": [{"title": "민원", "content": "도로 보수 요청"}] * 1201})
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.json()["saved"], 1201)
        self.assertEqual([len(call.args[0]) for call in save.await_args_list], [500, 500, 201])

    def test_global_test_delete_archives_all_statuses_without_touching_real_data(self):
        # PostgreSQL session-local tables shadow real tables and vanish on close.
        with connection() as db:
            with db.cursor() as cur:
                cur.execute("CREATE TEMP TABLE complaints(id INT PRIMARY KEY, category TEXT, complaint_status TEXT, cancelled_at TIMESTAMPTZ, cancelled_by_role TEXT, cancellation_reason TEXT, status_updated_at TIMESTAMPTZ, deleted_at TIMESTAMPTZ)")
                cur.execute("CREATE TEMP TABLE complaint_responses(complaint_id INT, response_state TEXT)")
                for identifier, status in enumerate(["접수", "진행중", "완료", "취소"], 1):
                    cur.execute("INSERT INTO complaints(id,category,complaint_status) VALUES(%s,%s,%s)", (identifier, "교통·국토" if identifier == 1 else "환경·위생", status))
                    cur.execute("INSERT INTO complaint_responses VALUES(%s,%s)", (identifier, "sent" if status == "완료" else "draft"))
            db.commit()
            with patch("app.main.connection", side_effect=lambda: nullcontext(db)):
                result = self.client.delete("/api/complaints/department/all")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["deleted"], 4)
            with db.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS count FROM complaints WHERE deleted_at IS NULL")
                self.assertEqual(cur.fetchone()["count"], 0)
                cur.execute("SELECT COUNT(*) AS count FROM complaints WHERE deleted_at IS NOT NULL AND complaint_status='취소'")
                self.assertEqual(cur.fetchone()["count"], 4)
                cur.execute("SELECT response_state FROM complaint_responses")
                self.assertEqual(cur.fetchall(), [{"response_state": "sent"}])

    def test_permanent_all_requires_password_and_deletes_all_departments(self):
        with connection() as db:
            with db.cursor() as cur:
                cur.execute("CREATE TEMP TABLE complaints(id INT PRIMARY KEY, category TEXT, complaint_status TEXT, deleted_at TIMESTAMPTZ)")
                cur.execute("INSERT INTO complaints VALUES(1,'교통·국토','취소',NOW()),(2,'교통·국토','완료',NOW()),(3,'환경·위생','취소',NOW()),(4,'교통·국토','접수',NULL)")
            db.commit()
            with patch("app.main.connection", side_effect=lambda: nullcontext(db)), patch("app.main.check_password") as check:
                result = self.client.request("DELETE", "/api/complaints/deleted/all", json={"password": "test-password"})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["permanently_deleted"], 3)
            self.assertEqual(check.call_args.args[1], "test-password")
            with db.cursor() as cur:
                cur.execute("SELECT id FROM complaints ORDER BY id")
                self.assertEqual(cur.fetchall(), [{"id": 4}])

    def test_permanent_all_rejects_regular_users(self):
        app.dependency_overrides[actor_from_auth] = lambda: {"role": "user"}
        with patch("app.main.connection") as connect:
            result = self.client.request("DELETE", "/api/complaints/deleted/all", json={"password": "test-password"})
        self.assertEqual(result.status_code, 403)
        connect.assert_not_called()

    def test_permanent_all_stops_before_delete_on_wrong_password(self):
        with patch("app.main.check_password", side_effect=HTTPException(403, "비밀번호가 일치하지 않습니다.")), patch("app.main.connection") as connect:
            result = self.client.request("DELETE", "/api/complaints/deleted/all", json={"password": "wrong-password"})
        self.assertEqual(result.status_code, 403)
        connect.assert_not_called()
