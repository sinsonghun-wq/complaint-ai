"""Run from complaintai-react-fastapi: python scripts/migrate_to_nine_departments.py --apply"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import connection
from app.department_migration import migrate_departments

parser = argparse.ArgumentParser(description='Replace administrator accounts and migrate department labels; keeps normal users and complaint data.')
parser.add_argument('--apply', action='store_true', help='Apply transaction after stopping the web server and CSV worker')
args = parser.parse_args()
if not args.apply:
    print('No changes made. Stop the server/worker, then pass --apply to migrate.')
else:
    with connection() as conn:
        print(json.dumps(migrate_departments(conn), ensure_ascii=True))
