"""Exercise SQL edits and real HTTP authorization in a disposable database."""
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from fastapi.testclient import TestClient

from app.db import connection
from app.departments import ensure_admin_department_identity
from app.main import app
from app.security import public_user
from app.services.trash import ensure_trash_schema
from app.settings import DATABASE_URL


ACCOUNTS = (
    ('admin-administration-safety', '문화·행정·안전'),
    ('admin-land-transport', '교통·국토'),
    ('admin-housing', '주택·건축'),
    ('admin-environment', '환경·위생'),
    ('admin-welfare', '보건·복지'),
    ('admin-fire', '노동·기업'),
    ('admin-other', '기타'),
)


class AdminDepartmentIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = 'complaintai_admin_identity_test_' + uuid.uuid4().hex
        cls.url = make_conninfo(DATABASE_URL, dbname=cls.name)
        with psycopg.connect(DATABASE_URL, autocommit=True) as db:
            db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(cls.name)))
        cls.addClassCleanup(cls.drop_database)
        root = Path(__file__).resolve().parents[2]
        with psycopg.connect(cls.url, autocommit=True) as db:
            db.execute((root / 'database/init.sql').read_text(encoding='utf-8'))
        database_patch = patch('app.db.DATABASE_URL', cls.url)
        database_patch.start()
        cls.addClassCleanup(database_patch.stop)
        ensure_trash_schema()
        ensure_admin_department_identity()
        cls.client = TestClient(app)
        cls.addClassCleanup(cls.client.close)
        with connection() as db:
            cls.catalog = db.execute('SELECT id,name FROM departments ORDER BY id').fetchall()

    @classmethod
    def drop_database(cls):
        assert cls.name.startswith('complaintai_admin_identity_test_')
        assert cls.name != conninfo_to_dict(DATABASE_URL).get('dbname')
        with psycopg.connect(DATABASE_URL, autocommit=True) as db:
            db.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(cls.name)))

    def setUp(self):
        with connection() as db:
            db.execute('DELETE FROM complaints')
            for department in self.catalog:
                db.execute('UPDATE departments SET name=%s WHERE id=%s', (department['name'], department['id']))
            for username, name in ACCOUNTS:
                db.execute('UPDATE app_users SET department_id=(SELECT id FROM departments WHERE name=%s) WHERE username=%s', (name, username))

    def account(self, username='admin-land-transport'):
        with connection() as db:
            return db.execute('SELECT id,username,display_name,department_id FROM app_users WHERE username=%s', (username,)).fetchone()

    def login(self):
        result = self.client.post('/api/auth/login', json={'username': 'admin-land-transport', 'password': 'AdminLand!2026'})
        self.assertEqual(result.status_code, 200, result.text)
        return {'Authorization': 'Bearer ' + result.json()['token']}

    def complaint(self, department_id):
        with connection() as db:
            return db.execute("""INSERT INTO complaints(title,content,summary,category,department_id)
                VALUES('연결 검증 민원','보존할 테스트 원문','요약',
                    (SELECT name FROM departments WHERE id=%s),%s) RETURNING id""",
                (department_id, department_id)).fetchone()['id']

    def test_repairs_all_legacy_null_assignments_without_changing_credentials(self):
        with connection() as db:
            before = db.execute('SELECT id,owner_id,username,password_salt,password_hash FROM app_users ORDER BY id').fetchall()
            db.execute('ALTER TABLE app_users DROP CONSTRAINT app_users_admin_department_required')
            db.execute("UPDATE app_users SET department_id=NULL,display_name='과거 이름' WHERE account_role='admin'")
        ensure_admin_department_identity()
        with connection() as db:
            self.assertEqual(before, db.execute('SELECT id,owner_id,username,password_salt,password_hash FROM app_users ORDER BY id').fetchall())
        for username, name in ACCOUNTS:
            with self.subTest(username=username):
                row = self.account(username)
                expected = next(d['id'] for d in self.catalog if d['name'] == name)
                self.assertEqual((row['department_id'], row['display_name']), (expected, name + ' 관리자'))

    def test_sql_department_rename_keeps_existing_token_and_processing_permission(self):
        headers = self.login()
        own = self.complaint(2)
        other = self.complaint(4)
        with connection() as db:
            db.execute("UPDATE departments SET name='미래 교통·국토' WHERE id=2")
        # Startup must preserve the numeric link even after the old name disappears.
        ensure_admin_department_identity()
        self.assertEqual(self.account()['display_name'], '미래 교통·국토 관리자')
        context = self.client.get('/api/auth/context', headers=headers).json()['user']
        self.assertEqual((context['department_id'], context['department_name'], context['name']), (2, '미래 교통·국토', '미래 교통·국토 관리자'))
        detail = self.client.get(f'/api/complaints/{own}', headers=headers).json()['complaint']
        self.assertEqual(detail['department_name'], '미래 교통·국토')
        self.assertEqual(detail['category'], '교통·국토')  # Historical classification stays intact.
        self.assertTrue(detail['can_manage'])
        rows = self.client.get('/api/complaints?department_id=2', headers=headers).json()['complaints']
        self.assertEqual(rows[0]['department_name'], '미래 교통·국토')
        self.assertEqual(self.client.post(f'/api/department/complaints/{other}/start', headers=headers).status_code, 403)
        self.assertEqual(self.client.post(f'/api/department/complaints/{own}/start', headers=headers).status_code, 200)
        draft = self.client.post(f'/api/department/complaints/{own}/responses', headers=headers, json={'content': '새 이름에서도 정상적으로 작성하는 답변'})
        self.assertEqual(draft.status_code, 201, draft.text)

    def test_reassignment_follows_actual_database_ids_and_does_not_revert_on_startup(self):
        headers = self.login()
        labor = next(d['id'] for d in self.catalog if d['name'] == '노동·기업')
        old = self.complaint(2)
        new = self.complaint(labor)
        with connection() as db:
            db.execute("UPDATE app_users SET department_id=%s WHERE username='admin-land-transport'", (labor,))
        ensure_admin_department_identity()
        user = self.client.get('/api/auth/context', headers=headers).json()['user']
        self.assertEqual((user['department_id'], user['name']), (labor, '노동·기업 관리자'))
        self.assertEqual(self.client.post(f'/api/department/complaints/{old}/start', headers=headers).status_code, 403)
        self.assertEqual(self.client.post(f'/api/department/complaints/{new}/start', headers=headers).status_code, 200)

    def test_database_rejects_missing_or_unknown_admin_departments(self):
        for identifier in (None, 999):
            with self.subTest(identifier=identifier), self.assertRaises(psycopg.IntegrityError):
                with connection() as db:
                    db.execute("UPDATE app_users SET department_id=%s WHERE username='admin-land-transport'", (identifier,))
        with self.assertRaises(psycopg.IntegrityError):
            with connection() as db:
                db.execute("INSERT INTO app_users(id,owner_id,email,username,account_role) VALUES(%s,%s,'unassigned@test.local','unassigned-test','admin')", (uuid.uuid4(), uuid.uuid4()))

    def test_manual_admin_display_name_cannot_diverge_but_user_names_are_preserved(self):
        with connection() as db:
            db.execute("UPDATE app_users SET display_name='틀린 부서 관리자' WHERE username='admin-land-transport'")
            db.execute("UPDATE app_users SET display_name='일반 사용자 고유 이름' WHERE username='user-a'")
        self.assertEqual(self.account()['display_name'], '교통·국토 관리자')
        self.assertEqual(self.account('user-a')['display_name'], '일반 사용자 고유 이름')

    def test_repeatable_migration_preserves_linked_accounts_and_catalog_ids(self):
        before = [self.account(username) for username, _ in ACCOUNTS]
        ensure_admin_department_identity()
        ensure_admin_department_identity()
        self.assertEqual([self.account(username) for username, _ in ACCOUNTS], before)
        with connection() as db:
            self.assertEqual(db.execute('SELECT id,name FROM departments ORDER BY id').fetchall(), self.catalog)

    def test_api_uses_joined_department_name_even_if_a_restored_display_name_is_stale(self):
        user = {'id': uuid.uuid4(), 'owner_id': uuid.uuid4(), 'username': 'admin-example',
                'display_name': '옛 이름', 'account_role': 'admin', 'department_id': 2, 'department_name': '변경된 부서'}
        self.assertEqual(public_user(user)['name'], '변경된 부서 관리자')
