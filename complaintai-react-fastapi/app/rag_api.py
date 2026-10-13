from __future__ import annotations

import uuid
import json
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from .db import connection, fetch_one
from .chat_intent import ChatIntentBody, ChatIntentResult, IntentUnavailable, classify_chat_intent
from .rag import run_recommendation
from .security import actor_from_auth

router = APIRouter(prefix='/api/ai-recommendations', tags=['AI 추천 답변'])


class RecommendationBody(BaseModel):
    complaint_id: int | None = Field(default=None, gt=0)
    parent_recommendation_id: uuid.UUID | None = None
    title: str = Field(default='', max_length=200)
    content: str = Field(default='', max_length=12000)

    @model_validator(mode='after')
    def validate_input(self):
        if self.complaint_id is not None and (self.parent_recommendation_id or self.title or self.content):
            raise ValueError('기존 민원 선택과 대화 입력을 동시에 사용할 수 없습니다.')
        if self.parent_recommendation_id is not None and (self.title or not self.content.strip()):
            raise ValueError('이어지는 대화에는 질문 내용만 입력해 주세요.')
        if self.complaint_id is None and self.parent_recommendation_id is None and (not self.title.strip() or not self.content.strip()):
            raise ValueError('민원 제목과 내용을 입력해 주세요.')
        return self


def followup_context(parent: dict) -> list[dict]:
    # Keep the original complaint and recent turns; prior AI text is never official evidence.
    turns = [*parent.get('conversation', []), {'role': 'user', 'content': parent['content']}]
    answer = (parent.get('result') or {}).get('answer')
    if answer:
        turns.append({'role': 'assistant', 'content': answer})
    selected = [turns[0], *turns[1:][-5:]]
    return [{'role': turn['role'], 'content': turn['content'][:1000 if index == 0 else 600]}
            for index, turn in enumerate(selected)]


def citizen(actor):
    if actor['role'] != 'user':
        raise HTTPException(403, '민원인 전용 AI 추천 답변 기능입니다.')


@router.get('/status')
def status(actor: Annotated[dict, Depends(actor_from_auth)]):
    citizen(actor)
    row = fetch_one("""SELECT COUNT(DISTINCT d.id) AS documents,COUNT(c.id) AS chunks
        FROM rag_documents d JOIN rag_chunks c ON c.document_id=d.id
        WHERE d.active AND d.review_status='verified' AND d.jurisdiction='전국'
        AND (d.effective_from IS NULL OR d.effective_from<=CURRENT_DATE)
        AND (d.effective_to IS NULL OR d.effective_to>=CURRENT_DATE)""")
    return {'knowledge_ready': row['chunks'] > 0, **row}


@router.post('', status_code=202)
def create(body: RecommendationBody, background: BackgroundTasks, actor: Annotated[dict, Depends(actor_from_auth)]):
    citizen(actor)
    with connection() as conn:
        # Serialize admission per owner so simultaneous requests cannot bypass the queue cap.
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('rag:' + actor['owner_id'],))
        count = conn.execute("SELECT COUNT(*) AS count FROM ai_recommendations WHERE owner_user_id=%s AND status IN ('queued','processing')", (actor['owner_id'],)).fetchone()['count']
        if count >= 2:
            raise HTTPException(429, '처리 중인 AI 추천 답변이 있습니다. 완료 후 다시 요청해 주세요.')
        title, content = body.title.strip(), body.content.strip()
        complaint_id, conversation = body.complaint_id, []
        if body.parent_recommendation_id is not None:
            parent = conn.execute('''SELECT title,content,complaint_id,conversation,result,status
                FROM ai_recommendations WHERE id=%s AND owner_user_id=%s FOR SHARE''',
                (body.parent_recommendation_id, actor['owner_id'])).fetchone()
            if not parent:
                raise HTTPException(404, '이전 대화를 찾을 수 없습니다.')
            if parent['status'] != 'completed':
                raise HTTPException(409, '이전 답변이 완료된 후 질문을 보내 주세요.')
            title, complaint_id = parent['title'], parent['complaint_id']
            conversation = followup_context(parent)
        if body.complaint_id is not None:
            complaint = conn.execute('SELECT title,content,deleted_at,complaint_status FROM complaints WHERE id=%s AND owner_user_id=%s FOR SHARE', (body.complaint_id, actor['owner_id'])).fetchone()
            if not complaint:
                raise HTTPException(404, '민원을 찾을 수 없습니다.')
            if complaint['deleted_at'] or complaint['complaint_status'] == '취소':
                raise HTTPException(409, '취소된 민원은 선택할 수 없습니다.')
            title, content = complaint['title'], complaint['content']
            if not content.strip() or len(content) > 12000 or len(title) > 200:
                raise HTTPException(400, '이 민원은 AI 추천 답변의 입력 길이 범위를 벗어납니다. 핵심 내용을 새로 작성해 주세요.')
        identifier = str(uuid.uuid4())
        row = conn.execute('''INSERT INTO ai_recommendations
            (id,owner_user_id,complaint_id,title,content,conversation,parent_recommendation_id)
            VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s)
            RETURNING id,status,stage,title,content,complaint_id,parent_recommendation_id,created_at''',
            (identifier, actor['owner_id'], complaint_id, title, content,
             json.dumps(conversation, ensure_ascii=False), body.parent_recommendation_id)).fetchone()
    background.add_task(run_recommendation, identifier)
    return row


@router.post('/intent', response_model=ChatIntentResult)
async def intent(body: ChatIntentBody, actor: Annotated[dict, Depends(actor_from_auth)]):
    citizen(actor)
    try:
        return await classify_chat_intent(body)
    except IntentUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get('/{recommendation_id}')
def detail(recommendation_id: uuid.UUID, actor: Annotated[dict, Depends(actor_from_auth)]):
    citizen(actor)
    row = fetch_one('SELECT id,status,stage,title,content,complaint_id,parent_recommendation_id,result,error_code,error_message,created_at,updated_at FROM ai_recommendations WHERE id=%s AND owner_user_id=%s', (recommendation_id, actor['owner_id']))
    if not row:
        raise HTTPException(404, 'AI 추천 답변을 찾을 수 없습니다.')
    return row
