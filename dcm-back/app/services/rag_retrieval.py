"""Extend the existing three-source RAG lookup with published long-source spans."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import date

import httpx

from ..db import fetch_all, fetch_one
from ..settings import ACTIVE_EMBEDDING_MODEL, LLAMA_SERVER_URL, LLM_BACKEND, LLM_NUM_CTX
from .rag_splitting import TOKEN_LIMIT, document_structure, token_count

ROW_LIMIT = 3
CONTEXT_LIMIT = min(8192, max(512, LLM_NUM_CTX // 3))


def legacy_lookup(vector):
    # Keep the original short-only path and output unchanged.
    return fetch_all('''SELECT SUBSTRING(content, 1, 5000) AS content FROM department_documents
        WHERE deleted_at IS NULL ORDER BY embedding <=> %s::vector LIMIT 3''', (str(vector),))


def lookup(vector, as_of=None):
    if not fetch_one('SELECT parent_id FROM department_documents_chunk_state LIMIT 1'):
        return None
    short = fetch_all('''SELECT p.id AS parent_id,p.title,SUBSTRING(p.content,1,5000) AS content,
            p.embedding <=> %s::vector AS distance,'short' AS kind
        FROM department_documents p WHERE p.deleted_at IS NULL AND p.embedding IS NOT NULL
        AND NOT EXISTS(SELECT 1 FROM department_documents_chunk_state s WHERE s.parent_id=p.id)
        ORDER BY p.embedding <=> %s::vector LIMIT 3''', (str(vector), str(vector)))
    catalog = fetch_all('''SELECT s.parent_id,p.title,s.metadata FROM department_documents_chunk_state s
        JOIN department_documents p ON p.id=s.parent_id WHERE s.status='ready' AND p.deleted_at IS NULL AND s.embedding_model=%s''', (ACTIVE_EMBEDDING_MODEL,))
    groups = {}
    for row in catalog:
        identifier = row['metadata'].get('law_id')
        groups.setdefault(('id',identifier) if identifier else ('title',row['title']), []).append(row)
    excluded = []; ambiguous = {}
    for rows in groups.values():
        eligible, warning = choose_versions(rows, as_of or date.today())
        excluded.extend(str(row['parent_id']) for row in rows if str(row['parent_id']) not in eligible)
        if warning:
            for row in rows: ambiguous[str(row['parent_id'])] = f"{row['title']}: {warning}"
    chunks = fetch_all('''SELECT c.parent_id,c.chunk_index,c.start_offset,c.end_offset,p.title,
            c.embedding <=> %s::vector AS distance,s.metadata,'long' AS kind
        FROM department_documents_chunks c JOIN department_documents_chunk_state s ON s.parent_id=c.parent_id
        JOIN department_documents p ON p.id=c.parent_id
        WHERE s.status='ready' AND s.completed_chunks=s.total_chunks AND s.total_chunks>0
          AND s.embedding_model=%s AND p.deleted_at IS NULL AND c.embedding IS NOT NULL
          AND NOT(c.parent_id=ANY(%s::uuid[]))
        ORDER BY c.embedding <=> %s::vector LIMIT 24''', (str(vector), ACTIVE_EMBEDDING_MODEL, excluded, str(vector)))
    warnings = []
    if ambiguous:
        closest = fetch_one('''SELECT c.parent_id,c.embedding <=> %s::vector AS distance FROM department_documents_chunks c
            JOIN department_documents_chunk_state s ON s.parent_id=c.parent_id JOIN department_documents p ON p.id=c.parent_id
            WHERE c.parent_id=ANY(%s::uuid[]) AND c.embedding IS NOT NULL AND s.status='ready' AND p.deleted_at IS NULL
            ORDER BY c.embedding <=> %s::vector LIMIT 1''', (str(vector), list(ambiguous), str(vector)))
        distances = [row['distance'] for row in short + chunks]
        if closest and (not distances or closest['distance'] <= min(distances)):
            warnings.append(ambiguous[str(closest['parent_id'])])
    return short, chunks, warnings


def effective_date(value):
    match = re.search(r'(\d{4})\D{1,3}(\d{1,2})\D{1,3}(\d{1,2})', str(value or ''))
    if not match: return None
    try: return date(*map(int, match.groups()))
    except ValueError: return None


def choose_versions(rows, as_of):
    dated = [(effective_date(row['metadata'].get('effective_date')), str(row['parent_id'])) for row in rows]
    if len(rows) == 1:
        day, identifier = dated[0]
        return ({identifier} if not day or day <= as_of else set()), None
    if any(day is None for day, _ in dated):
        versions = {str(row['metadata'].get('law_version') or '') for row in rows}
        if len(versions) > 1 or '' in versions:
            return set(), '동일 법령의 여러 자료가 있으나 시행일·개정본을 구분할 수 없습니다.'
        return {identifier for day, identifier in dated if day is None or day <= as_of}, None
    valid = [day for day, _ in dated if day <= as_of]
    if not valid: return set(), None
    latest = max(valid)
    owners = [identifier for day, identifier in dated if day == latest]
    if len(owners) != 1: return set(), '동일 시행일의 개정 자료가 여러 개여서 근거를 확정할 수 없습니다.'
    return set(owners), None


def version_eligible(candidate, as_of):
    metadata = candidate.get('metadata') or {}
    rows = fetch_all('''SELECT p.id AS parent_id,COALESCE(s.metadata,'{}'::jsonb) AS metadata
        FROM department_documents p LEFT JOIN department_documents_chunk_state s ON s.parent_id=p.id
        WHERE p.deleted_at IS NULL AND ((s.status='ready' AND s.embedding_model=%s) OR (s.parent_id IS NULL AND p.embedding IS NOT NULL))
          AND (CASE WHEN %s<>'' THEN s.metadata->>'law_id'=%s ELSE p.title=%s END)''',
        (ACTIVE_EMBEDDING_MODEL, metadata.get('law_id',''), metadata.get('law_id',''), candidate['title']))
    eligible, warning = choose_versions(rows, as_of)
    return str(candidate['parent_id']) in eligible, warning


def load_source(parent_id):
    source = fetch_one('''SELECT p.id,p.title,p.content,s.content_hash,s.structure,COALESCE(s.metadata,'{}'::jsonb) AS metadata
        FROM department_documents p LEFT JOIN department_documents_chunk_state s ON s.parent_id=p.id
        WHERE p.id=%s AND p.deleted_at IS NULL AND ((s.status='ready' AND s.embedding_model=%s)
            OR (s.parent_id IS NULL AND p.embedding IS NOT NULL))''', (parent_id, ACTIVE_EMBEDDING_MODEL))
    if source and source['content_hash'] is None:
        # A long source can explicitly cite an ordinary short source. Its storage
        # and normal vector lookup remain unchanged; expand only this citation.
        if token_count(source['content']) > TOKEN_LIMIT: return None
        source['structure'] = document_structure(source['content'])
        source['content_hash'] = hashlib.sha256(source['content'].encode('utf-8')).hexdigest()
    return source


def source_context(parent_id, seeds, _visited=None, _depth=0):
    source = load_source(parent_id)
    if not source: return None
    if hashlib.sha256(source['content'].encode('utf-8')).hexdigest() != source['content_hash']:
        return {'content':'원문 변경으로 조각과의 일치 여부를 확인할 수 없습니다. 이 자료를 근거로 답변하지 마세요.', 'context_complete':False, 'parent_id':str(parent_id)}
    units = source['structure']
    by_key = {}
    for index, unit in enumerate(units): by_key.setdefault(unit['key'], []).append(index)
    needed = {index for index, unit in enumerate(units) if any(unit['start'] < seed['end_offset'] and unit['end'] > seed['start_offset'] for seed in seeds)}
    visited = set(_visited or ())
    identities = {(str(parent_id), units[index]['key']) for index in needed}
    if identities and identities.issubset(visited):
        return {'content':'', 'context_complete':True, 'parent_id':str(parent_id)}
    visited.update(identities)
    warnings = []; pending = list(needed)
    external_refs = []
    # Include adjacent exception clauses even if their vectors were not top-ranked.
    for index in list(needed):
        for adjacent in (index-1,index+1):
            if 0 <= adjacent < len(units) and units[adjacent]['exception']:
                if adjacent not in needed: needed.add(adjacent); pending.append(adjacent)
    # Definitions/application scope and exception clauses pointing BACK to a selected rule.
    selected_keys = {units[index]['key'] for index in needed}
    for index, unit in enumerate(units):
        if (unit['exception'] and selected_keys.intersection(unit['references'])) or re.search(r'\((?:정의|적용\s*범위|적용\s*제외)\)', unit['heading']):
            if index not in needed: needed.add(index); pending.append(index)
    while pending:
        index = pending.pop()
        unit = units[index]
        for backlink, other in enumerate(units):
            if other['exception'] and unit['key'] in other['references'] and backlink not in needed:
                needed.add(backlink); pending.append(backlink)
        for external in unit.get('external', []):
            if external not in external_refs: external_refs.append(external)
        for ref in unit['references']:
            matches = by_key.get(ref, [])
            if not matches:
                warnings.append(f'참조한 {ref}의 원문이 이 자료에 없습니다.')
            if len(matches) > 1:
                warnings.append(f'{ref}가 원문 안에 중복되어 참조 대상을 확정할 수 없습니다.')
            for match in matches:
                if match not in needed:
                    needed.add(match); pending.append(match)
        if len(needed) > 64:
            warnings.append('연결된 조항이 많아 한 번의 답변에서 전체 적용 조건을 확인할 수 없습니다.'); break
    metadata = source['metadata']
    additions = []
    visited.update((str(parent_id),units[index]['key']) for index in needed)
    for external in external_refs:
        related = fetch_all('''SELECT p.id AS parent_id,p.title,COALESCE(s.metadata,'{}'::jsonb) AS metadata
            FROM department_documents p LEFT JOIN department_documents_chunk_state s ON s.parent_id=p.id
            WHERE p.title=%s AND p.deleted_at IS NULL AND ((s.status='ready' AND s.embedding_model=%s)
                OR (s.parent_id IS NULL AND p.embedding IS NOT NULL))''', (external['law'], ACTIVE_EMBEDDING_MODEL))
        as_of = effective_date(metadata.get('effective_date')) or date.today()
        related = [row for row in related if version_eligible(row, as_of)[0]]
        if len(related) != 1 or _depth >= 3:
            warnings.append(f"외부 법령 ‘{external['law']}’의 {external['key']}에 해당하는 개정본·원문을 확정하지 못했습니다.")
            continue
        target = related[0]
        target_source = load_source(target['parent_id'])
        matching = [unit for unit in target_source['structure'] if unit['key'] == external['key']] if target_source else []
        if len(matching) != 1:
            warnings.append(f"외부 법령 ‘{external['law']}’의 {external['key']}를 찾지 못했습니다."); continue
        unit = matching[0]
        extra = source_context(target['parent_id'], [{'start_offset':unit['start'],'end_offset':unit['end']}], visited, _depth+1)
        if not extra or not extra['context_complete']:
            warnings.append(f"외부 법령 ‘{external['law']}’의 연결 근거가 불완전합니다.")
        elif extra['content']: additions.append(extra['content'])
    header = f"[원문 ID: {source['id']} | 법령·자료명: {source['title']} | 시행일: {metadata.get('effective_date') or '미제공'} | 개정 정보: {metadata.get('law_version') or '미제공'}]"
    if warnings:
        content = header + '\n근거 확인 미완료: ' + ' '.join(dict.fromkeys(warnings)) + '\n예외·참조 근거를 확인하지 못했으므로 규정의 적용 여부를 단정하지 마세요.'
    else:
        content = header + '\n' + '\n\n'.join(f"[{units[index]['heading']}]\n{source['content'][units[index]['start']:units[index]['end']]}" for index in sorted(needed))
        if additions: content += '\n\n[명시적으로 참조된 다른 법령의 근거]\n' + '\n\n'.join(additions)
    return dict(content=content,parent_id=str(source['id']),title=source['title'],metadata=metadata,
                context_complete=not warnings,clauses=[units[index]['key'] for index in sorted(needed)],warnings=warnings)


async def chat_tokens(content):
    if LLM_BACKEND == 'llama_server':
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                result = await client.post(LLAMA_SERVER_URL+'/tokenize', json={'content':content,'add_special':False})
                result.raise_for_status()
                return len(result.json()['tokens'])
        except (httpx.HTTPError, ValueError, KeyError, TypeError): pass
    # Byte fallback deliberately overestimates ordinary byte-token vocabularies.
    return len(content.encode('utf-8'))


async def retrieve(vector, query=''):
    as_of = effective_date(query) or date.today()
    candidates = await asyncio.to_thread(lookup, vector, as_of)
    if candidates is None: return await asyncio.to_thread(legacy_lookup, vector)
    short, chunks, warnings = candidates
    if not chunks:
        results = [{'content':row['content']} for row in short]
        if warnings: results.append({'content':'근거 확인 미완료: '+' '.join(dict.fromkeys(warnings)), 'context_complete':False})
        return results
    ranked = sorted(short + chunks, key=lambda row:row['distance'])
    selected = []; seen = set()
    for candidate in ranked:
        parent = str(candidate['parent_id'])
        if parent in seen: continue
        seen.add(parent); selected.append(candidate)
        if len(selected) == ROW_LIMIT: break
    results = []; used = 0
    for candidate in selected:
        if candidate['kind'] == 'short':
            record = {'content':candidate['content']}
        else:
            seeds = [row for row in chunks if str(row['parent_id']) == str(candidate['parent_id'])][:1]
            record = await asyncio.to_thread(source_context, candidate['parent_id'], seeds)
            if not record: continue
        count = await chat_tokens(record['content'])
        if used + count > CONTEXT_LIMIT:
            if candidate['kind'] == 'short':
                # Original short text stays unchanged; prioritize it over optional long evidence.
                results.append(record); used += count; continue
            record = {'content':f"[원문 ID: {candidate['parent_id']} | {candidate['title']}] 관련 조항과 예외·참조를 모두 전달하면 답변 근거 한도를 초과합니다. 일부만 읽고 적용 여부를 단정하지 마세요.",
                      'parent_id':str(candidate['parent_id']), 'context_complete':False, 'warnings':['required_context_exceeds_budget']}
            count = await chat_tokens(record['content'])
        results.append(record); used += count
    if warnings: results.append({'content':'근거 확인 미완료: '+' '.join(dict.fromkeys(warnings)), 'context_complete':False})
    return results
