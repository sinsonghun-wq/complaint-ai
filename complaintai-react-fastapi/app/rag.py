from __future__ import annotations

import asyncio
import json
import re
import time
from collections import defaultdict
from datetime import date

from . import settings
from .db import connection, fetch_all, fetch_one
from .rag_models import PROMPT_VERSION, RagError, embed_texts, embedding_identity, generate_answer, rerank
from .logger import logger

recommendation_slots = asyncio.Semaphore(1)


def vector_literal(vector: list[float]) -> str:
    return '[' + ','.join(str(value) for value in vector) + ']'


def query_terms(text: str) -> list[str]:
    terms = []
    for word in re.findall(r'[가-힣A-Za-z0-9]{2,}', text):
        word = re.sub(r'(에서는|으로는|에서|에게|으로|하고|이나|까지|부터|해주세요|합니다|인데|입니다|을|를|은|는|이|가)$', '', word)
        if len(word) >= 2 and word not in {'민원', '문의', '요청', '어떻게', '있습니다', '해주세요'}:
            terms.append(word)
    return list(dict.fromkeys(terms))[:20]


def retrieval_questions(title: str, content: str) -> list[str]:
    parts = [part.strip() for part in re.split(r'[?？]+', content) if len(part.strip()) >= 10]
    if 1 < len(parts) <= 3:
        return [title + '\n' + part for part in parts]
    return [title + '\n' + content]


def retrieve_candidates(query: str, vector: list[float], profile: str) -> list[dict]:
    columns = '''c.id AS chunk_id,c.document_id,c.heading,c.content,c.type_ids,c.departments,c.locator,c.applicability,
        d.title AS document_title,d.source_id,d.official_url,d.publisher,d.kind,d.version,
        d.jurisdiction,d.retrieved_at,d.effective_from,d.effective_to,d.metadata AS document_metadata'''
    eligibility = '''d.active AND d.review_status='verified' AND c.embedding_profile=%s
        AND d.jurisdiction='전국'
        AND (d.effective_from IS NULL OR d.effective_from<=CURRENT_DATE)
        AND (d.effective_to IS NULL OR d.effective_to>=CURRENT_DATE)'''
    params = [profile]
    shared = bool(re.search(r'아파트|연립주택|다세대주택', query))
    non_shared = bool(re.search(r'다가구|오피스텔', query))
    if shared and not non_shared:
        eligibility += " AND COALESCE(c.applicability->>'building_scope','')!='non_shared_housing'"
    elif non_shared and not shared:
        eligibility += " AND COALESCE(c.applicability->>'building_scope','')!='shared_housing'"
    # Exact cosine search is intentional for this small corpus: filtered ANN can omit evidence.
    dense = fetch_all(f'''SELECT {columns},1-(c.embedding<=>%s::vector) AS similarity
        FROM rag_chunks c JOIN rag_documents d ON d.id=c.document_id
        WHERE {eligibility} ORDER BY c.embedding<=>%s::vector LIMIT %s''',
        (vector_literal(vector), *params, vector_literal(vector), settings.RAG_CANDIDATES * 2))
    terms = query_terms(query)
    lexical = []
    if terms:
        expressions = ['CASE WHEN c.content ILIKE %s OR c.heading ILIKE %s THEN 1 ELSE 0 END' for _ in terms]
        term_params = [pattern for word in terms for pattern in ('%' + word + '%', '%' + word + '%')]
        lexical = fetch_all(f'''SELECT {columns},({' + '.join(expressions)}) AS lexical_score
            FROM rag_chunks c JOIN rag_documents d ON d.id=c.document_id
            WHERE {eligibility} ORDER BY lexical_score DESC,c.id LIMIT %s''',
            (*term_params, *params, settings.RAG_CANDIDATES * 2))
        lexical = [row for row in lexical if row['lexical_score'] > 0]
    scores, rows = defaultdict(float), {}
    for results in (dense, lexical):
        for rank, row in enumerate(results, 1):
            scores[row['chunk_id']] += 1 / (60 + rank)
            rows[row['chunk_id']] = {**rows.get(row['chunk_id'], {}), **row}
    return [rows[key] for key in sorted(scores, key=scores.get, reverse=True)[:settings.RAG_CANDIDATES]]


def set_stage(identifier: str, stage: str) -> None:
    with connection() as conn:
        conn.execute('UPDATE ai_recommendations SET stage=%s,updated_at=NOW() WHERE id=%s', (stage, identifier))


def insufficient_answer() -> dict:
    return {
        'answer_status': 'insufficient_evidence',
        'answer': '현재 등록된 공식 자료에서 이 민원에 답변할 충분한 근거를 찾지 못했습니다.',
        'checks': ['발생 장소, 발생 시점, 대상 시설이나 제도, 요청하는 조치를 구체적으로 확인해 주세요.'],
        'next_steps': [], 'citations': [],
    }


