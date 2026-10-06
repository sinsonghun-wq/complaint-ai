from __future__ import annotations

import hashlib
import json
import math
import re
import time
from typing import Any
from pathlib import Path

import httpx

from .settings import EMBEDDING_API_URL, EMBEDDING_MODEL, EMBEDDING_PROVIDER, EMBEDDING_TIMEOUT_MS, LLM_MODEL, LLM_TIMEOUT_MS, OLLAMA_EMBEDDING_MODEL, OLLAMA_URL
from .rule_classifier import classify_rules

DEPARTMENTS = json.loads(Path(__file__).with_name('departments.json').read_text(encoding='utf-8'))
CATEGORIES = [item['name'] for item in DEPARTMENTS]
KEYWORDS = {item['name']: item['keywords'] for item in DEPARTMENTS}
DEPARTMENT_GUIDANCE = '\n'.join(f"- {item['name']}: {item['description']}" for item in DEPARTMENTS)
PROMPT_VERSION = 'complaintai-ko-seven-context-v4'
_embedding_unavailable_until = 0.0


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def redact(value: Any) -> str:
    text = clean(value)
    text = re.sub(r"(?:\d{2,3}[- ]?\d{3,4}[- ]?\d{4}|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})", "[개인정보 제외]", text)
    return re.sub(r"\b\d{6}[- ]?[1-4]\d{6}\b", "[개인정보 제외]", text)


def fallback(title: str, content: str, reason: str | None = None) -> dict[str, Any]:
    content = redact(content)
    sentences = re.findall(r"[^.!?。]+[.!?。]?", content) or [content]
    summary = clean(" ".join(sentences[:3]))[:700]
    source = f"{title} {content}".lower()
    decision = classify_rules(source, KEYWORDS)
    review = []
    if len(content) < 20:
        review.append('내용이 부족하여 검토가 필요합니다.')
    if decision['requires_llm']:
        review.append('규칙 점수가 낮거나 부서 간 점수 차이가 작아 LLM 확인이 필요합니다.')
    elif not decision['top_score']:
        review.append('부서를 판단할 키워드가 없어 검토가 필요합니다.')
    return {"title": clean(title) or summary[:40] or "제목 없음", "content": content, "summary": summary,
            "key_points": ["민원 대상과 발생 상황 확인", "생활 불편 및 안전 문제 검토", "민원인의 요청사항 확인"],
            "urgency": "high" if re.search(r"위험|사고|긴급|화재|붕괴", content) else "medium",
            "needs_review": bool(review), "review_reason": ' '.join(review) or None,
            "category": decision['category'], "confidence": decision['confidence'],
            "reason": reason or f"문맥·가중치 규칙으로 1차 판단했습니다. (점수 {decision['top_score']}, 차이 {decision['margin']})",
            "keywords": [hit['keyword'] for hit in decision['matched'][decision['category']]][:5],
            "rule_decision": decision, "processing_mode": "fallback", "model": None, "prompt_version": PROMPT_VERSION}


def prompt(title: str, content: str) -> str:
    return f"당신은 대한민국 민원 데이터를 정확하고 중립적으로 처리하는 AI다. 원문에 없는 사실·기관·법령·해결책·날짜를 만들지 말고 개인정보는 [개인정보 제외]로 처리한다. 허용 category 중 하나만 고른다: {', '.join(CATEGORIES)}. 민원인의 가장 직접적인 요청에 따라 하나만 선택한다. 여러 분야면 시급한 핵심 요청을 우선하며, 법률 민원은 법률이 적용되는 분야로 분류한다. 예약 대기는 대기환경이 아니며 상품 인도는 보행 공간이 아니다. 층간소음은 주택·건축을 우선하고 공장·공사장 소음은 환경·위생을 우선한다. 임대·전세 보증금은 주택·건축이며 일반 보증금은 계약 대상을 확인한다. 확신이 낮으면 needs_review=true로 표시한다. 부서별 기준:\n{DEPARTMENT_GUIDANCE}\nJSON만 반환한다. {{\"title\":\"짧은 제목\",\"summary\":\"2~4문장 요약\",\"key_points\":[\"핵심 쟁점\"],\"urgency\":\"low|medium|high\",\"needs_review\":false,\"review_reason\":null,\"category\":\"허용 category\",\"confidence\":0.0,\"reason\":\"분류 근거 한 문장\",\"keywords\":[\"핵심어\"]}}\n\n민원 제목:\n{clean(title)}\n\n민원 원문:\n{redact(content)}"


