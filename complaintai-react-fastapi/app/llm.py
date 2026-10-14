"""
LLM 호출 한 곳 - Ollama / llama-server(+어댑터) 를 .env 의 LLM_BACKEND 로 바꿔 씁니다.

작업(task) 이름
  analysis    : 민원 분석 JSON          (ai.analyze)
  csv_mapping : CSV 열 매핑 JSON        (ai.infer_csv_mapping)
  chat        : 챗봇 에이전트(도구 호출) (agent.py)

LLM_BACKEND=ollama
  지금까지와 같습니다. 모든 작업이 LLM_MODEL 을 쓰고, CSV 열 매핑만 CSV_MAPPING_LLM_MODEL 로 바꿀 수 있습니다.

LLM_BACKEND=llama_server
  llama-server 를 베이스 GGUF + 어댑터 GGUF 로 띄워 둡니다. (scripts/run_llama_server.ps1)
      llama-server -m base.gguf --lora adapter.gguf --lora-init-without-apply --jinja ...
  --lora-init-without-apply 로 띄우면 어댑터는 메모리에만 올라가고 기본 세기는 0(=베이스 모델)입니다.
  요청마다 "lora": [{"id": 0, "scale": 1}] 을 실으면 그 요청에만 어댑터가 적용됩니다.
  어떤 작업에 어댑터를 켤지는 LLM_ADAPTER_TASKS (기본 analysis,chat) 로 정합니다.
  프롬프트는 llama-server 가 GGUF 안의 Gemma 4 채팅 템플릿(--jinja)으로 조립하므로, 학습 때(apply_chat_template)와 같은 템플릿입니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from .logger import logger
from .settings import (
    CSV_MAPPING_LLM_MODEL,
    LLAMA_SERVER_ADAPTER_NAME,
    LLAMA_SERVER_BASE_NAME,
    LLAMA_SERVER_LORA_ID,
    LLAMA_SERVER_URL,
    LLM_ADAPTER_TASKS,
    LLM_BACKEND,
    LLM_MODEL,
    LLM_NUM_CTX,
    LLM_TIMEOUT_MS,
    OLLAMA_URL,
)

TASK_ANALYSIS = "analysis"
TASK_CHAT = "chat"
TASK_CSV_MAPPING = "csv_mapping"
TASKS = (TASK_ANALYSIS, TASK_CHAT, TASK_CSV_MAPPING)

# 학습 때 사고(thinking) 모드를 끄고 템플릿을 조립했으므로(model-training/common.ENABLE_THINKING) 실행 때도 끕니다.
CHAT_TEMPLATE_KWARGS = {"enable_thinking": False}

_unknown = LLM_ADAPTER_TASKS - set(TASKS)
if _unknown:
    logger.warning(f"LLM_ADAPTER_TASKS 에 모르는 작업 이름이 있습니다: {sorted(_unknown)} (가능: {', '.join(TASKS)})")


def is_llama_server() -> bool:
    return LLM_BACKEND == "llama_server"


def uses_adapter(task: str) -> bool:
    """이 작업에 학습한 어댑터를 쓰는지. (ollama 백엔드에서는 어댑터 개념이 없으므로 False)"""
    return is_llama_server() and task in LLM_ADAPTER_TASKS


def model_label(task: str) -> str:
    """DB(llm_model)·API 응답에 남길 모델 이름. 어떤 모델이 이 결과를 만들었는지 추적용."""
    if is_llama_server():
        return f"{LLAMA_SERVER_BASE_NAME}+{LLAMA_SERVER_ADAPTER_NAME}" if uses_adapter(task) else LLAMA_SERVER_BASE_NAME
    return CSV_MAPPING_LLM_MODEL if task == TASK_CSV_MAPPING else LLM_MODEL


def llama_server_extra(task: str) -> dict[str, Any]:
    """llama-server 요청 본문에 덧붙이는 값 - 어댑터 세기 + 템플릿 옵션.
    어댑터를 안 쓰는 작업도 scale 0 을 명시합니다. (서버 기본값이 바뀌어도 결과가 달라지지 않게)"""
    return {
        "lora": [{"id": LLAMA_SERVER_LORA_ID, "scale": 1.0 if uses_adapter(task) else 0.0}],
        "chat_template_kwargs": CHAT_TEMPLATE_KWARGS,
    }


# =============================================================================
# JSON 응답 작업 (민원 분석·CSV 열 매핑)
# =============================================================================
@dataclass
class LLMResult:
    content: str
    finish_reason: str | None = None     # "length" 면 최대 생성 길이에서 잘림
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    model: str = ""
    raw: dict = field(default_factory=dict)


async def chat_json(task: str, messages: list[dict], *, schema: dict | None = None, temperature: float = 0,
                    top_p: float | None = None, max_tokens: int | None = None) -> LLMResult:
    """
    JSON 하나를 돌려받는 호출. schema 를 주면 그 JSON 스키마로 출력을 강제하고, 없으면 'JSON 객체' 로만 강제합니다.
    실패(연결·HTTP 오류)는 예외로 올립니다. 호출한 쪽이 규칙 기반 처리로 넘어갑니다.
    """
    async with httpx.AsyncClient(timeout=LLM_TIMEOUT_MS / 1000) as client:
        if is_llama_server():
            body: dict[str, Any] = {
                "model": model_label(task), "messages": messages, "stream": False, "temperature": temperature,
                "response_format": ({"type": "json_schema", "json_schema": {"name": task, "schema": schema}}
                                    if schema else {"type": "json_object"}),
                **llama_server_extra(task),
            }
            if top_p is not None:
                body["top_p"] = top_p
            if max_tokens is not None:
                body["max_tokens"] = max_tokens
            response = await client.post(f"{LLAMA_SERVER_URL}/v1/chat/completions", json=body)
            response.raise_for_status()
            data = response.json()
            choice = (data.get("choices") or [{}])[0]
            usage = data.get("usage") or {}
            return LLMResult(content=(choice.get("message") or {}).get("content") or "",
                             finish_reason=choice.get("finish_reason"), prompt_tokens=usage.get("prompt_tokens"),
                             output_tokens=usage.get("completion_tokens"), model=model_label(task), raw=data)

        options: dict[str, Any] = {"temperature": temperature, "num_ctx": LLM_NUM_CTX}
        if top_p is not None:
            options["top_p"] = top_p
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        # format 에 JSON 스키마를 넘기면 Ollama 가 스키마에 맞는 JSON 만 생성합니다. (Ollama 0.5 이상)
        body = {"model": model_label(task), "stream": False, "format": schema or "json", "options": options, "messages": messages}
        response = await client.post(f"{OLLAMA_URL}/api/chat", json=body)
        response.raise_for_status()
        data = response.json()
        return LLMResult(content=(data.get("message") or {}).get("content") or "", finish_reason=data.get("done_reason"),
                         prompt_tokens=data.get("prompt_eval_count"), output_tokens=data.get("eval_count"),
                         model=model_label(task), raw=data)


def log_usage(label: str, result: LLMResult, max_tokens: int | None = None) -> None:
    """응답 길이 정보를 남깁니다. 잘림·컨텍스트 초과가 의심되면 경고합니다. (두 백엔드 공통)"""
    logger.info(f"[{label}] model={result.model} finish_reason={result.finish_reason} prompt_tokens={result.prompt_tokens} "
                f"output_tokens={result.output_tokens} (num_ctx={LLM_NUM_CTX}, max_tokens={max_tokens})")
    if result.finish_reason == "length":
        logger.warning(f"[{label}] 최대 생성 길이(max_tokens={max_tokens})에 닿아 출력이 잘렸습니다.")
    if isinstance(result.prompt_tokens, int) and result.prompt_tokens + (max_tokens or 0) >= LLM_NUM_CTX:
        logger.warning(f"[{label}] 프롬프트({result.prompt_tokens}토큰)가 컨텍스트({LLM_NUM_CTX})에 가까워 앞부분이 잘렸을 수 있습니다. "
                       + ("llama-server 의 슬롯당 컨텍스트(-c / -np)도 확인하세요." if is_llama_server() else ""))


# =============================================================================
# 챗봇 (LangChain 채팅 모델)
# =============================================================================
def make_chat_model(task: str = TASK_CHAT, temperature: float = 0.1):
    """agent.py 의 create_agent 에 넘길 LangChain 채팅 모델."""
    if is_llama_server():
        # llama-server 는 OpenAI 호환 API(/v1)를 제공하고, --jinja 로 띄우면 Gemma 4 도구 호출을 tool_calls 로 해석해 줍니다.
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model_label(task),
            base_url=f"{LLAMA_SERVER_URL}/v1",
            api_key="not-needed",             # llama-server 를 --api-key 없이 띄우면 아무 값이나 됩니다
            temperature=temperature,
            timeout=LLM_TIMEOUT_MS / 1000,
            max_retries=1,
            extra_body=llama_server_extra(task),
        )

    from langchain_ollama import ChatOllama

    # num_ctx 를 민원 분석과 같게 맞춤 (요청마다 num_ctx 가 다르면 Ollama 가 모델을 다시 올려 느려짐)
    return ChatOllama(model=model_label(task), temperature=temperature, num_ctx=LLM_NUM_CTX)


# =============================================================================
# 시작 시 점검
# =============================================================================
async def check_backend() -> dict[str, Any]:
    """서버 시작 때 한 번 - llama-server 가 떠 있는지, 어댑터가 올라와 있는지 로그로 알려 줍니다. (실패해도 앱은 계속 뜸)"""
    info: dict[str, Any] = {"backend": LLM_BACKEND, "models": {t: model_label(t) for t in TASKS}}
    if not is_llama_server():
        logger.info(f"[LLM] backend=ollama url={OLLAMA_URL} models={info['models']}")
        return info
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            health = await client.get(f"{LLAMA_SERVER_URL}/health")
            adapters = await client.get(f"{LLAMA_SERVER_URL}/lora-adapters")
        info["health"] = health.status_code
        info["adapters"] = adapters.json() if adapters.status_code == 200 else None
    except Exception as error:
        logger.warning(f"[LLM] llama-server({LLAMA_SERVER_URL})에 연결하지 못했습니다: {error}  "
                       "-> scripts/run_llama_server.ps1 로 먼저 띄우세요. 그 전까지 분석은 규칙 기반으로 저장됩니다.")
        return info
    ids = [a.get("id") for a in info["adapters"] or [] if isinstance(a, dict)]
    logger.info(f"[LLM] backend=llama_server url={LLAMA_SERVER_URL} health={info['health']} adapters={info['adapters']} "
                f"adapter_tasks={sorted(LLM_ADAPTER_TASKS)} models={info['models']}")
    if LLM_ADAPTER_TASKS and LLAMA_SERVER_LORA_ID not in ids:
        logger.warning(f"[LLM] 어댑터 id={LLAMA_SERVER_LORA_ID} 가 llama-server 에 없습니다 (올라온 id: {ids}). "
                       "--lora 옵션으로 어댑터를 올렸는지, LLAMA_SERVER_LORA_ID 가 맞는지 확인하세요. "
                       "이대로면 어댑터를 켠 작업도 베이스 모델로 답하거나 요청이 거절될 수 있습니다.")
    return info
