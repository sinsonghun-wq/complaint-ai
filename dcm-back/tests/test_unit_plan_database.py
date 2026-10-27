"""DB-object unit contracts in a disposable PostgreSQL database only."""
import json
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo,conninfo_to_dict
from psycopg.rows import dict_row
from app.settings import DATABASE_URL,ACTIVE_EMBEDDING_MODEL
from app.services import rag_retrieval


class UnitDatabaseObjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name='complaintai_unit_review_'+uuid.uuid4().hex
        cls.url=make_conninfo(DATABASE_URL,dbname=cls.name)
        with psycopg.connect(DATABASE_URL,autocommit=True) as db:
            db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(cls.name)))
        cls.addClassCleanup(cls.drop_database)
        root=Path(__file__).resolve().parents[2]
        with psycopg.connect(cls.url) as db:
            db.execute((root/'database/init.sql').read_text(encoding='utf-8'))
            for name in ('20261020_rag_imports.sql','20261021_department_document_chunks.sql','20261023_rag_reference_lookup.sql','20261026_admin_department_sync.sql'):
                db.execute((root/'database/migrations'/name).read_text(encoding='utf-8'))
        cls.dbpatch=patch('app.db.DATABASE_URL',cls.url);cls.dbpatch.start();cls.addClassCleanup(cls.dbpatch.stop)
        cls.conn=psycopg.connect(cls.url,autocommit=True,row_factory=dict_row);cls.addClassCleanup(cls.conn.close)
        cls.owner=cls.conn.execute("SELECT id FROM app_users WHERE username='admin-land-transport'").fetchone()['id']

    @classmethod
    def drop_database(cls):
        assert cls.name.startswith('complaintai_unit_review_')
        assert cls.name!=conninfo_to_dict(DATABASE_URL).get('dbname')
        with psycopg.connect(DATABASE_URL,autocommit=True) as db:
            db.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(cls.name)))

    def setUp(self):
        self.conn.execute('DELETE FROM department_documents')
        self.conn.execute("UPDATE departments SET name='교통·국토' WHERE id=2")

    def parent(self):
        identifier=uuid.uuid4()
        self.conn.execute("""INSERT INTO department_documents(id,document_id,department_id,title,original_name,version,storage_path,content,created_by)
            VALUES(%s,%s,2,'단위 규정','unit.csv',1,'unit','제1조 통행 규정',%s)""",(identifier,uuid.uuid4(),self.owner))
        self.conn.execute("""INSERT INTO department_documents_chunk_state(parent_id,source_row,content_hash,token_count,embedding_model,splitter_version,total_chunks,completed_chunks,status)
            VALUES(%s,2,'unit-hash',9000,%s,'unit',2,1,'processing')""",(identifier,ACTIVE_EMBEDDING_MODEL))
        return identifier

    def chunk(self,parent,index=0,tokens=2):
        self.conn.execute("""INSERT INTO department_documents_chunks(id,parent_id,chunk_index,start_offset,end_offset,content,token_count,embedding)
            VALUES(%s,%s,%s,%s,%s,'조각',%s,%s::vector)""",
            (uuid.uuid4(),parent,index,index*2,index*2+2,tokens,str([1.]+[0.]*1023)))

    def test_chunk_constraints_and_parent_cascade(self):
        parent=self.parent();self.chunk(parent)
        with self.assertRaises(psycopg.IntegrityError):self.chunk(parent)
        with self.assertRaises(psycopg.IntegrityError):self.chunk(uuid.uuid4())
        for value in (0,8193):
            with self.subTest(tokens=value),self.assertRaises(psycopg.IntegrityError):self.chunk(parent,1,value)
        self.conn.execute('DELETE FROM department_documents WHERE id=%s',(parent,))
        self.assertEqual(self.conn.execute('SELECT count(*) n FROM department_documents_chunks').fetchone()['n'],0)
        self.assertEqual(self.conn.execute('SELECT count(*) n FROM department_documents_chunk_state').fetchone()['n'],0)

    def test_incomplete_and_ready_count_mismatch_are_excluded_from_lookup(self):
        parent=self.parent();self.chunk(parent,0);self.chunk(parent,1)
        vector=[1.]+[0.]*1023
        self.assertEqual(rag_retrieval.lookup(vector)[1],[])
        self.conn.execute("UPDATE department_documents_chunk_state SET status='ready' WHERE parent_id=%s",(parent,))
        self.assertEqual(rag_retrieval.lookup(vector)[1],[])
        self.conn.execute('UPDATE department_documents_chunk_state SET completed_chunks=2 WHERE parent_id=%s',(parent,))
        self.assertEqual(len(rag_retrieval.lookup(vector)[1]),2)

    def test_admin_requires_actual_department_foreign_key(self):
        for value in (None,99999):
            with self.subTest(department=value),self.assertRaises(psycopg.IntegrityError):
                self.conn.execute("UPDATE app_users SET department_id=%s WHERE username='admin-land-transport'",(value,))
        row=self.conn.execute("SELECT department_id FROM app_users WHERE id=%s",(self.owner,)).fetchone()
        self.assertEqual(row['department_id'],2)

    def test_department_rename_trigger_updates_only_connected_admin_name(self):
        before=self.conn.execute('SELECT id,username,password_salt,password_hash FROM app_users ORDER BY id').fetchall()
        other_names=self.conn.execute('SELECT id,display_name,department_id FROM app_users WHERE department_id IS DISTINCT FROM 2 ORDER BY id').fetchall()
        self.conn.execute("UPDATE departments SET name='단위시험 새 부서' WHERE id=2")
        user=self.conn.execute('SELECT display_name,department_id FROM app_users WHERE id=%s',(self.owner,)).fetchone()
        self.assertEqual(user,{'display_name':'단위시험 새 부서 관리자','department_id':2})
        self.assertEqual(before,self.conn.execute('SELECT id,username,password_salt,password_hash FROM app_users ORDER BY id').fetchall())
        self.assertEqual(other_names,self.conn.execute('SELECT id,display_name,department_id FROM app_users WHERE department_id IS DISTINCT FROM 2 ORDER BY id').fetchall())


if __name__=='__main__':unittest.main()
