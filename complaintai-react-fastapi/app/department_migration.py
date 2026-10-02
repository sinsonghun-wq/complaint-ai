"""Data-only migration from seven departments to the current nine.

Does not drop tables or ordinary users. Reassigns administrator references before
deleting old accounts. Initial passwords are DEVELOPMENT ONLY.
"""
import json

from .ai import CATEGORIES, fallback
from .security import password_hash

ADMIN_ACCOUNTS = [
    ('노동', 'admin-labor', 'AdminLabor!2026'),
    ('기업', 'admin-business', 'AdminBusiness!2026'),
    ('교통', 'admin-traffic', 'AdminTraffic!2026'),
    ('주택·건축', 'admin-housing', 'AdminHousing!2026'),
    ('환경·위생', 'admin-environment', 'AdminEnvironment!2026'),
    ('건설·국토', 'admin-construction', 'AdminConstruction!2026'),
    ('문화·행정·안전', 'admin-culture-safety', 'AdminCulture!2026'),
    ('보건·복지', 'admin-welfare', 'AdminWelfare!2026'),
    ('기타', 'admin-other', 'AdminOther!2026'),
]
RENAMES = {'행정·안전': '문화·행정·안전', '소방': '문화·행정·안전',
           '주택건축': '주택·건축', '보건복지': '보건·복지',
           '행정·안전·생활서비스': '문화·행정·안전', '교통·주차': '교통', '도로·시설물': '건설·국토'}


def admin_identity(index):
    return (f'00000000-0000-4000-8000-{201 + index:012d}',
            f'00000000-0000-4000-9000-{201 + index:012d}')


def migrate_category(category, title='', content=''):
    if category is None:
        return None
    if category in RENAMES:
        return RENAMES[category]
    if category in CATEGORIES and category != '기타':
        return category
    inferred = fallback(title, content)['category']
    if category == '국토·교통' and inferred not in {'교통', '건설·국토', '주택·건축'}:
        return '건설·국토'
    return inferred


def migrate_departments(conn):
    with conn.transaction():
        conn.execute('LOCK TABLE app_users,complaints,complaint_responses,import_jobs,csv_schema_mappings,department_documents,organization_members IN SHARE ROW EXCLUSIVE MODE')
        admins = conn.execute("SELECT id,owner_id,username,department FROM app_users WHERE account_role='admin'").fetchall()
        expected = {admin_identity(i)[0] for i in range(9)}
        if len(admins) == 9 and {str(a['id']) for a in admins} == expected and {a['department'] for a in admins} == set(CATEGORIES):
            return {'already_migrated': True, 'created_admins': 0, 'deleted_admins': 0, 'moved_complaints': 0}
        # Temporary names free existing usernames, including admin-housing, without
        # deleting referenced administrator rows before handover is complete.
        for old in admins:
            conn.execute("UPDATE app_users SET username=%s,email=%s WHERE id=%s", ('retired-' + str(old['id']), str(old['id']) + '@retired.invalid', old['id']))
        replacements = {}
        for index, (department, username, password) in enumerate(ADMIN_ACCOUNTS):
            identifier, owner = admin_identity(index)
            # A prior failed migration rolls back atomically; unrelated users must
            # never be overwritten even if their IDs collide with provisioned IDs.
            salt, digest = password_hash(password)
            conn.execute("""INSERT INTO app_users(id,owner_id,username,email,display_name,password_salt,password_hash,account_role,department)
                VALUES(%s,%s,%s,%s,%s,%s,%s,'admin',%s)""", (identifier, owner, username, username + '@complaintai.local', department + ' 관리자', salt, digest, department))
            replacements[department] = identifier
        moved = 0
        for row in conn.execute('SELECT id,title,content,category FROM complaints').fetchall():
            category = migrate_category(row['category'], row['title'], row['content'] or '')
            if category != row['category']:
                metadata = json.dumps({'previous_category': row['category'], 'method': 'department-nine-rules', 'needs_review': row['category'] in ('국토·교통', '기타', '법률')}, ensure_ascii=False)
                conn.execute("""UPDATE complaints SET category=%s,department=%s,embedding=NULL,embedding_model=NULL,
                    analysis_revision=analysis_revision+1,analysis_metadata=analysis_metadata || jsonb_build_object('department_migration',%s::jsonb) WHERE id=%s""", (category, category, metadata, row['id']))
                moved += 1
        # Historical answers keep their content/status; only department/author refs
        # change. Ordinary user's owner IDs are not modified.
        for row in conn.execute('SELECT r.id,r.department,c.category FROM complaint_responses r JOIN complaints c ON c.id=r.complaint_id').fetchall():
            department = row['category'] or migrate_category(row['department']) or '기타'
            conn.execute('UPDATE complaint_responses SET department=%s WHERE id=%s', (department, row['id']))
        for row in conn.execute('SELECT id,title,content,department FROM department_documents').fetchall():
            department = migrate_category(row['department'], row['title'], row['content']) or '기타'
            conn.execute('UPDATE department_documents SET department=%s,embedding=NULL WHERE id=%s', (department, row['id']))
        for old in admins:
            replacement = replacements[migrate_category(old['department']) or '기타']
            conn.execute('UPDATE complaint_responses SET author_user_id=%s WHERE author_user_id=%s AND department=%s', (replacement, old['id'], migrate_category(old['department']) or '기타'))
            for department, identifier in replacements.items():
                conn.execute('UPDATE complaint_responses SET author_user_id=%s WHERE author_user_id=%s AND department=%s', (identifier, old['id'], department))
                conn.execute('UPDATE department_documents SET created_by=%s WHERE created_by=%s AND department=%s', (identifier, old['id'], department))
            conn.execute('UPDATE csv_schema_mappings SET created_by_user_id=%s WHERE created_by_user_id=%s', (replacement, old['id']))
            conn.execute('UPDATE complaints SET owner_user_id=NULL WHERE owner_user_id=%s', (old['owner_id'],))
            conn.execute('UPDATE import_jobs SET owner_user_id=NULL WHERE owner_user_id=%s', (old['owner_id'],))
            conn.execute('''INSERT INTO organization_members(organization_id,user_id,role)
                SELECT organization_id,%s,role FROM organization_members WHERE user_id=%s ON CONFLICT DO NOTHING''', (replacement, old['id']))
            conn.execute('DELETE FROM organization_members WHERE user_id=%s', (old['id'],))
            conn.execute('DELETE FROM app_users WHERE id=%s', (old['id'],))
        result = {'created_admins': 9, 'deleted_admins': len(admins), 'moved_complaints': moved}
        conn.execute("INSERT INTO audit_events(event_type,entity_type,detail) VALUES('departments_migrated','department',%s::jsonb)", (json.dumps(result),))
        return result
