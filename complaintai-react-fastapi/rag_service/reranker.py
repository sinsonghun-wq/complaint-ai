"""Dedicated CPU Qwen reranker using the model author's yes/no logit recipe."""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from transformers import AutoModelForCausalLM, AutoTokenizer

MODEL_ID = 'Qwen/Qwen3-Reranker-0.6B'
REVISION = os.getenv('RAG_RERANKER_REVISION', 'e61197ed45024b0ed8a2d74b80b4d909f1255473')
MAX_LENGTH = int(os.getenv('RAG_RERANKER_MAX_LENGTH', '2048'))
BATCH_SIZE = max(1, int(os.getenv('RAG_RERANKER_BATCH_SIZE', '1')))
torch.set_num_threads(max(1, int(os.getenv('RAG_RERANKER_THREADS', '2'))))
lock = threading.Lock()
state = {}
prefix = '<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
suffix = '<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n'


def load():
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, revision=REVISION, padding_side='left')
    model = AutoModelForCausalLM.from_pretrained(MODEL_ID, revision=REVISION, torch_dtype=torch.float32, attn_implementation='sdpa').eval()
    state.update(tokenizer=tokenizer, model=model, revision=getattr(model.config, '_commit_hash', REVISION),
        yes=tokenizer.convert_tokens_to_ids('yes'), no=tokenizer.convert_tokens_to_ids('no'),
        prefix=tokenizer.encode(prefix, add_special_tokens=False), suffix=tokenizer.encode(suffix, add_special_tokens=False))


@asynccontextmanager
async def lifespan(app):
    load()
    yield


app = FastAPI(title='ComplaintAI Qwen3 Reranker', lifespan=lifespan)


class Body(BaseModel):
    query: str = Field(min_length=1, max_length=12200)
    documents: list[str] = Field(min_length=1, max_length=30)


@app.get('/health')
def health():
    return {'ready': bool(state), 'model': MODEL_ID, 'revision': state.get('revision'), 'runtime': 'transformers-cpu-fp32'}


@app.post('/rerank')
def rerank(body: Body):
    if any(len(doc) > 10000 for doc in body.documents):
        raise HTTPException(400, 'Document too long')
    if not state:
        raise HTTPException(503, 'Model not loaded')
    instruction = 'Retrieve official Korean government guidance that answers the citizen complaint, preserving conditions, exceptions and procedures.'
    texts = [f'<Instruct>: {instruction}\n<Query>: {body.query}\n<Document>: {doc}' for doc in body.documents]
    scores = []
    tokenizer, model = state['tokenizer'], state['model']
    with lock, torch.inference_mode():
        for start in range(0, len(texts), BATCH_SIZE):
            inputs = tokenizer(texts[start:start + BATCH_SIZE], padding=False, truncation=True,
                return_attention_mask=False, max_length=MAX_LENGTH - len(state['prefix']) - len(state['suffix']))
            inputs['input_ids'] = [state['prefix'] + tokens + state['suffix'] for tokens in inputs['input_ids']]
            inputs = tokenizer.pad(inputs, padding=True, return_tensors='pt')
            logits = model(**inputs, use_cache=False, logits_to_keep=1).logits[:, -1, :]
            pair_logits = torch.stack([logits[:, state['no']], logits[:, state['yes']]], dim=1)
            scores.extend(torch.softmax(pair_logits, dim=1)[:, 1].tolist())
    return {'model': MODEL_ID, 'revision': state['revision'], 'runtime': 'transformers-cpu-fp32',
        'results': [{'index': i, 'relevance_score': float(score)} for i, score in enumerate(scores)]}
