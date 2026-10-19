"""Trash contract tests, isolated in a disposable PostgreSQL database."""
import asyncio
import json
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from fastapi.testclient import TestClient

from app.db import connection
from app.main import app
from app.services import trash
from app.services.complaint import classify_submission
from app.routers.complaints import soft_delete

ACCOUNTS = [(1,"admin-labor-business","AdminLabor!2026"),(2,"admin-land-transport","AdminLand!2026"),
            (3,"admin-housing","AdminHousing!2026"),(4,"admin-environment","AdminEnvironment!2026"),
            (5,"admin-administration-safety","AdminService!2026"),(6,"admin-welfare","AdminWelfare!2026"),
            (7,"admin-other","AdminOther!2026"),("user","user-a","UserA!2026")]


class TrashArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name="complaintai_trash_test_"+uuid.uuid4().hex
        from app.settings import DATABASE_URL
        cls.original_url=DATABASE_URL
        cls.url=make_conninfo(DATABASE_URL,dbname=cls.name)
        with psycopg.connect(DATABASE_URL,autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.name)))
        root=Path(__file__).resolve().parents[2]
        try:
            with psycopg.connect(cls.url) as db:
                db.execute((root/"database/init.sql").read_text(encoding="utf-8"))
                db.execute((root/"database/seed_demo_accounts.sql").read_text(encoding="utf-8"))
        except Exception:
            with psycopg.connect(DATABASE_URL,autocommit=True) as admin:
                admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.name)))
            raise
        cls.db_patch=patch("app.db.DATABASE_URL",cls.url);cls.db_patch.start()
        cls.client=TestClient(app)
        cls.tokens={};cls.actors={}
        for key,username,password in ACCOUNTS:
            response=cls.client.post("/api/auth/login",json={"username":username,"password":password})
            assert response.status_code==200,response.text
            data=response.json();cls.tokens[key]=data["token"]
            with connection() as db:
                row=db.execute("SELECT id,owner_id,account_role,department_id FROM app_users WHERE username=%s",(username,)).fetchone()
            cls.actors[key]={"sub":str(row["id"]),"owner_id":str(row["owner_id"]),"role":row["account_role"],"department_id":row["department_id"]}

    @classmethod
    def tearDownClass(cls):
        cls.client.close();cls.db_patch.stop()
        assert cls.name.startswith("complaintai_trash_test_") and cls.name!=conninfo_to_dict(cls.original_url).get("dbname")
        with psycopg.connect(cls.original_url,autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.name)))

    def setUp(self):
        with connection() as db:
            db.execute("DELETE FROM complaints");db.execute("DELETE FROM audit_events")

    def request(self,actor,method,path,**kwargs):
        return self.client.request(method,path,headers={"Authorization":"Bearer "+self.tokens[actor]},**kwargs)

    def complaint(self,department=2,owner=True,status="접수",trashed=False,origin="user",title="원본 제목",content="실제 원본 민원 내용입니다."):
        with connection() as db:
            return db.execute("""INSERT INTO complaints(title,content,summary,department_id,owner_user_id,
                complaint_status,deleted_at,cancelled_at,cancelled_by_role,cancellation_reason,analysis_state,analysis_revision)
                VALUES(%s,%s,'요약',%s,%s,%s,CASE WHEN %s THEN NOW() END,CASE WHEN %s THEN NOW() END,%s,'보관 사유','completed',1)
                RETURNING id""",(title,content,department,self.actors["user"]["owner_id"] if owner else None,status,trashed,trashed,origin if trashed else None)).fetchone()["id"]

    def snapshot(self,identifier):
        with connection() as db:return db.execute("SELECT * FROM complaints WHERE id=%s",(identifier,)).fetchone()

    def full_archive(self,count=10000):
        with connection() as db:
            db.execute("""INSERT INTO complaints(title,content,summary,department_id,complaint_status,deleted_at,cancelled_by_role)
                SELECT '保管 '||i,'원문 '||i,'요약',2,'취소',TIMESTAMPTZ '2020-01-01'+i*INTERVAL '1 second','admin'
                FROM generate_series(1,%s) AS i""",(count,))

    def test_admin_and_citizen_deletions_both_preserve_original_in_trash(self):
        for actor in (2,"user"):
            identifier=self.complaint()
            options={"json":{"reason":"관리자 판단 사유"}} if actor==2 else {}
            response=self.request(actor,"DELETE",f"/api/complaints/{identifier}",**options)
            self.assertEqual(response.status_code,200,response.text)
            row=self.snapshot(identifier)
            self.assertIsNotNone(row["deleted_at"])
            self.assertEqual(row["cancelled_by_role"],"admin" if actor==2 else "user")
            self.assertEqual(row["title"],"원본 제목")
            self.assertEqual(row["content"],"실제 원본 민원 내용입니다.")
            self.assertEqual(self.request(2,"GET",f"/api/trash/{identifier}").json()["complaint"]["content"],row["content"])
        listing=self.request(2,"GET","/api/trash/items").json()
        self.assertEqual(listing["total"],2)
        self.assertNotIn("content",listing["items"][0])
        self.assertNotIn("summary",listing["items"][0])
        self.assertEqual(self.request(2,"GET","/api/complaints?department_id=2").json()["total"],0)

    def test_all_seven_departments_all_view_pagination_and_read_only(self):
        for department in range(1,8):self.complaint(department=department,trashed=True,status="취소",title=f"부서 {department}")
        self.complaint(department=None,trashed=True,status="취소")
        for actor in range(1,8):
            counts=self.request(actor,"GET","/api/trash/counts").json()
            self.assertEqual((counts["total"],counts["unassigned"],len(counts["departments"])),(8,1,7))
            for department in range(1,8):
                result=self.request(actor,"GET",f"/api/trash/items?department_id={department}").json()
                self.assertEqual(result["total"],1)
                self.assertEqual(result["items"][0]["can_manage"],actor==department)
            result=self.request(actor,"GET","/api/trash/items?limit=3&offset=3").json()
            self.assertEqual((result["total"],len(result["items"])),(8,3))
        self.assertEqual(self.request(2,"GET","/api/trash/items?unassigned=true").json()["total"],1)
        self.assertEqual(self.request(2,"GET","/api/trash/items?unassigned=true&department_id=2").status_code,400)
        self.assertEqual(self.request(2,"GET","/api/trash/items?limit=1000").status_code,422)

    def test_restore_user_or_admin_deleted_is_admin_only_and_answer_stays_private(self):
        for origin in ("user","admin"):
            identifier=self.complaint(trashed=True,status="취소",origin=origin)
            result=self.request(2,"POST",f"/api/trash/{identifier}/restore")
            self.assertEqual(result.status_code,200,result.text)
            row=self.snapshot(identifier)
            self.assertEqual(row["complaint_status"],"접수")
            self.assertIsNone(row["owner_user_id"])
            self.assertEqual(str(row["original_owner_user_id"]),self.actors["user"]["owner_id"])
            self.assertIsNone(row["deleted_at"])
            self.assertEqual(self.request("user","GET",f"/api/complaints/{identifier}").status_code,404)
            self.assertEqual(self.request("user","GET","/api/complaints").json()["total"],0)
            draft=self.request(2,"POST",f"/api/department/complaints/{identifier}/responses",json={"content":"복원 후 관리자 내부 답변입니다."})
            self.assertEqual(draft.status_code,201,draft.text)
            sent=self.request(2,"POST",f"/api/department/complaints/{identifier}/responses/send",json={"content":"관리자만 확인할 완료 답변"})
            self.assertEqual(sent.status_code,200,sent.text)
            self.assertEqual(self.request("user","GET",f"/api/my/complaints/{identifier}/responses").status_code,404)
            self.assertEqual(self.request("user","GET","/api/complaints").json()["total"],0)

    def test_restore_unassigned_uses_managing_department_and_repeated_restore_is_404(self):
        identifier=self.complaint(department=None,trashed=True,status="취소")
        result=self.request(4,"POST",f"/api/trash/{identifier}/restore")
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(self.snapshot(identifier)["department_id"],4)
        self.assertEqual(self.request(4,"POST",f"/api/trash/{identifier}/restore").status_code,404)

    def test_completed_restore_preserves_sent_answer_and_remains_admin_only(self):
        identifier=self.complaint(trashed=True,status="취소",origin="admin")
        with connection() as db:
            db.execute("UPDATE complaints SET trash_previous_status='완료' WHERE id=%s",(identifier,))
            db.execute("""INSERT INTO complaint_responses(id,complaint_id,author_user_id,department,department_id,content,response_state,sent_at)
                SELECT %s,%s,id,'교통·국토',2,'보존해야 하는 최종 답변','sent',NOW() FROM app_users WHERE username='admin-land-transport'""",(uuid.uuid4(),identifier))
        response=self.request(2,"POST",f"/api/trash/{identifier}/restore")
        self.assertEqual(response.status_code,200,response.text)
        row=self.snapshot(identifier)
        self.assertEqual(row["complaint_status"],"완료")
        self.assertIsNone(row["owner_user_id"])
        self.assertEqual(self.request("user","GET",f"/api/my/complaints/{identifier}/responses").status_code,404)
        with connection() as db:
            self.assertEqual(db.execute("SELECT content FROM complaint_responses WHERE complaint_id=%s",(identifier,)).fetchone()["content"],"보존해야 하는 최종 답변")

    def test_admin_cancel_notice_visible_until_admin_only_restore(self):
        identifier=self.complaint()
        self.assertEqual(self.request(2,"DELETE",f"/api/complaints/{identifier}",json={"reason":"상세 취소 사유"}).status_code,200)
        notice=self.request("user","GET","/api/complaints").json()
        self.assertEqual(notice["total"],1)
        self.assertEqual(notice["complaints"][0]["cancellation_reason"],"상세 취소 사유")
        self.assertEqual(self.request(2,"POST",f"/api/trash/{identifier}/restore").status_code,200)
        self.assertEqual(self.request("user","GET","/api/complaints").json()["total"],0)

    def test_selection_permanent_requires_password_and_is_atomic_and_scoped(self):
        own=self.complaint(trashed=True,status="취소")
        other=self.complaint(department=4,trashed=True,status="취소")
        response=self.request(2,"POST","/api/trash/permanent",json={"ids":[own],"password":"wrong"})
        self.assertEqual(response.status_code,403)
        response=self.request(2,"POST","/api/trash/permanent",json={"ids":[own,other],"password":"AdminLand!2026"})
        self.assertEqual(response.status_code,403)
        self.assertIsNotNone(self.snapshot(own));self.assertIsNotNone(self.snapshot(other))
        response=self.request(2,"POST","/api/trash/permanent",json={"ids":[own,own],"password":"AdminLand!2026"})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["permanently_deleted"],1)
        self.assertIsNone(self.snapshot(own));self.assertIsNotNone(self.snapshot(other))

    def test_department_permanent_removes_only_its_trash_not_active_or_other(self):
        own=self.complaint(trashed=True,status="취소");active=self.complaint()
        other=self.complaint(department=4,trashed=True,status="취소")
        self.assertEqual(self.request(2,"POST","/api/trash/department/4/permanent",json={"password":"AdminLand!2026"}).status_code,403)
        result=self.request(2,"POST","/api/trash/department/2/permanent",json={"password":"AdminLand!2026"})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()["permanently_deleted"],1)
        self.assertIsNone(self.snapshot(own));self.assertIsNotNone(self.snapshot(active));self.assertIsNotNone(self.snapshot(other))

    def test_all_seven_have_own_only_restore_and_permanent(self):
        for department,_,password in ACCOUNTS[:7]:
            own=self.complaint(department=department,trashed=True,status="취소")
            other=self.complaint(department=1 if department!=1 else 2,trashed=True,status="취소")
            self.assertEqual(self.request(department,"POST",f"/api/trash/{other}/restore").status_code,403)
            self.assertEqual(self.request(department,"DELETE",f"/api/complaints/{other}/permanent",json={"password":password}).status_code,403)
            self.assertEqual(self.request(department,"POST",f"/api/trash/{own}/restore").status_code,200)
            deletable=self.complaint(department=department,trashed=True,status="취소")
            result=self.request(department,"POST","/api/trash/permanent",json={"ids":[deletable],"password":password})
            self.assertEqual(result.status_code,200,result.text)
            self.assertIsNone(self.snapshot(deletable))
            self.assertIsNotNone(self.snapshot(other))

    def test_users_cannot_access_trash_or_legacy_restoration_endpoints(self):
        identifier=self.complaint(trashed=True,status="취소")
        for method,path,body in [("GET","/api/trash/items",None),("GET","/api/trash/counts",None),("GET",f"/api/trash/{identifier}",None),
            ("POST",f"/api/trash/{identifier}/restore",None),("POST",f"/api/complaints/{identifier}/restore",None),
            ("POST","/api/trash/permanent",{"ids":[identifier],"password":"UserA!2026"})]:
            with self.subTest(path=path):self.assertEqual(self.request("user",method,path,**({"json":body} if body else {})).status_code,403)

    def test_full_capacity_single_incoming_purges_newest_existing_100(self):
        self.full_archive()
        identifier=self.complaint()
        response=self.request("user","DELETE",f"/api/complaints/{identifier}")
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["automatically_purged"],100)
        with connection() as db:
            self.assertEqual(db.execute("SELECT count(*) AS n FROM complaints WHERE deleted_at IS NOT NULL").fetchone()["n"],9901)
            self.assertIsNotNone(db.execute("SELECT id FROM complaints WHERE title='保管 1'").fetchone())
            self.assertIsNone(db.execute("SELECT id FROM complaints WHERE title='保管 10000'").fetchone())
        self.assertIsNotNone(self.snapshot(identifier))

    def test_full_capacity_150_incoming_purges_existing_250(self):
        self.full_archive()
        with connection() as db:
            db.execute("INSERT INTO complaints(title,content,summary,department_id) SELECT '새 민원 '||i,'원문','요약',2 FROM generate_series(1,150) i")
        result=self.request(2,"DELETE","/api/departments/2/complaints",json={"reason":"일괄 보관"})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual((result.json()["deleted"],result.json()["automatically_purged"]),(150,250))
        with connection() as db:
            self.assertEqual(db.execute("SELECT count(*) AS n FROM complaints WHERE deleted_at IS NOT NULL").fetchone()["n"],9900)
            self.assertEqual(db.execute("SELECT count(*) AS n FROM complaints WHERE title LIKE '새 민원 %'").fetchone()["n"],150)

    def test_batch_larger_than_capacity_is_still_bounded(self):
        with connection() as db:
            db.execute("INSERT INTO complaints(title,content,summary,department_id) SELECT '대량 '||i,'원문','요약',2 FROM generate_series(1,10100) i")
        result=self.request(2,"DELETE","/api/departments/2/complaints",json={"reason":"대량 보관"})
        self.assertEqual(result.status_code,200,result.text)
        with connection() as db:
            self.assertLessEqual(db.execute("SELECT count(*) AS n FROM complaints WHERE deleted_at IS NOT NULL").fetchone()["n"],trash.TRASH_CAPACITY)

    def test_concurrent_archive_writers_do_not_exceed_capacity(self):
        self.full_archive(9999)
        ids=[self.complaint() for _ in range(2)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda identifier:soft_delete(identifier,self.actors["user"]),ids))
        self.assertEqual(sum(r["automatically_purged"] for r in results),100)
        with connection() as db:self.assertEqual(db.execute("SELECT count(*) AS n FROM complaints WHERE deleted_at IS NOT NULL").fetchone()["n"],9901)

    def test_late_classifier_cannot_resurrect_deleted_or_restored_complaint(self):
        identifier=self.complaint()
        with connection() as db:db.execute("UPDATE complaints SET analysis_state='pending' WHERE id=%s",(identifier,))
        async def scenario():
            entered=asyncio.Event();release=asyncio.Event()
            async def analyze(title,content):
                entered.set();await release.wait()
                return {"title":title,"content":content,"summary":"폐기될 오래된 요약","category":"환경·위생"}
            with patch("app.services.complaint.analyze",new=AsyncMock(side_effect=analyze)):
                task=asyncio.create_task(classify_submission(identifier,1))
                await entered.wait()
                soft_delete(identifier,self.actors["user"])
                trash.restore_record(identifier,self.actors[2])
                release.set();await task
        asyncio.run(scenario())
        row=self.snapshot(identifier)
        self.assertEqual(row["department_id"],2);self.assertIsNone(row["owner_user_id"])
        self.assertNotEqual(row["summary"],"폐기될 오래된 요약")

    def test_repeatable_migration_moves_legacy_cancel_without_permanent_deletion(self):
        identifier=self.complaint(status="취소")
        with connection() as db:db.execute("UPDATE complaints SET cancelled_by_role='admin' WHERE id=%s",(identifier,))
        trash.ensure_trash_schema();before=self.snapshot(identifier)
        trash.ensure_trash_schema();after=self.snapshot(identifier)
        self.assertIsNotNone(after["deleted_at"])
        self.assertEqual(before,after)
        self.assertEqual(after["title"],"원본 제목")


if __name__=="__main__":unittest.main()
