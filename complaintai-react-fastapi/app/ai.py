from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

import httpx

from .settings import EMBEDDING_API_URL, EMBEDDING_MODEL, EMBEDDING_PROVIDER, EMBEDDING_TIMEOUT_MS, LLM_MODEL, LLM_TIMEOUT_MS, OLLAMA_EMBEDDING_MODEL, OLLAMA_URL

CATEGORIES = ["행정·안전·생활서비스", "교통·주차", "주택건축", "도로·시설물", "환경·위생", "보건복지", "소방", "법률", "기타"]
KEYWORDS = {
    "행정·안전·생활서비스": ["행정", "안전", "생활", "민원", "불편", "단속", "서비스"],
    "교통·주차": ["주차", "차량", "교통", "불법 주정차", "버스", "택시", "횡단보도", "등하교"],
    "주택건축": ["주택", "아파트", "건축", "공사", "재개발", "건물", "누수"],
    "도로·시설물": ["도로", "보도", "가로등", "시설", "포트홀", "인도", "표지판"],
    "환경·위생": ["쓰레기", "악취", "소음", "위생", "환경", "폐기물", "미세먼지"],
    "보건복지": ["복지", "의료", "보건", "돌봄", "장애", "노인", "지원"],
    "소방": ["화재", "소방", "소화전", "피난", "불법 적치", "위험물"],
    "법률": ["법률", "법", "규정", "처벌", "권리", "분쟁"], "기타": [],
}


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def redact(value: Any) -> str:
    text = clean(value)
    text = re.sub(r"(?:\d{2,3}[- ]?\d{3,4}[- ]?\d{4}|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})", "[개인정보 제외]", text)
    return re.sub(r"\b\d{6}[- ]?[1-4]\d{6}\b", "[개인정보 제외]", text)


def fallback(title: str, content: str, reason: str = "LLM을 사용할 수 없어 규칙 기반 처리로 저장했습니다.") -> dict[str, Any]:
    content = redact(content)
    sentences = re.findall(r"[^.!?。]+[.!?。]?", content) or [content]
    summary = clean(" ".join(sentences[:3]))[:700]
    source = f"{title} {summary}".lower()
    ranked = sorted(((category, sum(word in source for word in words)) for category, words in KEYWORDS.items()), key=lambda item: item[1], reverse=True)
    category, score = ranked[0]
    return {"title": clean(title) or summary[:40] or "제목 없음", "content": content, "summary": summary, "key_points": ["민원 대상과 발생 상황 확인", "생활 불편 및 안전 문제 검토", "민원인의 요청사항 확인"], "urgency": "high" if re.search(r"위험|사고|긴급|화재|붕괴", content) else "medium", "needs_review": len(content) < 20, "review_reason": "내용이 부족하여 검토가 필요합니다." if len(content) < 20 else None, "category": category if score else "기타", "confidence": 0.8 if score >= 3 else (0.6 if score else 0.3), "reason": reason, "keywords": KEYWORDS[category][:5] if score else [], "processing_mode": "fallback", "model": None, "prompt_version": "complaintai-ko-v1"}


def prompt(title: str, content: str) -> str:
    return f"당신은 대한민국 민원 데이터를 정확하고 중립적으로 처리하는 AI다. 원문에 없는 사실·기관·법령·해결책·날짜를 만들지 말고 개인정보는 [개인정보 제외]로 처리한다. 허용 category 중 하나만 고른다: {', '.join(CATEGORIES)}. JSON만 반환한다. {{\"title\":\"짧은 제목\",\"summary\":\"2~4문장 요약\",\"key_points\":[\"핵심 쟁점\"],\"urgency\":\"low|medium|high\",\"needs_review\":false,\"review_reason\":null,\"category\":\"허용 category\",\"confidence\":0.0,\"reason\":\"분류 근거 한 문장\",\"keywords\":[\"핵심어\"]}}\n\n민원 제목:\n{clean(title)}\n\n민원 원문:\n{redact(content)}"


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
        return {**safe, **raw, "title": clean(raw.get("title")) or safe["title"], "content": redact(content), "summary": clean(raw["summary"])[:900], "category": raw["category"], "urgency": raw.get("urgency") if raw.get("urgency") in {"low", "medium", "high"} else "medium", "processing_mode": "llm", "model": LLM_MODEL}
    except Exception as error:
        return fallback(title, content, f"LLM을 사용할 수 없어 규칙 기반 처리로 저장했습니다. ({error})")


def fingerprint(content: str) -> str:
    return hashlib.sha256(clean(content).encode()).hexdigest()


def embedding_document(record: dict[str, Any]) -> str:
    return f"민원 제목: {record['title']}\n민원 원문: {record['content']}\n민원 요약: {record['summary']}\n분류 카테고리: {record['category']}\n핵심어: {', '.join(record.get('keywords') or [])}"


async def embedding(record: dict[str, Any]) -> list[float] | None:
    text = embedding_document(record)
    try:
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
        return None
