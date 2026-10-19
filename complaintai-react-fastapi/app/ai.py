from __future__ import annotations

import hashlib
import json
import math
import re
import time
from typing import Any
from .logger import logger

import httpx

from . import categories as category_defs
from . import prompts
from . import llm
from .dedup import identity
from .settings import ACTIVE_EMBEDDING_MODEL, EMBEDDING_API_URL, EMBEDDING_MODEL, EMBEDDING_PROVIDER, EMBEDDING_TIMEOUT_MS, LLM_MODEL, LLM_NUM_CTX, OLLAMA_EMBEDDING_MODEL, OLLAMA_URL

# 카테고리 이름·설명·키워드는 categories.py 한 곳에서 관리합니다.
CATEGORIES = list(category_defs.NAMES)
KEYWORDS = {c.name: list(c.keywords) for c in category_defs.CATEGORIES}
# DB 의 prompt_version 에 남기는 값 (어떤 프롬프트로 분석했는지 추적)
PROMPT_VERSION = prompts.PROMPT_VERSION
# 분석 JSON 의 최대 생성 토큰 (요약 2~3문장 + key_points + keywords 가 잘리지 않도록)
ANALYSIS_MAX_TOKENS = 512


_embedding_unavailable_until = 0.0


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def redact(value: Any) -> str:
    text = clean(value)
    text = re.sub(r"(?:\d{2,3}[- ]?\d{3,4}[- ]?\d{4}|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})", "[개인정보 제외]", text)
    return re.sub(r"\b\d{6}[- ]?[1-4]\d{6}\b", "[개인정보 제외]", text)


def fallback(title: str, content: str, reason: str = "LLM을 사용할 수 없어 규칙 기반 처리로 저장했습니다.") -> dict[str, Any]:
    raw_identity = identity(content)
    content = redact(content)
    sentences = re.findall(r"[^.!?。]+[.!?。]?", content) or [content]
    summary = clean(" ".join(sentences[:3]))[:700]
    source = f"{title} {summary}".lower()
    ranked = sorted(((category, sum(word in source for word in words)) for category, words in KEYWORDS.items()), key=lambda item: item[1], reverse=True)
    category, score = ranked[0]
    return {"_raw_identity": raw_identity, "title": clean(title) or summary[:40] or "제목 없음", "content": content, "summary": summary, "key_points": ["민원 대상과 발생 상황 확인", "생활 불편 및 안전 문제 검토", "민원인의 요청사항 확인"], "urgency": "high" if re.search(r"위험|사고|긴급|화재|붕괴", content) else "medium", "needs_review": len(content) < 20, "review_reason": "내용이 부족하여 검토가 필요합니다." if len(content) < 20 else None, "category": category if score else "기타", "confidence": 0.8 if score >= 3 else (0.6 if score else 0.3), "reason": reason, "keywords": [word for word in KEYWORDS[category] if word in source][:5] if score else [], "processing_mode": "fallback", "model": None, "prompt_version": PROMPT_VERSION}


def prompt(title: str, content: str) -> str:
    """민원 분석 프롬프트. 문장은 prompts.py 에서 관리합니다."""
    return prompts.analysis_prompt(clean(title), redact(content))