async def analyze(title: str, content: str) -> dict[str, Any]:
    safe = fallback(title, content)
    if not clean(content):
        return safe
    try:
        async with httpx.AsyncClient(timeout=LLM_TIMEOUT_MS / 1000) as client:
            response = await client.post(f"{OLLAMA_URL}/api/chat", json={"model": LLM_MODEL, "stream": False, "format": "json", "options": {"temperature": 0, "top_p": 0.1, "num_ctx": 2048, "num_predict": 260}, "messages": [{"role": "user", "content": prompt(title, content)}]})
            response.raise_for_status()
            raw = json.loads(response.json()["message"]["content"].replace("```json", "").replace("```", "").strip())
        if raw.get("category") not in CATEGORIES or not clean(raw.get("summary")):
            raise ValueError("LLM JSON validation failed")
        return {**safe, **raw, "title": clean(raw.get("title")) or safe["title"], "content": redact(content), "summary": clean(raw["summary"])[:900], "category": raw["category"], "urgency": raw.get("urgency") if raw.get("urgency") in {"low", "medium", "high"} else "medium", "processing_mode": "llm", "model": LLM_MODEL, "llm_attempted": True, "rule_decision": safe['rule_decision']}
    except Exception as error:
        return {**fallback(title, content, f"LLM을 사용할 수 없어 규칙 기반 처리로 저장했습니다. ({error})"), "llm_attempted": True}


def fingerprint(content: str) -> str:
    return hashlib.sha256(clean(content).encode()).hexdigest()


async def infer_csv_mapping(headers: list[str], samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Ask the LLM once per previously unseen CSV schema; callers validate the result."""
    safe_headers = [clean(header)[:120] for header in headers if clean(header)]
    safe_samples = [{header: redact(row.get(header, ""))[:240] for header in safe_headers} for row in samples[:5]]
    instruction = """당신은 CSV 민원 데이터의 열 매핑을 판단한다. 제공된 헤더와 예시 행만 사용한다.
민원 제목 열은 짧은 사건명·질문명이다. 민원 본문 열은 민원인의 신청·문의·불편 내용을 가진다.
답변 열과 처리결과·내부메모 열은 본문에 넣지 않는다. 분류 열은 기존 업무 분류가 있을 때만 선택한다.
반드시 아래 JSON만 반환한다. 모든 열 이름은 제공된 headers 중 정확히 하나여야 한다.
{"title_column":"", "content_columns":[""], "response_column":"", "category_column":"", "confidence":0.0, "reason":""}"""
    try:
        async with httpx.AsyncClient(timeout=LLM_TIMEOUT_MS / 1000) as client:
            response = await client.post(
                f"{OLLAMA_URL}/api/chat",
                json={"model": LLM_MODEL, "stream": False, "format": "json", "options": {"temperature": 0}, "messages": [{"role": "user", "content": f"{instruction}\n\nheaders:\n{json.dumps(safe_headers, ensure_ascii=False)}\n\nsamples:\n{json.dumps(safe_samples, ensure_ascii=False)}"}]},
            )
            response.raise_for_status()
            result = json.loads(response.json()["message"]["content"].replace("```json", "").replace("```", "").strip())
        return {"title_column": clean(result.get("title_column")), "content_columns": [clean(value) for value in result.get("content_columns", []) if clean(value)], "response_column": clean(result.get("response_column")), "category_column": clean(result.get("category_column")), "confidence": float(result.get("confidence", 0)), "reason": clean(result.get("reason")), "source": "llm"}
    except Exception as error:
        return {"title_column": "", "content_columns": [], "response_column": "", "category_column": "", "confidence": 0.0, "reason": f"헤더 매핑 LLM을 사용할 수 없습니다. ({error})", "source": "unavailable"}


def embedding_document(record: dict[str, Any]) -> str:
    return f"민원 제목: {record['title']}\n민원 원문: {record['content']}\n민원 요약: {record['summary']}\n분류 카테고리: {record['category']}\n핵심어: {', '.join(record.get('keywords') or [])}"


async def embedding(record: dict[str, Any]) -> list[float] | None:
    global _embedding_unavailable_until
    if time.monotonic() < _embedding_unavailable_until:
        return None
    text = embedding_document(record)
    try:
        # Allow model loading and embedding generation up to the configured timeout.
        async with httpx.AsyncClient(timeout=EMBEDDING_TIMEOUT_MS / 1000) as client:
            if EMBEDDING_PROVIDER == "ollama":
                response = await client.post(f"{OLLAMA_URL}/api/embed", json={"model": OLLAMA_EMBEDDING_MODEL, "input": text, "keep_alive": "10m"})
                response.raise_for_status(); vector = response.json().get("embeddings", [None])[0]
            else:
                response = await client.post(f"{EMBEDDING_API_URL}/v1/embeddings", json={"model": EMBEDDING_MODEL, "input": text, "dimensions": 1536, "encoding_format": "float"})
                response.raise_for_status(); vector = response.json()["data"][0]["embedding"]
        if not isinstance(vector, list) or len(vector) < 1536:
            return None
        vector = [float(value) for value in vector[:1536]]
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector
    except Exception:
        _embedding_unavailable_until = time.monotonic() + 30
        return None
