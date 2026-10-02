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


if __name__ == '__main__':
    unittest.main()
