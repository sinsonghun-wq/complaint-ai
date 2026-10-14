"""Merge only the four affected departments/accounts, without changing schema.

Run with the web server and import worker stopped. All changes are transactional;
ordinary users and the five unaffected department accounts are left untouched.
The initial administrator passwords below are for LOCAL DEVELOPMENT only.
"""
import json

from .ai import CATEGORIES
from .security import password_hash
from .worker_manager import WORKER_LOCK_KEY

DEPARTMENT_MERGES = {
    '노동': '노동·기업',
    '기업': '노동·기업',
    '교통': '교통·국토',
    '건설·국토': '교통·국토',
}
MERGED_ADMIN_ACCOUNTS = [
    ('노동·기업', 'admin-labor-business', 'AdminLaborBusiness!2026', 301),
    ('교통·국토', 'admin-traffic-land', 'AdminTrafficLand!2026', 302),
]
ADMIN_ACCOUNTS = [(department, username, password) for department, username, password, _ in MERGED_ADMIN_ACCOUNTS] + [
    ('주택·건축', 'admin-housing', 'AdminHousing!2026'),
    ('환경·위생', 'admin-environment', 'AdminEnvironment!2026'),
    ('문화·행정·안전', 'admin-culture-safety', 'AdminCulture!2026'),
    ('보건·복지', 'admin-welfare', 'AdminWelfare!2026'),
    ('기타', 'admin-other', 'AdminOther!2026'),
]


def admin_identity(number):
    return (f'00000000-0000-4000-8000-{number:012d}',
            f'00000000-0000-4000-9000-{number:012d}')


def merge_category(category):
    """A deterministic rename, not a reclassification of unaffected complaints."""
    return DEPARTMENT_MERGES.get(category, category)


def merge_departments(conn):
    if set(CATEGORIES) != {department for department, _, _ in ADMIN_ACCOUNTS}:
        raise RuntimeError('The department configuration must match the seven merged departments.')
    with conn.transaction():
        conn.execute("SET LOCAL lock_timeout = '10s'")
        if not conn.execute('SELECT pg_try_advisory_xact_lock(%s) acquired', (WORKER_LOCK_KEY,)).fetchone()['acquired']:
            raise RuntimeError('Stop the CSV worker before merging departments.')
        conn.execute('LOCK TABLE app_users,complaints,complaint_responses,import_jobs,csv_schema_mappings,department_documents,organization_members IN SHARE ROW EXCLUSIVE MODE')
        if conn.execute("SELECT id FROM import_jobs WHERE status='processing' LIMIT 1").fetchone():
            raise RuntimeError('A processing import job must be stopped and marked failed before migration.')
        retired = conn.execute("SELECT id,owner_id,username,department FROM app_users WHERE account_role='admin' AND department=ANY(%s)", (list(DEPARTMENT_MERGES),)).fetchall()
        replacements = {}
        created = 0
        for department, username, password, number in MERGED_ADMIN_ACCOUNTS:
            identifier, owner = admin_identity(number)
            email = username + '@complaintai.local'
            existing = conn.execute('SELECT id,owner_id,username,email,account_role,department FROM app_users WHERE id=%s OR owner_id=%s OR username=%s OR email=%s', (identifier, owner, username, email)).fetchall()
            if existing:
                if len(existing) != 1 or any(str(existing[0][key]) != expected for key, expected in {
                    'id': identifier, 'owner_id': owner, 'username': username, 'email': email,
                    'account_role': 'admin', 'department': department,
                }.items()):
                    raise RuntimeError(f'Account collision for {username}; no accounts have been overwritten.')
            else:
                salt, digest = password_hash(password)
                conn.execute("""INSERT INTO app_users(id,owner_id,username,email,display_name,password_salt,password_hash,account_role,department)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,'admin',%s)""", (identifier, owner, username, email, department + ' 관리자', salt, digest, department))
                created += 1
            replacements[department] = (identifier, owner)

        moved = 0
        for old, new in DEPARTMENT_MERGES.items():
            # Publish only the new category; preserve title, original text, answers,
            # status, ownership and all timestamps. Preserve existing semantic
            # vectors so search remains available; flag the old department text
            # for optional re-embedding rather than removing searchable data.
            result = conn.execute("""UPDATE complaints SET category=%s,department=%s,
                analysis_revision=analysis_revision+1,
                analysis_metadata=analysis_metadata || jsonb_build_object('department_merge',
                    jsonb_build_object('previous_category',%s::text,'category',%s::text,'needs_reembedding',true))
                WHERE category=%s""", (new, new, old, new, old))
            moved += result.rowcount
            conn.execute('UPDATE complaints SET department=%s WHERE department=%s', (new, old))
            conn.execute('UPDATE complaint_responses SET department=%s WHERE department=%s', (new, old))
            conn.execute('UPDATE department_documents SET department=%s WHERE department=%s', (new, old))

        for old in retired:
            identifier, owner = replacements[merge_category(old['department'])]
            conn.execute('UPDATE complaint_responses SET author_user_id=%s WHERE author_user_id=%s', (identifier, old['id']))
            conn.execute('UPDATE department_documents SET created_by=%s WHERE created_by=%s', (identifier, old['id']))
            conn.execute('UPDATE csv_schema_mappings SET created_by_user_id=%s WHERE created_by_user_id=%s', (identifier, old['id']))
            conn.execute('UPDATE complaints SET owner_user_id=%s WHERE owner_user_id=%s', (owner, old['owner_id']))
            conn.execute('UPDATE import_jobs SET owner_user_id=%s WHERE owner_user_id=%s', (owner, old['owner_id']))
            conn.execute("""INSERT INTO organization_members(organization_id,user_id,role)
                SELECT organization_id,%s,role FROM organization_members WHERE user_id=%s
                ON CONFLICT (organization_id,user_id) DO UPDATE SET role=CASE
                    WHEN organization_members.role='admin' OR EXCLUDED.role='admin' THEN 'admin'
                    WHEN organization_members.role='manager' OR EXCLUDED.role='manager' THEN 'manager'
                    ELSE 'viewer' END""", (identifier, old['id']))
            conn.execute('DELETE FROM organization_members WHERE user_id=%s', (old['id'],))
            conn.execute('DELETE FROM app_users WHERE id=%s', (old['id'],))
        result = {'created_admins': created, 'deleted_admins': len(retired), 'moved_complaints': moved,
                  'already_migrated': not (created or retired or moved)}
        if not result['already_migrated']:
            detail = {**result, 'retired_accounts': [{key: str(value) for key, value in row.items()} for row in retired],
                      'department_mapping': DEPARTMENT_MERGES}
            conn.execute("INSERT INTO audit_events(event_type,entity_type,detail) VALUES('departments_merged','department',%s::jsonb)", (json.dumps(detail, ensure_ascii=False),))
        return result
