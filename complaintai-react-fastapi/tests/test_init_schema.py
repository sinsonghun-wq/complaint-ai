"""Verify destructive initialization only in an isolated, disposable database.

Requires a development PostgreSQL role with CREATEDB and pgvector installed.
The configured application database is never reset by these tests.
"""
import unittest
import uuid
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from app.settings import DATABASE_URL


class InitSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = 'complaintai_schema_test_' + uuid.uuid4().hex
        cls.params = conninfo_to_dict(DATABASE_URL)
        cls.params['dbname'] = cls.name
        cls.root = Path(__file__).resolve().parents[2] / 'database'
        cls.init_sql = (cls.root / 'init.sql').read_text(encoding='utf-8')
        with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
            try:
                admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(cls.name)))
            except psycopg.errors.InsufficientPrivilege:
                raise unittest.SkipTest('Schema test requires CREATEDB; no application data was changed.')

    @classmethod
    def tearDownClass(cls):
        # The target is the exact random database created above, never the app DB.
        assert cls.name.startswith('complaintai_schema_test_')
        assert cls.name != conninfo_to_dict(DATABASE_URL).get('dbname')
        with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
            admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(cls.name)))

    def setUp(self):
        self.db = psycopg.connect(**self.params, autocommit=True)
        self.db.execute(self.init_sql)

    def tearDown(self):
        if self.db.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            self.db.execute('ROLLBACK')
        self.db.close()

    def test_all_live_columns_and_vector_dimensions_are_present(self):
        query = """SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod)
            FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped"""
        with psycopg.connect(DATABASE_URL) as live:
            live_columns = set(live.execute(query).fetchall())
        self.assertEqual(set(self.db.execute(query).fetchall()), live_columns)

    def test_existing_outdated_table_is_replaced_and_data_cleared(self):
        self.db.execute('ALTER TABLE complaints DROP COLUMN analysis_revision')
        self.db.execute('ALTER TABLE complaints ADD COLUMN obsolete_column TEXT')
        self.db.execute("INSERT INTO complaints(title,content) VALUES('test-only','old row')")
        self.db.execute(self.init_sql)
        columns = {r[0] for r in self.db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='complaints'")}
        self.assertIn('analysis_revision', columns)
        self.assertIn('analysis_state', columns)
        self.assertNotIn('obsolete_column', columns)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM complaints').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM app_users').fetchone()[0], 0)

    def test_seed_is_optional_and_reinitialization_works(self):
        self.db.execute((self.root / 'seed_demo_accounts.sql').read_text(encoding='utf-8'))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM app_users WHERE account_role='user'").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM app_users WHERE account_role='admin'").fetchone()[0], 7)
        from app.department_merge import ADMIN_ACCOUNTS
        from app.security import verify_password
        for _, username, password in ADMIN_ACCOUNTS:
            salt, digest = self.db.execute('SELECT password_salt,password_hash FROM app_users WHERE username=%s', (username,)).fetchone()
            self.assertTrue(verify_password(password, salt, digest))
        self.db.execute(self.init_sql)
        self.db.execute(self.init_sql)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM app_users').fetchone()[0], 0)
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            self.db.execute("INSERT INTO complaints(title,owner_user_id) VALUES('orphan',%s)", (uuid.uuid4(),))

    def test_external_dependency_blocks_reset_and_rolls_back(self):
        self.db.execute("INSERT INTO complaints(title) VALUES('must survive rollback')")
        self.db.execute('CREATE VIEW external_schema_test_view AS SELECT id FROM complaints')
        try:
            with self.assertRaises(psycopg.errors.DependentObjectsStillExist):
                self.db.execute(self.init_sql)
            self.db.execute('ROLLBACK')
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM complaints').fetchone()[0], 1)
            self.assertEqual(self.db.execute("SELECT to_regclass('public.complaint_responses') IS NOT NULL").fetchone()[0], True)
        finally:
            self.db.execute('DROP VIEW external_schema_test_view')

    def test_department_merge_preserves_data_and_only_replaces_affected_accounts(self):
        from psycopg.rows import dict_row, tuple_row
        from app.department_merge import ADMIN_ACCOUNTS, merge_departments
        from app.security import verify_password
        self.db.execute((self.root / 'seed_demo_accounts.sql').read_text(encoding='utf-8'))
        self.db.execute("DELETE FROM app_users WHERE username IN ('admin-labor-business','admin-traffic-land')")
        unchanged = self.db.execute('SELECT * FROM app_users ORDER BY id').fetchall()
        old_departments = ['노동', '기업', '교통', '건설·국토']
        old_admins = []
        for department in old_departments:
            identifier, owner = uuid.uuid4(), uuid.uuid4()
            old_admins.append((identifier, owner))
            self.db.execute("INSERT INTO app_users(id,owner_id,email,username,account_role,department) VALUES(%s,%s,%s,%s,'admin',%s)", (identifier, owner, str(identifier)+'@test.local', 'old-'+str(identifier), department))
        user_owner = self.db.execute("SELECT owner_id FROM app_users WHERE username='user-a'").fetchone()[0]
        complaint = self.db.execute("INSERT INTO complaints(title,content,category,owner_user_id,complaint_status) VALUES('버스 민원','버스 주차 교통신호','교통',%s,'완료') RETURNING id", (user_owner,)).fetchone()[0]
        archived = self.db.execute("INSERT INTO complaints(title,content,category,department,owner_user_id,complaint_status,deleted_at) VALUES('보관 민원','보존할 원문','기업','기업',%s,'취소',NOW()) RETURNING id", (old_admins[1][1],)).fetchone()[0]
        vector = '[' + ','.join(['1'] + ['0'] * 1535) + ']'
        unchanged_record = self.db.execute("INSERT INTO complaints(title,content,category,embedding) VALUES('다른 부서','보존할 원문','환경·위생',%s::vector) RETURNING id", (vector,)).fetchone()[0]
        self.db.execute('UPDATE complaints SET embedding=%s::vector WHERE id=%s', (vector, complaint))
        before = self.db.execute('SELECT title,content,complaint_status,created_at FROM complaints WHERE id=%s', (complaint,)).fetchone()
        response_id = uuid.uuid4()
        self.db.execute("INSERT INTO complaint_responses(id,complaint_id,author_user_id,department,content) VALUES(%s,%s,%s,'교통','보존할 답변')", (response_id, complaint, old_admins[2][0]))
        document_id, job_id, org_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        self.db.execute("INSERT INTO department_documents(id,document_id,department,title,original_name,version,storage_path,content,created_by) VALUES(%s,%s,'건설·국토','도로 공사','test.txt',1,'/test','도로 포트홀 교량',%s)", (document_id, uuid.uuid4(), old_admins[3][0]))
        self.db.execute("INSERT INTO import_jobs(id,source_file,status,owner_user_id) VALUES(%s,'test.csv','queued',%s)", (job_id, old_admins[2][1]))
        self.db.execute("INSERT INTO csv_schema_mappings(schema_signature,column_mapping,created_by_user_id) VALUES(%s,'{}',%s)", ('a'*64, old_admins[0][0]))
        self.db.execute("INSERT INTO organizations(id,name) VALUES(%s,'테스트 조직')", (org_id,))
        self.db.execute("INSERT INTO organization_members VALUES(%s,%s,'viewer'),(%s,%s,'admin')", (org_id, old_admins[0][0], org_id, old_admins[1][0]))
        self.db.row_factory = dict_row
        result = merge_departments(self.db)
        self.assertEqual(result['deleted_admins'], 4)
        self.assertEqual(result['created_admins'], 2)
        self.assertEqual(result['moved_complaints'], 2)
        self.assertEqual(self.db.execute('SELECT owner_user_id FROM complaints WHERE id=%s', (complaint,)).fetchone()['owner_user_id'], user_owner)
        self.assertEqual(self.db.execute('SELECT category,complaint_status FROM complaints WHERE id=%s', (complaint,)).fetchone(), {'category':'교통·국토','complaint_status':'완료'})
        answer = self.db.execute('SELECT content,department FROM complaint_responses WHERE id=%s', (response_id,)).fetchone()
        self.assertEqual(answer, {'content':'보존할 답변','department':'교통·국토'})
        labor = self.db.execute("SELECT id,owner_id FROM app_users WHERE username='admin-labor-business'").fetchone()
        traffic = self.db.execute("SELECT id,owner_id FROM app_users WHERE username='admin-traffic-land'").fetchone()
        self.assertEqual(self.db.execute('SELECT author_user_id FROM complaint_responses WHERE id=%s', (response_id,)).fetchone()['author_user_id'], traffic['id'])
        self.assertEqual(self.db.execute('SELECT created_by,department FROM department_documents WHERE id=%s', (document_id,)).fetchone(), {'created_by':traffic['id'], 'department':'교통·국토'})
        self.assertEqual(self.db.execute('SELECT owner_user_id FROM import_jobs WHERE id=%s', (job_id,)).fetchone()['owner_user_id'], traffic['owner_id'])
        self.assertEqual(self.db.execute('SELECT created_by_user_id FROM csv_schema_mappings').fetchone()['created_by_user_id'], labor['id'])
        self.assertEqual(self.db.execute('SELECT user_id,role FROM organization_members').fetchone(), {'user_id':labor['id'], 'role':'admin'})
        archived_row = self.db.execute('SELECT category,owner_user_id,deleted_at,complaint_status FROM complaints WHERE id=%s', (archived,)).fetchone()
        self.assertEqual(archived_row['category'], '노동·기업')
        self.assertEqual(archived_row['owner_user_id'], labor['owner_id'])
        self.assertEqual(archived_row['complaint_status'], '취소')
        self.assertIsNotNone(archived_row['deleted_at'])
        self.assertIsNotNone(self.db.execute('SELECT embedding FROM complaints WHERE id=%s', (complaint,)).fetchone()['embedding'])
        self.assertTrue(self.db.execute('SELECT analysis_metadata FROM complaints WHERE id=%s', (complaint,)).fetchone()['analysis_metadata']['department_merge']['needs_reembedding'])
        self.assertIsNotNone(self.db.execute('SELECT embedding FROM complaints WHERE id=%s', (unchanged_record,)).fetchone()['embedding'])
        for department, username, password in ADMIN_ACCOUNTS:
            account = self.db.execute('SELECT * FROM app_users WHERE username=%s', (username,)).fetchone()
            self.assertEqual(account['department'], department)
            self.assertTrue(verify_password(password, account['password_salt'], account['password_hash']))
        self.assertTrue(merge_departments(self.db)['already_migrated'])
        self.db.row_factory = tuple_row
        self.assertEqual(self.db.execute('SELECT title,content,complaint_status,created_at FROM complaints WHERE id=%s', (complaint,)).fetchone(), before)
        self.assertEqual(self.db.execute("SELECT * FROM app_users WHERE username NOT IN ('admin-labor-business','admin-traffic-land') ORDER BY id").fetchall(), unchanged)

    def test_department_merge_collision_rolls_back_without_deleting_accounts(self):
        from psycopg.rows import dict_row
        from app.department_merge import merge_departments
        self.db.execute((self.root / 'seed_demo_accounts.sql').read_text(encoding='utf-8'))
        self.db.execute("DELETE FROM app_users WHERE username='admin-labor-business'")
        self.db.execute("UPDATE app_users SET account_role='user' WHERE username='admin-traffic-land'")
        old_id = uuid.uuid4()
        self.db.execute("INSERT INTO app_users(id,owner_id,email,username,account_role,department) VALUES(%s,%s,'old@test.local','old-labor','admin','노동')", (old_id, uuid.uuid4()))
        self.db.row_factory = dict_row
        with self.assertRaisesRegex(RuntimeError, 'Account collision'):
            merge_departments(self.db)
        self.assertIsNone(self.db.execute("SELECT id FROM app_users WHERE username='admin-labor-business'").fetchone())
        self.assertIsNotNone(self.db.execute('SELECT id FROM app_users WHERE id=%s', (old_id,)).fetchone())

    def test_historical_nine_migration_cannot_replace_current_accounts(self):
        from app.department_migration import migrate_departments
        with self.assertRaisesRegex(RuntimeError, 'Historical nine-department'):
            migrate_departments(self.db)

    def test_department_merge_rejects_running_worker(self):
        from psycopg.rows import dict_row
        from app.department_merge import merge_departments
        from app.worker_manager import WORKER_LOCK_KEY
        self.db.row_factory = dict_row
        with psycopg.connect(**self.params) as worker:
            worker.execute('SELECT pg_advisory_lock(%s)', (WORKER_LOCK_KEY,))
            with self.assertRaisesRegex(RuntimeError, 'Stop the CSV worker'):
                merge_departments(self.db)
        self.assertEqual(self.db.execute('SELECT COUNT(*) count FROM app_users').fetchone()['count'], 0)

    def test_department_merge_rejects_interrupted_processing_job(self):
        from psycopg.rows import dict_row
        from app.department_merge import merge_departments
        self.db.execute("INSERT INTO import_jobs(id,source_file,status) VALUES(%s,'test.csv','processing')", (uuid.uuid4(),))
        self.db.row_factory = dict_row
        with self.assertRaisesRegex(RuntimeError, 'processing import job'):
            merge_departments(self.db)
        self.assertEqual(self.db.execute('SELECT COUNT(*) count FROM app_users').fetchone()['count'], 0)


if __name__ == '__main__':
    unittest.main()
