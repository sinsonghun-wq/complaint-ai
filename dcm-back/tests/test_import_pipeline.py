"""File ingestion regression tests. DB tests use a disposable database only."""
import asyncio
import codecs
import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from fastapi import HTTPException
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.main import app
from app.db import connection
from app.security import actor_from_auth
from app.settings import DATABASE_URL
from app.services import imports, parser
from app.services.complaint import ImportVectorPolicy, insert_complaint
from app.ai import fallback


class ParserRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_worker_no_longer_imports_web_application(self):
        result = subprocess.run([sys.executable, "-B", "-c", "import app.worker,sys; assert 'app.main' not in sys.modules"], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_dimension_mismatch_is_reported_without_repeating_unusable_vectors(self):
        embedder=AsyncMock(return_value=[0.1]*1536)
        policy=ImportVectorPolicy(embedder)
        async def verify():
            for i in range(10):
                record={"content":str(i)}
                self.assertIsNone(await policy.vector(record))
                self.assertIn("1536",record["_embedding_warning"])
        asyncio.run(verify())
        self.assertEqual(embedder.await_count,1)

    def test_worker_launch_error_is_503_not_connection_type_error(self):
        with patch.object(parser, "ensure_csv_worker", side_effect=RuntimeError("launch failed")), patch.object(parser, "connection") as connect:
            with self.assertRaises(HTTPException) as error:
                parser.start_import_worker("test")
            self.assertEqual(error.exception.status_code, 503)
            connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.execute.assert_called_once()

    def test_utf8_bom_boundary_cp949_and_trimmed_headers(self):
        for prefix in (b"", codecs.BOM_UTF8):
            p = self.root / "boundary.csv"
            p.write_bytes(prefix + b"a" * (65535-len(prefix)) + "한글".encode())
            self.assertEqual(parser.csv_encoding(p), "utf-8-sig")
        p = self.root / "cp949.csv"
        p.write_bytes(" 제목 , 내용 \n보수,도로를 보수해주세요\n".encode("cp949"))
        headers, sample = parser.csv_preview(p, parser.csv_encoding(p))
        self.assertEqual(headers, ["제목", "내용"])
        self.assertEqual(sample[0], next(parser.csv_rows(p, parser.csv_encoding(p))))

    def test_xlsx_all_sheets_individual_rows_over_500(self):
        p = self.root / "many.xlsx"
        book = Workbook()
        book.active.title = "안내"
        book.active.append(["안내 시트입니다"])
        for sheet_number in range(2):
            sheet = book.create_sheet(f"자료 {sheet_number}")
            sheet.append(["자료 안내"])
            sheet.append(["민원 제목", "신청원인"])
            for i in range(601):
                sheet.append([f"도로 보수 {sheet_number}-{i}", f"도로의 보수가 필요합니다. 사례 {sheet_number}-{i}"])
        book.save(p)
        records = parser.spreadsheet_records(p, ".xlsx")
        self.assertEqual(len(records), 1202)
        self.assertEqual(len({r["title"] for r in records}), 1202)

    def test_pdf_hwp_case_numbers_beyond_999(self):
        pages = [(f"사례 {i:03d}\n도로 민원 {i}\n신청원인\n도로 보수 요청 {i}", 1, 0) for i in range(1, 1202)]
        self.assertEqual(len(parser._records_from_case_pages(pages, "many.pdf")), 1201)
        text = "\n".join(f"02 국토 / 사례 {i:03d}\n도로 민원 {i}\n신청원인\n도로 보수 요청 {i}" for i in range(1,1202))
        self.assertEqual(len(parser._records_from_hwp_text(text, "many.hwp")), 1201)

    def test_invalid_file_is_readable_422_and_cleans_upload(self):
        p = self.root / "broken.pdf"
        p.write_bytes(b"not a PDF")
        previous = dict(app.dependency_overrides)
        app.dependency_overrides[actor_from_auth] = lambda: {"role": "admin"}
        try:
            with patch("app.routers.complaints.save_upload", new=AsyncMock(return_value=(p,".pdf"))):
                response = TestClient(app).post("/api/intake", files={"file":("broken.pdf",b"broken")})
            self.assertEqual(response.status_code,422)
            self.assertFalse(p.exists())
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous)

    def test_batch_route_preserves_500_row_chunks(self):
        previous = dict(app.dependency_overrides)
        app.dependency_overrides[actor_from_auth] = lambda: {"role": "admin"}
        save = AsyncMock(side_effect=lambda rows, actor: (len(rows), 0))
        try:
            with patch("app.routers.complaints.save_records", new=save):
                response = TestClient(app).post("/api/complaints/batch", json={"complaints":[{"title":"민원","content":"본문"}]*1201})
            self.assertEqual(response.status_code,201,response.text)
            self.assertEqual([len(call.args[0]) for call in save.await_args_list],[500,500,201])
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous)

    def test_header_llm_timeout_still_queues_known_csv_headers(self):
        p=self.root/"known.csv"
        p.write_text("제목,내용\n보수,"+"도로와 교통 시설에 대한 민원입니다. "*20+"\n",encoding="utf-8-sig")
        previous=dict(app.dependency_overrides)
        app.dependency_overrides[actor_from_auth]=lambda:{"role":"admin","sub":str(uuid.uuid4()),"department_id":2}
        try:
            with patch("app.routers.complaints.save_upload",new=AsyncMock(return_value=(p,".csv"))),patch("app.routers.complaints.infer_csv_mapping",new=AsyncMock(side_effect=TimeoutError)),patch("app.routers.complaints.fetch_one",return_value=None),patch("app.routers.complaints.connection"),patch("app.routers.complaints.start_import_worker"):
                result=TestClient(app).post("/api/imports",files={"file":("known.csv",b"test")})
            self.assertEqual(result.status_code,202,result.text)
            self.assertEqual(result.json()["status"],"queued")
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous)

    def test_ocr_passes_space_containing_paths_as_single_arguments(self):
        from PIL import Image
        languages=self.root/"languages with spaces"
        languages.mkdir()
        for lang in ("kor","eng"): (languages/f"{lang}.traineddata").touch()
        completed=MagicMock(returncode=0,stdout=b"recognized",stderr=b"")
        with patch.object(parser,"TESSDATA_DIR",languages),patch.object(parser,"TESSERACT_CMD","C:/tools with spaces/tesseract.exe"),patch.object(parser.subprocess,"run",return_value=completed) as execute:
            with Image.new("RGB",(10,10)) as image:
                self.assertEqual(parser.ocr_image(image),"recognized")
        args=execute.call_args.args[0]
        self.assertEqual(args[0],"C:/tools with spaces/tesseract.exe")
        self.assertEqual(args[args.index("--tessdata-dir")+1],str(languages))


class ImportDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = Path(__file__).resolve().parents[2]
        cls.name = "complaintai_import_test_"+uuid.uuid4().hex
        cls.url = make_conninfo(DATABASE_URL, dbname=cls.name)
        with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.name)))
        with psycopg.connect(cls.url) as db:
            db.execute((cls.repo/"dcm-back/tests/fixtures/init.sql").read_text(encoding="utf-8"))
            db.execute((cls.repo/"dcm-back/tests/fixtures/seed_demo_accounts.sql").read_text(encoding="utf-8"))
        cls.database_patch = patch("app.db.DATABASE_URL", cls.url)
        cls.database_patch.start()
        cls.temp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.database_patch.stop()
        cls.temp.cleanup()
        assert cls.name.startswith("complaintai_import_test_")
        assert cls.name != conninfo_to_dict(DATABASE_URL).get("dbname")
        with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.name)))

    def setUp(self):
        with connection() as db:
            db.execute("DELETE FROM complaints")
            db.execute("DELETE FROM import_jobs")

    def make_job(self, count):
        p = Path(self.temp.name)/"sample.csv"
        with p.open("w",encoding="utf-8-sig",newline="") as f:
            writer=csv.writer(f); writer.writerow(["제목","내용"])
            for i in range(count): writer.writerow([f"도로 {i}",f"도로와 버스, 교통 신호등을 보수해주세요. 번호 {i}"])
        with connection() as db:
            return db.execute("""INSERT INTO import_jobs(id,source_file,status,total_rows,storage_path,encoding,column_mapping)
                VALUES(%s,'sample.csv','processing',%s,%s,'utf-8-sig',%s::jsonb) RETURNING *""",
                (uuid.uuid4(),count,str(p),json.dumps({"title_column":"제목","content_columns":["내용"]}))).fetchone()

    def test_csv_1201_rows_checkpoint_progress_and_duplicate_handling(self):
        job=self.make_job(1201)
        updates=[]
        actual=imports.update_progress
        def progress(job_id,completed=None):
            if completed is not None: updates.append(completed)
            return actual(job_id,completed)
        with patch.object(imports,"embeddingForSelect",new=AsyncMock(return_value=None)),patch.object(imports,"LLM_IMPORT_ENABLED",False),patch.object(imports,"update_progress",side_effect=progress):
            imports.process_csv_job(job)
        with connection() as db:
            state=db.execute("SELECT * FROM import_jobs WHERE id=%s",(job["id"],)).fetchone()
            self.assertEqual((state["status"],state["completed_rows"],state["checkpoint_rows"],state["saved_rows"],state["failed_rows"]),("completed",1201,1201,1201,0))
            self.assertEqual(db.execute("SELECT count(*) AS n FROM complaints").fetchone()["n"],1201)
        self.assertEqual(updates[0],10)
        self.assertIn(500,updates)
        duplicate=self.make_job(1201)
        with patch.object(imports,"embeddingForSelect",new=AsyncMock(return_value=None)),patch.object(imports,"LLM_IMPORT_ENABLED",False): imports.process_csv_job(duplicate)
        with connection() as db:
            state=db.execute("SELECT * FROM import_jobs WHERE id=%s",(duplicate["id"],)).fetchone()
            self.assertEqual((state["saved_rows"],state["skipped_rows"]),(0,1201))

    def test_one_bad_database_row_does_not_rollback_other_rows(self):
        job=self.make_job(12)
        actual=imports.insert_complaint
        def insert(db,record,*args):
            if record["source_row"]==7:
                db.execute("SELECT 1/0")
            return actual(db,record,*args)
        with patch.object(imports,"insert_complaint",side_effect=insert),patch.object(imports,"embeddingForSelect",new=AsyncMock(return_value=None)),patch.object(imports,"LLM_IMPORT_ENABLED",False): imports.process_csv_job(job)
        with connection() as db:
            state=db.execute("SELECT * FROM import_jobs WHERE id=%s",(job["id"],)).fetchone()
            self.assertEqual((state["status"],state["saved_rows"],state["failed_rows"]),("completed",11,1))
            self.assertEqual(db.execute("SELECT count(*) AS n FROM import_failures WHERE job_id=%s",(job["id"],)).fetchone()["n"],1)

    def test_incompatible_optional_vector_never_discards_complaint(self):
        record=fallback("도로 보수","도로 보수가 필요합니다.")
        with connection() as db:
            self.assertTrue(insert_complaint(db,record,"sample.xlsx",1,None,[0.1]*1536))
            row=db.execute("SELECT embedding,analysis_metadata FROM complaints").fetchone()
            self.assertIsNone(row["embedding"])
            self.assertIn("embedding_warning",row["analysis_metadata"])

    def test_missing_source_is_failed_not_permanently_processing(self):
        job=self.make_job(1)
        Path(job["storage_path"]).unlink()
        imports.process_csv_job(job)
        with connection() as db:
            self.assertEqual(db.execute("SELECT status FROM import_jobs WHERE id=%s",(job["id"],)).fetchone()["status"],"failed")

    def test_unreadable_csv_rows_are_recorded_and_processing_continues(self):
        job = self.make_job(3)
        with Path(job["storage_path"]).open("w", encoding="utf-8-sig", newline="") as source:
            writer = csv.writer(source)
            writer.writerow(["제목", "내용"])
            writer.writerow(["제외할 긴 본문", '시작,"인용"\n' + "한" * 2_000_001 + '\n끝'])
            writer.writerow(["정상 민원", "도로 교통 신호등과 버스 정류장 보수를 요청합니다."])
            writer.writerow(["제외할 다음 본문", "가" * 2_000_001])
        with patch.object(imports, "embeddingForSelect", new=AsyncMock(return_value=None)), patch.object(imports, "LLM_IMPORT_ENABLED", False):
            imports.process_csv_job(job)
        with connection() as db:
            state = db.execute("SELECT * FROM import_jobs WHERE id=%s", (job["id"],)).fetchone()
            self.assertEqual((state["status"], state["completed_rows"], state["saved_rows"], state["failed_rows"]), ("completed", 3, 1, 2))
            failures = db.execute("SELECT source_row,raw_data,reason FROM import_failures WHERE job_id=%s ORDER BY source_row", (job["id"],)).fetchall()
            self.assertEqual([row["source_row"] for row in failures], [2, 4])
            self.assertTrue(all("2,000,000" in row["reason"] for row in failures))
            self.assertTrue(all(len(json.dumps(row["raw_data"])) < 300 for row in failures))
            self.assertEqual(db.execute("SELECT title FROM complaints").fetchone()["title"], "정상 민원")
        from app.routers.complaints import import_status
        status = import_status(str(job["id"]), {"role": "admin"})
        self.assertEqual(status["unreadable_rows"], 2)
        # Retrying from a durable checkpoint must not double count exclusions.
        with patch.object(imports, "embeddingForSelect", new=AsyncMock(return_value=None)), patch.object(imports, "LLM_IMPORT_ENABLED", False):
            imports.process_csv_job(status | {"storage_path": job["storage_path"], "encoding": job["encoding"]})
        self.assertEqual(import_status(str(job["id"]), {"role": "admin"})["unreadable_rows"], 2)

    def test_upload_queues_when_every_data_record_is_unreadable(self):
        from app.routers import complaints as routes
        from app.utils import util
        actor = {"role": "admin", "department_id": 2, "owner_id": None}
        content = ("제목,본문\n긴 본문," + "가" * 2_000_001 + "\n").encode("utf-8-sig")
        app.dependency_overrides[actor_from_auth] = lambda: actor
        client = TestClient(app)
        try:
            with patch.object(util, "FILE_STORAGE_DIR", Path(self.temp.name)), patch.object(routes, "start_import_worker"), patch.object(routes, "infer_csv_mapping", new=AsyncMock()) as infer:
                result = client.post("/api/imports", files={"file": ("all-long.csv", content, "text/csv")})
            self.assertEqual(result.status_code, 202, result.text)
            created = result.json()
            self.assertEqual((created["status"], created["total_rows"], created["unreadable_rows"], created["needs_mapping"]), ("queued", 1, 1, False))
            infer.assert_not_awaited()
            with connection() as db:
                job = db.execute("SELECT * FROM import_jobs WHERE id=%s", (created["job_id"],)).fetchone()
            imports.process_csv_job(job)
            self.assertEqual(routes.import_status(created["job_id"], actor)["unreadable_rows"], 1)
            Path(job["storage_path"]).unlink(missing_ok=True)
        finally:
            client.close()
            app.dependency_overrides.pop(actor_from_auth, None)

    def test_retry_skips_durable_checkpoint_and_reprocesses_uncommitted_progress(self):
        job=self.make_job(12)
        raw=list(parser.csv_rows(Path(job["storage_path"]),job["encoding"]))
        with connection() as db:
            for i in range(5):
                record=parser.csv_row_to_record(raw[i],i+2,job["column_mapping"])
                insert_complaint(db,fallback(record["title"],record["content"]),job["source_file"],i+2,None)
            db.execute("UPDATE import_jobs SET checkpoint_rows=5,completed_rows=8,saved_rows=5 WHERE id=%s",(job["id"],))
            job=db.execute("SELECT * FROM import_jobs WHERE id=%s",(job["id"],)).fetchone()
        with patch.object(imports,"embeddingForSelect",new=AsyncMock(return_value=None)),patch.object(imports,"LLM_IMPORT_ENABLED",False): imports.process_csv_job(job)
        with connection() as db:
            state=db.execute("SELECT * FROM import_jobs WHERE id=%s",(job["id"],)).fetchone()
            self.assertEqual((state["status"],state["saved_rows"],state["completed_rows"]),("completed",12,12))
            self.assertEqual(db.execute("SELECT count(*) AS n FROM complaints").fetchone()["n"],12)


if __name__=="__main__":
    unittest.main()