async def analyze(title: str, content: str) -> dict[str, Any]:
    safe = fallback(title, content)
    if not clean(content):
        return safe
    result = None
    try:
        # 어느 백엔드(Ollama / llama-server+어댑터)로 보낼지는 llm.py 가 .env 의 LLM_BACKEND 로 정합니다.
        result = await llm.chat_json(llm.TASK_ANALYSIS, [{"role": "user", "content": prompt(title, content)}],
                                     schema=prompts.ANALYSIS_JSON_SCHEMA, temperature=0, top_p=0.1, max_tokens=ANALYSIS_MAX_TOKENS)
        llm.log_usage("민원 분석", result, ANALYSIS_MAX_TOKENS)
        raw = json.loads(result.content.replace("```json", "").replace("```", "").strip())
        if raw.get("category") not in CATEGORIES or not clean(raw.get("summary")):
            raise ValueError("LLM JSON validation failed")
        # 정해진 필드(prompts.ANALYSIS_FIELDS)만 받습니다. confidence·reason 은 LLM 이 만들지 않으므로 비웁니다.
        fields = {key: raw[key] for key in prompts.ANALYSIS_FIELDS if key in raw}
        return {**safe, **fields, "title": clean(raw.get("title")) or safe["title"], "content": redact(content), "summary": clean(raw["summary"])[:900], "category": raw["category"], "urgency": raw.get("urgency") if raw.get("urgency") in prompts.URGENCY_VALUES else "medium", "confidence": None, "reason": None, "processing_mode": "llm", "model": result.model}
    except Exception as error:
        logger.exception(error)
        if result is not None:   # 응답은 받았는데 처리에 실패한 경우에만 모델 출력을 남깁니다.
            logger.warning(f"[민원 분석] 실패한 모델 출력: {str(result.content)[:2000]}")
        return fallback(title, content, f"LLM을 사용할 수 없어 규칙 기반 처리로 저장했습니다. ({error})")


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
        # CSV 열 매핑은 학습하지 않은 작업이라 기본 설정에서는 어댑터 없이(베이스 모델) 답합니다. (llm.uses_adapter)
        answer = await llm.chat_json(llm.TASK_CSV_MAPPING, [{"role": "user", "content": f"{instruction}\n\nheaders:\n{json.dumps(safe_headers, ensure_ascii=False)}\n\nsamples:\n{json.dumps(safe_samples, ensure_ascii=False)}"}], temperature=0)
        llm.log_usage("CSV 열 매핑", answer)
        result = json.loads(answer.content.replace("```json", "").replace("```", "").strip())
        return {"title_column": clean(result.get("title_column")), "content_columns": [clean(value) for value in result.get("content_columns", []) if clean(value)], "response_column": clean(result.get("response_column")), "category_column": clean(result.get("category_column")), "confidence": float(result.get("confidence", 0)), "reason": clean(result.get("reason")), "source": "llm"}
    except Exception as error:
        return {"title_column": "", "content_columns": [], "response_column": "", "category_column": "", "confidence": 0.0, "reason": f"헤더 매핑 LLM을 사용할 수 없습니다. ({error})", "source": "unavailable"}


async def embeddingForSelect(text: str) -> list[float] | None:
    """민원 본문(또는 챗봇 검색 구절) 텍스트를 형식 없이 그대로 임베딩합니다.

    저장(민원 접수·수정·일괄 저장)과 검색(/api/analyze, 챗봇 조회)이 모두 이 함수를 써서
    같은 입력 형식·같은 모델로 벡터를 만듭니다.
    """
    global _embedding_unavailable_until
    if not text or not text.strip():
        return None
    if time.monotonic() < _embedding_unavailable_until:
        return None
    try:
        # Allow model loading and embedding generation up to the configured timeout.
        async with httpx.AsyncClient(timeout=EMBEDDING_TIMEOUT_MS / 1000) as client:
            if EMBEDDING_PROVIDER == "ollama":
                response = await client.post(f"{OLLAMA_URL}/api/embed", json={"model": OLLAMA_EMBEDDING_MODEL, "input": text, "keep_alive": "10m"})
                response.raise_for_status(); vector = response.json().get("embeddings", [None])[0]
            else:
                response = await client.post(f"{EMBEDDING_API_URL}/v1/embeddings", json={"model": EMBEDDING_MODEL, "input": text, "dimensions": 1024, "encoding_format": "float"})
                response.raise_for_status(); vector = response.json()["data"][0]["embedding"]
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector
    except Exception:
        _embedding_unavailable_until = time.monotonic() + 30
        return None
