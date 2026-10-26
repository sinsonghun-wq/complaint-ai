"""
학습 샘플 만들기 - 정답 레코드를 '실행 때 모델이 받는 입력 + 정답 출력' 토큰으로 바꿉니다.

입력 조립 원칙 (실행 때와 글자까지 같게)
  - 챗봇 : 시스템 프롬프트(prompts.agent_system_prompt(0)) + 도구 목록(data/tools_mode0.json) + 사용자 발화
           도구 결과 메시지는 LangChain 이 보내는 그대로 json.dumps(..., ensure_ascii=False) 문자열
           (export_prompts.py 로 실제 모양을 확인함)
  - 분석 : 사용자 메시지 하나 = app.ai.prompt(title, content)  (앱과 같은 함수 - 공백 정리·개인정보 가림·1,500자 자르기 포함)
  - 문장 -> 토큰은 Gemma 4 공식 채팅 템플릿(tokenizer.apply_chat_template)으로 조립합니다.
    Ollama 의 gemma4 렌더러가 같은 형식으로 조립하는지는 노트북의 '렌더링 확인'과 Ollama 평가로 확인합니다.

정답(loss 를 거는 부분)
  - 어시스턴트가 이번에 쓸 부분만 (도구 호출 또는 안내 문장 또는 분석 JSON) + 발화 끝 토큰 <turn|>
  - 그 앞(시스템·도구 목록·사용자 발화·이전 대화)은 loss 에서 뺍니다.

한 챗봇 레코드에서 나오는 샘플
  chat_call  : [system, user] -> 도구 호출 (도구가 없는 레코드는 고정 안내 문장)
  chat_reply : [system, user, 도구 호출, 도구 결과] -> 고정 안내 문장 (A10~A14)
               - 도구 레코드 중 reply_ratio 비율만 만듭니다. (문장이 고정이라 많이 필요 없음)
               - 조회 결과(민원 목록)는 가짜로 만듭니다. content 가 있으면 레코드마다 '찾음/못 찾음' 중 하나.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from dataclasses import dataclass

import common  # noqa: F401  (sys.path 에 dcm-back 추가)
from app import categories, prompts

IGNORE = -100
TURN_END = "<turn|>"   # Gemma 4 의 어시스턴트 발화 끝 토큰


# =============================================================================
# 샘플
# =============================================================================
@dataclass
class Sample:
    task: str                 # chat_call / chat_reply / analysis
    record_id: str
    input_ids: list[int]
    labels: list[int]
    answer_text: str = ""     # 정답 문자열 (확인용)

    @property
    def length(self) -> int:
        return len(self.input_ids)

    @property
    def answer_tokens(self) -> int:
        return sum(1 for x in self.labels if x != IGNORE)

    def to_dict(self) -> dict:
        return {"input_ids": self.input_ids, "labels": self.labels}


def load_tools() -> list[dict]:
    data = json.loads(common.TOOLS_FILE.read_text(encoding="utf-8"))
    tools = data["tools"]
    names = [t["function"]["name"] for t in tools]
    if names != ["insertRequestT", "selectComplaintT"]:
        raise ValueError(f"tools_mode0.json 의 도구가 예상과 다릅니다: {names}")
    # 버전 이름이 아니라 내용으로 비교합니다. (분석 프롬프트만 바뀐 경우엔 도구 파일을 다시 뽑을 필요가 없음)
    system = prompts.agent_system_prompt(prompts.AGENT_MODE_INTAKE)
    for name, entry in (data.get("messages") or {}).items():
        msgs = (entry or {}).get("ollama_messages") or []
        if msgs and msgs[0].get("role") == "system" and msgs[0].get("content") != system:
            raise ValueError("tools_mode0.json 의 모드 0 시스템 프롬프트가 prompts.py 와 다릅니다. export_prompts.py 를 다시 실행하세요.")
    for t in tools:
        if t["function"]["description"] != prompts.TOOL_DESCRIPTIONS[t["function"]["name"]]:
            raise ValueError("tools_mode0.json 의 도구 설명이 prompts.py 와 다릅니다. export_prompts.py 를 다시 실행하세요.")
    return tools


# =============================================================================
# 채팅 템플릿
# =============================================================================
def render(tokenizer, messages: list[dict], tools: list[dict] | None, add_generation_prompt: bool) -> str:
    kwargs = {"tokenize": False, "add_generation_prompt": add_generation_prompt}
    if tools:
        kwargs["tools"] = tools
    try:
        return tokenizer.apply_chat_template(messages, enable_thinking=common.ENABLE_THINKING, **kwargs)
    except TypeError:
        return tokenizer.apply_chat_template(messages, **kwargs)


def turn_end_id(tokenizer) -> int:
    tid = tokenizer.convert_tokens_to_ids(TURN_END)
    if isinstance(tid, int) and tid >= 0 and tid != getattr(tokenizer, "unk_token_id", None):
        return tid
    return tokenizer.eos_token_id


def _encode(tokenizer, text: str) -> list[int]:
    # 템플릿이 <bos> 를 문자열로 넣으므로 특수 토큰을 다시 붙이지 않습니다.
    return tokenizer(text, add_special_tokens=False)["input_ids"]


def build_sample(tokenizer, task: str, record_id: str, messages: list[dict], target: dict,
                 tools: list[dict] | None, max_length: int = common.MAX_LENGTH) -> Sample | None:
    """
    messages 뒤에 target(어시스턴트 메시지)이 이어지는 대화를 렌더링해, target 부분만 정답으로 표시합니다.
    정답은 발화 끝 토큰(<turn|>)까지만 남깁니다. 너무 길면 None.
    """
    prompt_text = render(tokenizer, messages, tools, add_generation_prompt=True)
    full_text = render(tokenizer, messages + [target], tools, add_generation_prompt=False)
    if not full_text.startswith(prompt_text):
        raise ValueError(
            f"[{record_id}] 채팅 템플릿이 '질문 + 생성 시작' 을 '질문 + 정답' 의 앞부분으로 만들지 않습니다. "
            "템플릿 버전을 확인하세요."
        )
    answer_text = full_text[len(prompt_text):]
    cut = answer_text.find(TURN_END)
    if cut >= 0:
        answer_text = answer_text[:cut + len(TURN_END)]

    prompt_ids = _encode(tokenizer, prompt_text)
    answer_ids = _encode(tokenizer, answer_text)
    if not answer_ids or answer_ids[-1] != turn_end_id(tokenizer):
        answer_ids = answer_ids + [turn_end_id(tokenizer)]
    input_ids = prompt_ids + answer_ids
    if len(input_ids) > max_length:
        return None
    labels = [IGNORE] * len(prompt_ids) + answer_ids
    return Sample(task, record_id, input_ids, labels, answer_text)


# =============================================================================
# 대화 만들기
# =============================================================================
def _seed(*parts: str) -> int:
    return int(hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest(), 16) % (2 ** 32)


def _fake_rows(record: dict, rng: random.Random) -> list[dict]:
    """조회 도구가 돌려줄 가짜 민원 목록. (agent.selectComplaintT 의 SELECT 컬럼·순서와 같게)"""
    keyword = record["arguments"]["content"]
    rows = []
    for _ in range(rng.randint(1, 3)):
        day = rng.randint(1, 28)
        rows.append({
            "id": rng.randint(10, 9999),
            "title": f"{keyword} {rng.choice(['고장', '불편', '문제', '민원', '신고'])}",
            "content": f"{keyword} 관련해서 {rng.choice(['불편이 있습니다', '처리 부탁드립니다', '확인 부탁드립니다'])}.",
            "category": rng.choice(categories.NAMES),
            "created_at": f"2026-{rng.randint(1, 9):02d}-{day:02d} {rng.randint(8, 21):02d}:{rng.randint(0, 59):02d}:00",
            "complaint_status": rng.choice(["접수", "진행중", "완료", "취소"]),
        })
    return rows


def chat_conversations(record: dict, reply_ratio: float) -> list[tuple[str, list[dict], dict]]:
    """챗봇 레코드 -> [(task, messages, target)] - 1~2개."""
    system = {"role": "system", "content": prompts.agent_system_prompt(prompts.AGENT_MODE_INTAKE)}
    user = {"role": "user", "content": prompts.agent_user_message(record["text"])}
    base = [system, user]
    tool = record.get("tool")
    if tool is None:
        reply = {"role": "assistant", "content": prompts.REPLY_TEXTS[record["reply_kind"]]}
        return [("chat_call", base, reply)]

    args = record["arguments"]
    call_id = f"call_{record['id']}"
    call_msg = {"role": "assistant", "content": "",
                "tool_calls": [{"id": call_id, "type": "function", "function": {"name": tool, "arguments": args}}]}
    out = [("chat_call", base, call_msg)]

    rng = random.Random(_seed("reply", record["id"]))
    if rng.random() >= reply_ratio:
        return out
    if tool == "insertRequestT":
        result = {"code": 1, "result": [{"title": args["title"], "content": args["content"]}]}
        kind = "insert_filled" if args["content"] else "insert_empty"
    elif not args["content"]:
        result, kind = {"code": 2, "result": []}, "select_undescribed"
    elif rng.random() < 0.7:
        result, kind = {"code": 2, "result": _fake_rows(record, rng)}, "select_found"
    else:
        result, kind = {"code": 2, "result": []}, "select_none"
    tool_msg = {"role": "tool", "name": tool, "tool_call_id": call_id,
                "content": json.dumps(result, ensure_ascii=False)}   # LangChain 이 보내는 모양 그대로
    out.append(("chat_reply", base + [call_msg, tool_msg],
                {"role": "assistant", "content": prompts.REPLY_TEXTS[kind]}))
    return out


def analysis_conversation(record: dict) -> tuple[str, list[dict], dict]:
    from app.ai import prompt as analysis_user_prompt   # 앱과 같은 함수 (clean·redact·1,500자 자르기 포함)

    messages = [{"role": "user", "content": analysis_user_prompt(record["title"], record["content"])}]
    output = {key: record["output"][key] for key in prompts.ANALYSIS_FIELDS}   # 스키마 순서대로
    return "analysis", messages, {"role": "assistant", "content": json.dumps(output, ensure_ascii=False)}


# =============================================================================
# 전체
# =============================================================================
def build_samples(tokenizer, chat_records: list[dict], analysis_records: list[dict], reply_ratio: float = 0.5,
                  analysis_repeat: int = 1, no_tool_repeat: int = 1,
                  max_length: int = common.MAX_LENGTH) -> tuple[list[Sample], dict]:
    """
    종류별 비율·반복은 보통 records.pick_balanced() 로 레코드를 고를 때 맞춥니다. (노트북 5번 셀)
    그렇게 골랐다면 아래 두 값은 기본값 1 로 둡니다. (한 번 더 반복하면 비율이 다시 틀어짐)
    analysis_repeat : 분석 샘플을 몇 번 반복해 넣을지
    no_tool_repeat  : 도구 없이 답하는 챗봇 레코드(reject·inquiry_redirect)의 샘플을 몇 번 반복해 넣을지
    """
    tools = load_tools()
    samples: list[Sample] = []
    too_long: Counter = Counter()
    for record in common.progress(chat_records, "학습 샘플 - 챗봇"):
        repeat = max(1, no_tool_repeat) if record.get("tool") is None else 1
        for task, messages, target in chat_conversations(record, reply_ratio):
            s = build_sample(tokenizer, task, record["id"], messages, target, tools, max_length)
            if s is None:
                too_long[task] += 1
            else:
                samples.extend([s] * repeat)
    for record in (common.progress(analysis_records, "학습 샘플 - 분석") if analysis_records else []):
        task, messages, target = analysis_conversation(record)
        s = build_sample(tokenizer, task, record["id"], messages, target, None, max_length)
        if s is None:
            too_long[task] += 1
            continue
        samples.extend([s] * max(1, analysis_repeat))
    counts = Counter(s.task for s in samples)
    no_tool_ids = {r["id"] for r in chat_records if r.get("tool") is None}
    lengths = [s.length for s in samples] or [0]
    stats = {
        "samples": dict(counts),
        "chat_call_no_tool": sum(1 for s in samples if s.task == "chat_call" and s.record_id in no_tool_ids),
        "too_long_dropped": dict(too_long),
        "max_length": max(lengths),
        "mean_length": round(sum(lengths) / len(lengths), 1),
        "answer_tokens_mean": round(sum(s.answer_tokens for s in samples) / max(len(samples), 1), 1),
    }
    return samples, stats


def show(tokenizer, sample: Sample, max_chars: int = 2500) -> str:
    """샘플을 사람이 읽을 수 있게: 입력 부분과 정답 부분(【 】)을 나눠 보여 줍니다."""
    cut = next((i for i, x in enumerate(sample.labels) if x != IGNORE), len(sample.labels))
    prompt = tokenizer.decode(sample.input_ids[:cut], skip_special_tokens=False)
    answer = tokenizer.decode(sample.input_ids[cut:], skip_special_tokens=False)
    if len(prompt) > max_chars:
        prompt = prompt[: max_chars // 2] + "\n…(중략)…\n" + prompt[-max_chars // 2:]
    return f"[{sample.task} | {sample.record_id} | 총 {sample.length}토큰, 정답 {sample.answer_tokens}토큰]\n{prompt}【{answer}】"
