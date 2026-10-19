"""Pre-redaction identities. Only native PDF line-wrap spaces may vary.

Compact hashes select candidates, NEVER decide duplicates by themselves.
Profiles/tokens contain hashes and space positions, not original PII.
"""
import base64
import hashlib
import hmac
import json
from pathlib import Path
import re

from fastapi import HTTPException
from .db import connection
from .settings import AUTH_TOKEN_SECRET

ACTIVE = "deleted_at IS NULL AND complaint_status<>'취소'"
MIGRATION_LOCK = 831947263


def normalized(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def identity(text, pdf_wraps=False):
    text = str(text or '')
    positions, wraps = set(), set()
    compact_length = 0
    for match in re.finditer(r"\S+|\s+", text):
        value = match.group()
        if value.isspace():
            if compact_length:
                positions.add(compact_length)
                if pdf_wraps and re.fullmatch(r"[ \t]*\r?\n[ \t]*", value):
                    wraps.add(compact_length)
        else:
            compact_length += len(value)
    positions.discard(compact_length); wraps.discard(compact_length)
    return {'v': 1, 'raw': digest(normalized(text)),
            'compact': digest(re.sub(r'\s+', '', text)),
            'spaces': sorted(positions), 'wraps': sorted(wraps), 'length': compact_length}


def equivalent(first, second):
    if first.get('v') != 1 or second.get('v') != 1:
        return False
    if first['raw'] == second['raw']:
        return True
    if first['compact'] != second['compact'] or first['length'] != second['length']:
        return False
    differences = set(first['spaces']) ^ set(second['spaces'])
    return differences <= (set(first.get('wraps', [])) | set(second.get('wraps', [])))


def intake_token(profile, masked_content):
    payload = {**profile, 'display': digest(normalized(masked_content))}
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode().rstrip('=')
    signature = hmac.new(AUTH_TOKEN_SECRET.encode(), ('intake-dedup-v1:' + body).encode(), hashlib.sha256).hexdigest()
    return body + '.' + signature


def item_identity(item):
    token = item.get('dedup_token')
    if not token:
        return identity(item.get('content', ''))
    try:
        body, signature = token.split('.')
        expected = hmac.new(AUTH_TOKEN_SECRET.encode(), ('intake-dedup-v1:' + body).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError('signature')
        profile = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
        if profile['v'] != 1 or profile.pop('display') != digest(normalized(item.get('content', ''))):
            raise ValueError('changed preview')
        return profile
    except Exception as exc:
        raise HTTPException(422, '추출한 원문 지문이 변경되었습니다. 파일을 다시 선택해 처리해 주세요.') from exc


def lock_identities(conn, profiles, owner):
    # Batch keys must be acquired in the same order by ALL file writers.
    for key in sorted({f"exact-import:{owner or 'admin'}:{p['compact']}" for p in profiles}):
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (key,))


def find_duplicate(conn, profile, owner, exclude_id=None):
    rows = conn.execute(f"""SELECT id,dedup_profile FROM complaints
        WHERE dedup_compact_fingerprint=%s AND owner_user_id IS NOT DISTINCT FROM %s
        AND {ACTIVE} AND (%s::bigint IS NULL OR id<>%s) ORDER BY id""",
        (profile['compact'], owner, exclude_id, exclude_id)).fetchall()
    for row in rows:
        if equivalent(profile, row['dedup_profile']):
            return row['id']
    return None


def ensure_dedup_schema():
    path = Path(__file__).resolve().parents[2] / 'database/migrations/20261019_exact_dedup.sql'
    with connection() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(%s)', (MIGRATION_LOCK,))
        conn.execute('LOCK TABLE complaints IN SHARE ROW EXCLUSIVE MODE')
        conn.execute(path.read_text(encoding='utf-8'))
        rows = conn.execute("""SELECT id,content,owner_user_id,deleted_at,complaint_status
            FROM complaints WHERE dedup_profile='{}'::jsonb ORDER BY id""").fetchall()
        for row in rows:
            # Old masking cannot be undone: do NOT claim this is original text.
            if not row['content'] or '[개인정보 제외]' in row['content']:
                conn.execute("UPDATE complaints SET dedup_profile=%s::jsonb WHERE id=%s",
                             (json.dumps({'v': 0, 'legacy_original_unavailable': True}), row['id']))
                continue
            profile = identity(row['content'])
            duplicate = find_duplicate(conn, profile, row['owner_user_id']) if row['deleted_at'] is None and row['complaint_status'] != '취소' else None
            if duplicate:
                profile['legacy_duplicate_of'] = duplicate
            conn.execute("""UPDATE complaints SET raw_content_fingerprint=%s,
                dedup_compact_fingerprint=%s,dedup_profile=%s::jsonb WHERE id=%s""",
                (None if duplicate else profile['raw'], profile['compact'], json.dumps(profile), row['id']))
