"""Verify repeatable additive migration in an isolated PostgreSQL database."""
import unittest
import uuid
from pathlib import Path
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from app.settings import DATABASE_URL


class RagMigrationTests(unittest.TestCase):
    def test_migration_preserves_existing_records_and_vectors_on_repeat(self):
        name = 'complaintai_rag_test_' + uuid.uuid4().hex
        params = conninfo_to_dict(DATABASE_URL)
        params['dbname'] = name
        root = Path(__file__).resolve().parents[2] / 'database'
        with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
            try:
                admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
            except psycopg.errors.InsufficientPrivilege:
                self.skipTest('Requires CREATEDB')
        try:
            with psycopg.connect(**params) as conn:
                conn.execute((root / 'init.sql').read_text(encoding='utf-8'))
                old_vector = '[' + ','.join(['1'] + ['0'] * 1535) + ']'
                identifier = conn.execute('INSERT INTO complaints(title,content,embedding) VALUES(%s,%s,%s::vector) RETURNING id', ('기존 민원', '보존할 원문', old_vector)).fetchone()[0]
                migration = (root / 'migrations/20261007_ai_recommendations.sql').read_text(encoding='utf-8')
                conn.execute(migration); conn.execute(migration)
                self.assertEqual(conn.execute('SELECT title,content,vector_dims(embedding) FROM complaints WHERE id=%s', (identifier,)).fetchone(), ('기존 민원', '보존할 원문', 1536))
                self.assertEqual(conn.execute("SELECT format_type(atttypid,atttypmod) FROM pg_attribute WHERE attrelid='rag_chunks'::regclass AND attname='embedding'").fetchone()[0], 'vector(1024)')
                self.assertEqual(conn.execute("SELECT column_default FROM information_schema.columns WHERE table_name='ai_recommendations' AND column_name='conversation'").fetchone()[0], "'[]'::jsonb")
        finally:
            assert name.startswith('complaintai_rag_test_') and name != conninfo_to_dict(DATABASE_URL).get('dbname')
            with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
                admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
