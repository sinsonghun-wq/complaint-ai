"""Run from complaintai-react-fastapi after stopping server/worker; --apply writes."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import connection
from app.department_merge import DEPARTMENT_MERGES, merge_departments


def main():
    parser = argparse.ArgumentParser(description='Merge labor/business and traffic/land; preserve other accounts and all complaint data.')
    parser.add_argument('--apply', action='store_true', help='Apply the atomic, data-only migration')
    args = parser.parse_args()
    with connection() as conn:
        if args.apply:
            result = merge_departments(conn)
        else:
            result = {'dry_run': True, 'mapping': DEPARTMENT_MERGES,
                      'affected_accounts': conn.execute("SELECT username,department FROM app_users WHERE account_role='admin' AND department=ANY(%s)", (list(DEPARTMENT_MERGES),)).fetchall(),
                      'affected_complaints': conn.execute('SELECT COUNT(*) count FROM complaints WHERE category=ANY(%s)', (list(DEPARTMENT_MERGES),)).fetchone()['count']}
        print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
