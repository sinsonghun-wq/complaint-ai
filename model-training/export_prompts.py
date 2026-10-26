"""
챗봇 모드 0 의 도구 목록 JSON 을 파일로 뽑습니다. (학습 샘플을 실제 실행 때와 똑같이 만들기 위함)

앱이 실행될 때 LangChain 은 agent.py 의 도구 함수를 보고 도구 목록 JSON 을 자동으로 만들어 Ollama 에 보냅니다.
그 JSON 은 코드 어디에도 글자로 남지 않으므로, 학습 코드가 흉내 내지 않도록 같은 변환 함수로 만들어 저장합니다.

실행 (dcm-back 가상환경 - langchain 이 설치된 곳에서, 저장소 루트 기준)
    python model-training/export_prompts.py

결과: model-training/data/tools_mode0.json
  - tools    : 도구 목록 JSON (Ollama 에 보내는 그대로)
  - messages : 예시 대화 3개를 LangChain -> Ollama 요청 메시지(ollama_messages)와
               LangChain -> OpenAI 호환 요청 메시지(openai_messages, llama-server 용)로 바꾼 결과
               (도구 호출과 도구 결과가 어떤 글자로 모델에 전달되는지 - 두 번째 응답 학습에 필요)
다시 실행해야 하는 경우: dcm-back/app/prompts.py 의 TOOL_DESCRIPTIONS 나 agent.py 의 도구 함수 인자를 바꿨을 때

DB·Ollama 에는 연결하지 않습니다. (selectComplaintT 는 content="" 로만 실행해 DB 를 건드리지 않음)
"""

from __future__ import annotations

import asyncio
import json
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "tools_mode0.json"
sys.path.insert(0, str(ROOT.parent / "dcm-back"))


def _version(package: str) -> str | None:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None


def _example_messages(agent, prompts) -> dict:
    """예시 대화를 실제 도구로 실행해, ChatOllama 가 Ollama 에 보내는 메시지 모양을 그대로 남깁니다."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    from langchain_ollama import ChatOllama

    system = SystemMessage(prompts.agent_system_prompt(prompts.AGENT_MODE_INTAKE))
    cases = {
        "insert_filled": ("학산공원 벤치가 부서졌어요. 고쳐 주세요", agent.insertRequestT,
                          {"title": "공원 벤치 파손", "content": "학산공원 벤치가 부서졌어요. 고쳐 주세요"}),
        "insert_empty": ("민원 넣고 싶어요", agent.insertRequestT, {"title": "", "content": ""}),
        "select_undescribed": ("어제 넣은 거 취소해 줘", agent.selectComplaintT, {"content": ""}),
    }
    llm = ChatOllama(model="unused")   # 메시지 변환에만 씀 (요청은 보내지 않음)
    convert = getattr(llm, "_convert_messages_to_ollama_messages", None)
    result: dict = {}
    for name, (text, tool_obj, args) in cases.items():
        call = {"name": tool_obj.name, "args": args, "id": f"call_{name}", "type": "tool_call"}
        tool_message = asyncio.run(tool_obj.ainvoke(call))     # 실제 도구 실행 결과 (ToolMessage)
        messages = [system, HumanMessage(prompts.agent_user_message(text)),
                    AIMessage(content="", tool_calls=[call]), tool_message]
        entry = {"tool_message_content": tool_message.content,
                 "tool_message_content_type": type(tool_message.content).__name__}
        if convert is None:
            entry["ollama_messages"] = None
            print("경고: ChatOllama._convert_messages_to_ollama_messages 가 없어 Ollama 메시지 변환을 건너뜁니다.")
        else:
            try:
                entry["ollama_messages"] = json.loads(json.dumps(list(convert(messages)), ensure_ascii=False, default=str))
            except Exception as exc:   # 내부 함수라 버전에 따라 모양이 다를 수 있음 - 도구 스키마 저장은 계속
                entry["ollama_messages"] = None
                print(f"경고: Ollama 메시지 변환 실패 ({type(exc).__name__}: {exc})")
        # LLM_BACKEND=llama_server 일 때 ChatOpenAI 가 보내는 모양 (langchain-openai 가 설치돼 있을 때만)
        try:
            from langchain_openai.chat_models.base import _convert_message_to_dict
            entry["openai_messages"] = json.loads(json.dumps([_convert_message_to_dict(m) for m in messages], ensure_ascii=False, default=str))
        except Exception as exc:
            entry["openai_messages"] = None
            print(f"참고: OpenAI 메시지 변환을 건너뜁니다 ({type(exc).__name__}: {exc})")
        result[name] = entry
    return result


def main() -> int:
    try:
        from langchain_core.utils.function_calling import convert_to_openai_tool
    except ImportError:
        print("langchain_core 를 찾을 수 없습니다. dcm-back 가상환경에서 실행하세요.")
        return 1
    from app import agent, prompts   # agent 를 불러오면 settings 가 dcm-back/.env 를 읽습니다 (DB 에는 연결하지 않음)

    # 모드 0 에서 쓰는 도구 - agent.py 의 llm_worker 와 같은 목록·순서
    tools = [agent.insertRequestT, agent.selectComplaintT]
    # ChatOllama.bind_tools 가 쓰는 것과 같은 변환 함수
    schemas = [convert_to_openai_tool(t) for t in tools]

    # 도구 설명이 prompts.py 와 같은지 확인 (다르면 agent.py 가 설명을 제대로 못 넣은 것)
    for schema in schemas:
        name = schema["function"]["name"]
        if schema["function"].get("description") != prompts.TOOL_DESCRIPTIONS[name]:
            print(f"경고: {name} 의 description 이 prompts.TOOL_DESCRIPTIONS 와 다릅니다.")
        params = schema["function"].get("parameters", {}).get("properties", {})
        if "config" in params:
            print(f"경고: {name} 의 인자에 config 가 보입니다. (LLM 에게 숨겨져야 하는 값)")

    payload = {
        "agent_mode": prompts.AGENT_MODE_INTAKE,
        "prompt_version": prompts.PROMPT_VERSION,
        "versions": {p: _version(p) for p in ("langchain", "langchain-core", "langchain-ollama", "langchain-openai", "langgraph")},
        "tools": schemas,
        "messages": _example_messages(agent, prompts),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"저장: {OUT}")
    for schema in schemas:
        props = schema["function"].get("parameters", {}).get("properties", {})
        print(f"  - {schema['function']['name']}: 인자 {list(props)}")
    for name, entry in payload["messages"].items():
        print(f"  - 예시 {name}: 도구 결과 = {entry['tool_message_content']!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
