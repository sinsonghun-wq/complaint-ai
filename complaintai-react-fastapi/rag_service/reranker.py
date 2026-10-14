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
OFFICIAL_INSTRUCTION = 'Retrieve official Korean government guidance that answers the citizen complaint, preserving conditions, exceptions and procedures.'
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
    instruction: str = Field(default=OFFICIAL_INSTRUCTION, min_length=1, max_length=1000)
    strict_context: bool = False


def document_inputs(body, tokenizer, prefix_ids, suffix_ids):
    """Keep existing official retrieval behavior; cover all search document tokens."""
    if not body.strict_context:
        texts = [f'<Instruct>: {body.instruction}\n<Query>: {body.query}\n<Document>: {doc}' for doc in body.documents]
        inputs = tokenizer(texts, padding=False, truncation=True, return_attention_mask=False,
                           max_length=MAX_LENGTH - len(prefix_ids) - len(suffix_ids))
        return [prefix_ids + tokens + suffix_ids for tokens in inputs['input_ids']], list(range(len(texts)))
    header = tokenizer.encode(f'<Instruct>: {body.instruction}\n<Query>: {body.query}\n<Document>: ', add_special_tokens=False)
    capacity = MAX_LENGTH - len(prefix_ids) - len(suffix_ids) - len(header)
    if capacity < 32:
        raise HTTPException(400, 'Search query exceeds the reranker context; shorten the query.')
    inputs, owners = [], []
    stride = capacity - min(64, capacity // 4)
    for index, document in enumerate(body.documents):
        tokens = tokenizer.encode(document, add_special_tokens=False)
        starts = range(0, len(tokens), stride) if tokens else [0]
        for start in starts:
            inputs.append(prefix_ids + header + tokens[start:start + capacity] + suffix_ids)
            owners.append(index)
            if start + capacity >= len(tokens):
                break
    return inputs, owners


@app.get('/health')
def health():
    return {'ready': bool(state), 'model': MODEL_ID, 'revision': state.get('revision'), 'runtime': 'transformers-cpu-fp32'}


@app.post('/rerank')
def rerank(body: Body):
    if any(len(doc) > 10000 for doc in body.documents):
        raise HTTPException(400, 'Document too long')
    if not state:
        raise HTTPException(503, 'Model not loaded')
    tokenizer, model = state['tokenizer'], state['model']
    token_rows, owners = document_inputs(body, tokenizer, state['prefix'], state['suffix'])
    scores = [0.0] * len(body.documents)
    with lock, torch.inference_mode():
        for start in range(0, len(token_rows), BATCH_SIZE):
            inputs = tokenizer.pad({'input_ids': token_rows[start:start + BATCH_SIZE]}, padding=True, return_tensors='pt')
            logits = model(**inputs, use_cache=False, logits_to_keep=1).logits[:, -1, :]
            pair_logits = torch.stack([logits[:, state['no']], logits[:, state['yes']]], dim=1)
            for offset, score in enumerate(torch.softmax(pair_logits, dim=1)[:, 1].tolist()):
                index = owners[start + offset]
                scores[index] = max(scores[index], score)
    return {'model': MODEL_ID, 'revision': state['revision'], 'runtime': 'transformers-cpu-fp32',
        'context_policy': 'complete_document' if body.strict_context else 'official_truncated',
        'results': [{'index': i, 'relevance_score': float(score)} for i, score in enumerate(scores)]}