async def recommend(title: str, content: str, *, stage_callback=None, conversation=None) -> dict:
    started = time.monotonic()
    timings = {}
    async def stage(name):
        if stage_callback:
            await asyncio.to_thread(stage_callback, name)
    await stage('embedding')
    identity = await embedding_identity()
    questions = retrieval_questions(title, content)
    previous_user_context = '\n'.join(turn['content'] for turn in conversation or [] if turn['role'] == 'user')
    if previous_user_context:
        questions = [f'이전 민원 설명:\n{previous_user_context}\n현재 질문:\n{question}' for question in questions]
    vectors = await embed_texts(questions, query=True)
    timings['embedding_seconds'] = round(time.monotonic() - started, 3)
    await stage('retrieval')
    candidate_groups = []
    for question, vector in zip(questions, vectors):
        candidate_groups.append(await asyncio.to_thread(retrieve_candidates, question, vector, identity['profile']))
    metadata = {'embedding': identity, 'llm': settings.RAG_LLM_MODEL, 'prompt_version': PROMPT_VERSION, 'reference_date': date.today().isoformat(),
        'retrieval_settings': {'candidates': settings.RAG_CANDIDATES, 'context_chunks': settings.RAG_CONTEXT_CHUNKS, 'min_relevance': settings.RAG_MIN_RELEVANCE, 'num_ctx': settings.RAG_NUM_CTX, 'num_predict': settings.RAG_NUM_PREDICT}}
    metadata['retrieval_settings']['question_count'] = len(questions)
    if not any(candidate_groups):
        return {**insufficient_answer(), 'models': metadata, 'timings': timings}
    await stage('reranking')
    rerank_start = time.monotonic()
    relevant_groups = []
    for question, candidates in zip(questions, candidate_groups):
        if not candidates:
            relevant_groups.append([]); continue
        ranked, reranker_identity = await rerank(question, [row['heading'] + '\n' + row['content'] for row in candidates])
        metadata['reranker'] = reranker_identity
        relevant_groups.append([{**candidates[item['index']], 'relevance_score': item['relevance_score']}
            for item in ranked if item['relevance_score'] >= settings.RAG_MIN_RELEVANCE])
    timings['reranking_seconds'] = round(time.monotonic() - rerank_start, 3)
    evidence = []
    seen = set()
    # First take one result per question, then fill the remaining context budget.
    priority = [group[0] for group in relevant_groups if group]
    remaining = sorted([row for group in relevant_groups for row in group[1:]], key=lambda row: row['relevance_score'], reverse=True)
    for row in priority + remaining:
        if row['content'] in seen:
            continue
        evidence.append(row)
        seen.add(row['content'])
        if len(evidence) == settings.RAG_CONTEXT_CHUNKS:
            break
    if not evidence:
        return {**insufficient_answer(), 'models': metadata, 'timings': timings}
    await stage('generation')
    generation_start = time.monotonic()
    generated = await generate_answer(title, content, evidence, conversation=conversation or [])
    citations = []
    for citation_id in generated.pop('citation_ids'):
        row = evidence[citation_id - 1]
        citations.append({
            'number': citation_id, 'chunk_id': row['chunk_id'], 'document_id': str(row['document_id']),
            'source_id': row['source_id'], 'title': row['document_title'], 'heading': row['heading'],
            'url': row['official_url'], 'publisher': row['publisher'], 'kind': row['kind'],
            'version': row['version'], 'jurisdiction': row['jurisdiction'], 'locator': row['locator'],
            'retrieved_at': str(row['retrieved_at']), 'excerpt': row['content'],
            'limitations': row.get('document_metadata', {}).get('limitations', ''),
            'applicability': row.get('applicability', {}),
            'relevance_score': row['relevance_score'], 'type_ids': row['type_ids'],
        })
    timings['generation_seconds'] = round(time.monotonic() - generation_start, 3)
    timings['total_seconds'] = round(time.monotonic() - started, 3)
    return {**generated, 'citations': citations, 'models': metadata, 'timings': timings}


async def run_recommendation(identifier: str) -> None:
    async with recommendation_slots:
        row = await asyncio.to_thread(fetch_one, 'SELECT * FROM ai_recommendations WHERE id=%s AND status=\'queued\'', (identifier,))
        if not row:
            return
        def begin():
            with connection() as conn:
                updated = conn.execute("UPDATE ai_recommendations SET status='processing',stage='embedding',updated_at=NOW() WHERE id=%s AND status='queued' RETURNING id", (identifier,)).fetchone()
                return bool(updated)
        if not await asyncio.to_thread(begin):
            return
        try:
            result = await recommend(row['title'], row['content'], conversation=row.get('conversation', []),
                                     stage_callback=lambda stage: set_stage(identifier, stage))
            def complete():
                with connection() as conn:
                    conn.execute("UPDATE ai_recommendations SET status='completed',stage='completed',result=%s::jsonb,model_metadata=%s::jsonb,updated_at=NOW() WHERE id=%s", (json.dumps(result, ensure_ascii=False), json.dumps(result['models']), identifier))
            await asyncio.to_thread(complete)
        except Exception as exc:
            logger.exception(exc)
            code = exc.code if isinstance(exc, RagError) else 'recommendation_failed'
            message = str(exc) if isinstance(exc, RagError) else 'AI 추천 답변을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.'
            def fail():
                with connection() as conn:
                    conn.execute("UPDATE ai_recommendations SET status='failed',stage='failed',error_code=%s,error_message=%s,updated_at=NOW() WHERE id=%s", (code, message, identifier))
            await asyncio.to_thread(fail)
