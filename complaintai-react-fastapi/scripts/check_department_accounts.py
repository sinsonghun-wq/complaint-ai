"""Read-only login smoke test for the provisioned DEVELOPMENT accounts."""
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.ai import CATEGORIES
from app.department_merge import ADMIN_ACCOUNTS
from app.security import issue_token

with httpx.Client(base_url='http://127.0.0.1:8000', timeout=10) as client:
    for department, username, password in ADMIN_ACCOUNTS:
        response = client.post('/api/auth/login', json={'username':username, 'password':password})
        response.raise_for_status()
        data = response.json()
        assert data['user']['role'] == 'admin' and data['user']['department'] == department
        context = client.get('/api/department/context', headers={'Authorization':'Bearer '+data['token']})
        context.raise_for_status()
        assert context.json()['categories'] == [department]
        print(json.dumps({'username':username,'department':department,'login':'ok'}, ensure_ascii=True))
    # Historical accounts and all four merged-away accounts must reject old tokens.
    for i in [*range(101,108), 201, 202, 203, 206]:
        token = issue_token({'id':f'00000000-0000-4000-8000-{i:012d}', 'owner_id':f'00000000-0000-4000-9000-{i:012d}', 'account_role':'admin'})
        assert client.get('/api/complaints/counts', headers={'Authorization':'Bearer '+token}).status_code == 401
    print(json.dumps({'admin_logins':len(CATEGORIES),'retired_tokens':'rejected'}, ensure_ascii=True))
