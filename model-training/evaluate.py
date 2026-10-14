"""
평가 - 평가셋(test 분할)으로 과제별 지표를 계산합니다.

세 가지 방식으로 같은 지표를 냅니다.
  1. HF 모델 (Colab, 학습 직후)      : hf_generator(model, tokenizer)
  2. Ollama (GGUF 로 등록한 뒤, 로컬) : ollama_generator(model_name)  <- 앱의 LLM_BACKEND=ollama 와 같은 경로
     (Ollama 의 gemma4 렌더러·파서, 분석 JSON 스키마 강제까지 포함)
  3. llama-server (베이스 + 어댑터)  : llama_server_generator(url, lora_scale)  <- 앱의 LLM_BACKEND=llama_server 와 같은 경로
     (GGUF 안의 Gemma 4 채팅 템플릿(--jinja), 도구 호출 해석, 분석 JSON 스키마 강제, 요청별 어댑터 on/off 포함)
  1 과 2·3 의 결과가 크게 다르면 그 서버의 프롬프트 조립이 학습 때와 다르다는 뜻입니다.

평가 (저장소 루트에서, 서버가 떠 있는 PC)
    python model-training/evaluate.py --ollama complaint-gemma4:latest --limit 300
    python model-training/evaluate.py --llama-server http://127.0.0.1:8080 --limit 300                 # 어댑터 켬
    python model-training/evaluate.py --llama-server http://127.0.0.1:8080 --lora-scale 0 --limit 300  # 같은 서버의 베이스 모델

지표
  챗봇 (chat_call)  : 도구 선택 정확도, 접수 content 정확 일치, 조회 content 정확 일치, 도구 없는 답의 고정 문구 일치
  챗봇 (chat_reply) : 도구 결과 뒤 고정 안내 문구 일치
  분석 (analysis)   : JSON 파싱 성공률(앱 기준 - ```json 코드블록 허용), JSON 그대로 파싱(코드블록 없음),
                      카테고리 정확도, 긴급도 정확도, 핵심어가 원문에 있는 비율,
                      요약 속 숫자가 원문·제목에 있는 비율 (숫자 단위 - 지어낸 숫자를 잡음, label_guide C3)

평가 레코드는 records.pick_balanced() 로 정답 종류(챗봇 6종, 분석: 카테고리 7종)별로 같은 수만큼 고릅니다. (반복 없음)
  모드 1 (문의)     : 점수 없이 답변만 모아 보여 줌 (학습으로 망가지지 않았는지 눈으로 확인)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

import common
from app import prompts
import records as records_mod
from samples import analysis_conversation, chat_conversations, load_tools, render, turn_end_id

# 평가 방식이 바뀌면 올립니다. 노트북의 학습 전 평가 캐시 파일 이름에 들어가, 예전 방식의 결과를 다시 불러오지 않게 합니다.
#   eval-v1: 처음 방식 (분석 출력을 json.loads 로 그대로 읽음, id 해시 순서로 평가 레코드 선택)
#   eval-v2: 분석 출력을 앱(ai.py)처럼 코드블록을 벗겨 읽음 + 'JSON 그대로 파싱' 지표 추가 + 생성 잘림 감지
#            + 평가 레코드를 정답 종류별로 고르게 선택(pick_stratified)
#   eval-v3: '형식 통과율'·'is_described 정확도' 제거, '요약 속 숫자가 원문·제목에 있는 비율' 추가,
#            평가 레코드를 정답 종류별로 같은 수만큼 선택(records.pick_balanced, 챗봇 6종)
EVAL_VERSION = "eval-v3"

MODE1_QUESTIONS = [
    "여기서는 무슨 일을 하나요?",
    "민원 처리는 보통 얼마나 걸려요?",
    "전입신고는 어디서 하나요?",
    "맛집 추천해 줘",
]


# =============================================================================
# 출력 해석
# =============================================================================
# Gemma 4 고유의 도구 호출 형식: <|tool_call>call:이름{키:<|"|>값<|"|>,키2:true}<tool_call|>
_GEMMA_CALL = re.compile(r"<\|tool_call>\s*call:([\w.\-]+)\s*(\{.*?\})\s*<tool_call\|>", re.DOTALL)
_GEMMA_STR = re.compile(r'<\|"\|>(.*?)<\|"\|>', re.DOTALL)
_BARE_KEY = re.compile(r'([{,]\s*)([A-Za-z_][\w\-]*)\s*:')
_THINKING = re.compile(r"<\|channel>.*?<channel\|>", re.DOTALL)


def parse_gemma_arguments(body: str) -> dict | None:
    strings: list[str] = []

    def keep(match: re.Match) -> str:
        strings.append(match.group(1))
        return f"\x00{len(strings) - 1}\x00"

    text = _GEMMA_STR.sub(keep, body.strip())
    text = _BARE_KEY.sub(lambda m: f'{m.group(1)}"{m.group(2)}":', text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: json.dumps(strings[int(m.group(1))], ensure_ascii=False), text)
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def parse_raw(raw: str, special_tokens: list[str]) -> dict:
    """HF 생성 원문 -> {"tool", "arguments", "text", "raw"}"""
    text = _THINKING.sub("", raw)
    calls = _GEMMA_CALL.findall(text)
    tool, args = (None, None)
    if calls:
        tool, args = calls[0][0], parse_gemma_arguments(calls[0][1])
        text = _GEMMA_CALL.sub("", text)
    for tok in special_tokens:
        text = text.replace(tok, "")
    return {"tool": tool, "arguments": args, "text": text.strip(), "raw": raw}


_FENCE = re.compile(r"^\s*```[A-Za-z]*\s*\n?|\n?\s*```\s*$")


def is_plain_json(text: str) -> bool:
    """코드블록·설명 없이 출력 전체가 그대로 JSON 객체로 읽히는지. (형식을 배웠는지 보는 엄격한 기준)"""
    try:
        return isinstance(json.loads(text), dict)
    except ValueError:
        return False


def parse_json_output(text: str) -> dict | None:
    """
    분석 출력 -> dict. 앱(dcm-back/app/ai.py)과 같은 기준으로 읽습니다.
      1) 그대로 json.loads
      2) ```json ... ``` 코드블록 표시를 벗기고 다시 시도 (ai.py 의 .replace("```json", "").replace("```", "") 와 같은 효과)
      3) 그래도 안 되면 첫 '{' ~ 마지막 '}' 구간만 시도 (앞뒤에 설명 문장이 붙은 경우)
    끝까지 안 되면 None. (생성이 중간에 잘려 '}' 가 없는 경우 등)
    """
    if not isinstance(text, str):
        return None
    stripped = _FENCE.sub("", text.strip()).strip()
    candidates = [text, stripped, text.replace("```json", "").replace("```", "").strip()]
    start, end = stripped.find("{"), stripped.rfind("}")
    if 0 <= start < end:
        candidates.append(stripped[start:end + 1])
    for cand in candidates:
        try:
            value = json.loads(cand)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


# =============================================================================
# 생성기 (HF / Ollama) - gen(messages, tools=None, schema=None) -> dict
# =============================================================================
def hf_generator(model, tokenizer, max_new_tokens: int = 512):
    import torch

    specials = sorted({t for t in (tokenizer.all_special_tokens or []) if t}
                      | {t.content for t in getattr(tokenizer, "added_tokens_decoder", {}).values()
                         if getattr(t, "special", False) and t.content}, key=len, reverse=True)
    stop = [turn_end_id(tokenizer)] + ([tokenizer.eos_token_id] if tokenizer.eos_token_id is not None else [])

    def gen(messages: list[dict], tools: list[dict] | None = None, schema: dict | None = None) -> dict:
        text = render(tokenizer, messages, tools, add_generation_prompt=True)
        enc = tokenizer(text, add_special_tokens=False, return_tensors="pt").to(model.device)
        with torch.inference_mode():
            out = model.generate(**enc, max_new_tokens=max_new_tokens, do_sample=False, eos_token_id=stop,
                                 pad_token_id=tokenizer.pad_token_id, use_cache=True)
        new_ids = out[0][enc["input_ids"].shape[1]:]
        raw = tokenizer.decode(new_ids, skip_special_tokens=False)
        result = parse_raw(raw, specials)
        # 끝 토큰 없이 max_new_tokens 를 다 쓰고 멈췄으면 '잘림' (분석 JSON 이 닫히지 않은 원인 구분용)
        result["truncated"] = len(new_ids) >= max_new_tokens and int(new_ids[-1]) not in stop
        return result

    return gen


def ollama_generator(model_name: str, url: str = "http://127.0.0.1:11434", num_ctx: int = 8192, timeout: int = 300):
    """앱과 같은 방식으로 /api/chat 을 부릅니다. (챗봇: tools, 분석: format=JSON 스키마)"""

    def gen(messages: list[dict], tools: list[dict] | None = None, schema: dict | None = None) -> dict:
        body: dict = {"model": model_name, "messages": [_to_ollama(m) for m in messages], "stream": False,
                      "options": {"temperature": 0, "num_ctx": num_ctx}}
        if tools:
            body["tools"] = tools
        if schema:
            body["format"] = schema
            body["options"].update({"top_p": 0.1, "num_predict": 512})
        req = urllib.request.Request(f"{url}/api/chat", data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        msg = data.get("message") or {}
        calls = msg.get("tool_calls") or []
        tool = calls[0]["function"]["name"] if calls else None
        args = calls[0]["function"].get("arguments") if calls else None
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = None
        return {"tool": tool, "arguments": args, "text": (msg.get("content") or "").strip(), "raw": data,
                "truncated": data.get("done_reason") == "length"}

    return gen


def llama_server_generator(url: str = "http://127.0.0.1:8080", lora_scale: float = 1.0, lora_id: int = 0, timeout: int = 300):
    """앱(dcm-back/app/llm.py)과 같은 방식으로 /v1/chat/completions 를 부릅니다.
    (챗봇: tools, 분석: response_format=JSON 스키마, 요청마다 lora 세기, 사고 모드 끔)"""
    url = url.rstrip("/")

    def gen(messages: list[dict], tools: list[dict] | None = None, schema: dict | None = None) -> dict:
        body: dict = {"model": "eval", "messages": [_to_openai(m) for m in messages], "stream": False, "temperature": 0,
                      "lora": [{"id": lora_id, "scale": lora_scale}],
                      "chat_template_kwargs": {"enable_thinking": common.ENABLE_THINKING}}
        if tools:
            body["tools"] = tools
        if schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "analysis", "schema": schema}}
            body.update({"top_p": 0.1, "max_tokens": 512})
        req = urllib.request.Request(f"{url}/v1/chat/completions", data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls = msg.get("tool_calls") or []
        tool = calls[0]["function"]["name"] if calls else None
        args = calls[0]["function"].get("arguments") if calls else None
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = None
        return {"tool": tool, "arguments": args, "text": (msg.get("content") or "").strip(), "raw": data,
                "truncated": choice.get("finish_reason") == "length"}

    return gen


def check_llama_server(url: str, lora_id: int = 0) -> list[dict]:
    """평가 전에 서버·어댑터가 떠 있는지 확인합니다. 어댑터가 없으면 멈춥니다. (없으면 조용히 베이스 모델 점수가 나옴)"""
    url = url.rstrip("/")
    with urllib.request.urlopen(f"{url}/health", timeout=10) as resp:
        resp.read()
    with urllib.request.urlopen(f"{url}/lora-adapters", timeout=10) as resp:
        adapters = json.loads(resp.read().decode("utf-8"))
    if lora_id not in [a.get("id") for a in adapters if isinstance(a, dict)]:
        raise SystemExit(f"llama-server 에 어댑터 id={lora_id} 가 없습니다: {adapters}  (--lora 로 어댑터를 올려 띄웠는지 확인)")
    return adapters


def _to_openai(m: dict) -> dict:
    """학습용 메시지 -> OpenAI 호환 API 메시지. (LangChain ChatOpenAI 처럼 도구 인자는 JSON 문자열로 보냄)"""
    out = {"role": m["role"], "content": m.get("content") or ""}
    if m.get("tool_calls"):
        out["tool_calls"] = [{"type": "function", "id": c.get("id"),
                              "function": {"name": c["function"]["name"],
                                           "arguments": c["function"]["arguments"] if isinstance(c["function"]["arguments"], str)
                                           else json.dumps(c["function"]["arguments"], ensure_ascii=False)}}
                             for c in m["tool_calls"]]
    if m["role"] == "tool":
        out["tool_call_id"] = m.get("tool_call_id")
    return out


def _to_ollama(m: dict) -> dict:
    """학습용 메시지(OpenAI 형식) -> LangChain 이 Ollama 에 보내는 모양 (export_prompts.py 결과 참고)."""
    out = {"role": m["role"], "content": m.get("content") or ""}
    if m.get("tool_calls"):
        out["tool_calls"] = [{"type": "function", "id": c.get("id"),
                              "function": {"name": c["function"]["name"], "arguments": c["function"]["arguments"]}}
                             for c in m["tool_calls"]]
    if m["role"] == "tool":
        out["tool_call_id"] = m.get("tool_call_id")
    return out


# =============================================================================
# 지표
# =============================================================================
# 정답 종류 - records.py 에 있는 것을 그대로 씁니다. (학습 데이터 선택과 같은 기준)
chat_label = records_mod.chat_kind          # insert / insert_empty / select / select_empty / reject / inquiry_redirect
analysis_label = records_mod.analysis_kind  # 카테고리 7종


def pick_eval(rows: list[dict], limit: int | None, key) -> list[dict]:
    """평가 레코드 선택 - 종류별로 같은 수, 반복 없음. 같은 limit 이면 항상 같은 레코드."""
    return records_mod.pick_balanced(rows, limit, key, max_repeat=1)


def _rate(hit: int, total: int) -> dict:
    return {"value": round(hit / total, 4) if total else None, "hit": hit, "total": total}


def evaluate_chat(gen, chat_records: list[dict], limit: int | None = None, reply_limit: int = 100, label: str = "") -> dict:
    """label: 진행 줄 앞에 붙는 이름 (예: '학습 전')"""
    tools = load_tools()
    recs = pick_eval(chat_records, limit, chat_label)
    tag = f"[{label}] " if label else ""
    c = Counter()
    confusion = Counter()
    per_kind: dict[str, list[int]] = {}
    for r in recs:
        per_kind.setdefault(chat_label(r), [0, 0])
    mistakes: list[dict] = []
    for r in common.progress(recs, f"{tag}챗봇 도구 호출"):
        (task, messages, _target), *_ = chat_conversations(r, reply_ratio=0.0)
        out = gen(messages, tools=tools)
        exp_tool, got_tool = r.get("tool"), out["tool"]
        confusion[(chat_label(r), got_tool or "(도구 없음)")] += 1
        c["n"] += 1
        ok = exp_tool == got_tool
        c["tool_ok"] += ok
        per_kind[chat_label(r)][0] += ok
        per_kind[chat_label(r)][1] += 1
        args = out["arguments"] or {}
        if exp_tool == "insertRequestT":
            c["ins_n"] += 1
            if ok:
                c["ins_content_ok"] += args.get("content") == r["arguments"]["content"]
        elif exp_tool == "selectComplaintT":
            c["sel_n"] += 1
            if ok:
                c["sel_content_ok"] += args.get("content") == r["arguments"]["content"]
        else:
            c["none_n"] += 1
            c["none_text_ok"] += ok and out["text"] == prompts.REPLY_TEXTS[r["reply_kind"]]
        if not ok and len(mistakes) < 30:
            mistakes.append({"id": r["id"], "text": r["text"], "expected": chat_label(r),
                             "got": got_tool, "got_text": out["text"][:120]})

    # 도구 결과 뒤 안내 문장 (정답 대화를 그대로 넣고 마지막 답만 생성)
    reply_n = reply_ok = 0
    replies = []
    for r in recs:
        convs = [x for x in chat_conversations(r, reply_ratio=1.0) if x[0] == "chat_reply"]
        if convs:
            replies.append(convs[0])
        if len(replies) >= reply_limit:
            break
    for _, messages, target in common.progress(replies, f"{tag}챗봇 안내 문구"):
        out = gen(messages, tools=tools)
        reply_n += 1
        reply_ok += out["tool"] is None and out["text"] == target["content"]

    return {
        "도구 선택 정확도": _rate(c["tool_ok"], c["n"]),
        "접수 content 정확 일치": _rate(c["ins_content_ok"], c["ins_n"]),
        "조회 content 정확 일치": _rate(c["sel_content_ok"], c["sel_n"]),
        "도구 없는 답 고정 문구 일치": _rate(c["none_text_ok"], c["none_n"]),
        "도구 결과 뒤 안내 문구 일치": _rate(reply_ok, reply_n),
        # 정답 종류별 도구 선택 정확도 - 거절이 도구 호출로 바뀌는 쏠림을 여기서 봅니다
        "_종류별 도구 선택(맞음/전체)": {k: f"{h}/{n}" for k, (h, n) in sorted(per_kind.items())},
        "_confusion(정답, 예측)": {f"{k[0]} -> {k[1]}": v for k, v in confusion.most_common()},
        "_틀린 예": mistakes,
    }


def evaluate_analysis(gen, analysis_records: list[dict], limit: int | None = None, label: str = "") -> dict:
    """
    'JSON 파싱 성공률' 은 앱(ai.py)이 받아들이는 기준 - ```json 코드블록을 벗겨서라도 읽히면 성공.
    'JSON 그대로 파싱' 은 출력 전체가 그대로 JSON 인 비율 - 코드블록 없이 쓰는 형식을 배웠는지 봅니다.
    (HF 평가는 Ollama 처럼 JSON 스키마를 강제하지 않으므로, 베이스 모델은 보통 ```json 으로 감쌉니다)
    """
    recs = pick_eval(analysis_records, limit, analysis_label)
    tag = f"[{label}] " if label else ""
    c = Counter()
    mistakes: list[dict] = []
    for r in common.progress(recs, f"{tag}민원 분석"):
        _, messages, _ = analysis_conversation(r)
        out = gen(messages, schema=prompts.ANALYSIS_JSON_SCHEMA)
        c["n"] += 1
        c["plain_ok"] += is_plain_json(out["text"])
        c["truncated"] += bool(out.get("truncated"))
        parsed = parse_json_output(out["text"])
        if parsed is None:
            if len(mistakes) < 30:
                problem = "생성 잘림(max_new_tokens)" if out.get("truncated") else "JSON 아님"
                mistakes.append({"id": r["id"], "problem": problem, "text": out["text"][:200]})
            continue
        c["json_ok"] += 1
        c["cat_ok"] += parsed.get("category") == r["output"]["category"]
        c["urg_ok"] += parsed.get("urgency") == r["output"]["urgency"]
        seen = prompts.analysis_content(r["content"])
        kws = parsed.get("keywords") if isinstance(parsed.get("keywords"), list) else []
        c["kw_n"] += len(kws)
        c["kw_in"] += sum(1 for k in kws if isinstance(k, str) and k and k in seen)
        summary = parsed.get("summary") if isinstance(parsed.get("summary"), str) else ""
        nums = records_mod.numbers_in(summary)
        missing = records_mod.summary_numbers_missing(r["title"], r["content"], summary)
        c["num_n"] += len(nums)
        c["num_in"] += len(nums) - len(missing)
        wrong_cat = parsed.get("category") != r["output"]["category"]
        if (wrong_cat or missing) and len(mistakes) < 30:
            mistakes.append({"id": r["id"], "problem": "카테고리 틀림" if wrong_cat else "요약에 원문에 없는 숫자",
                             "expected_category": r["output"]["category"], "got_category": parsed.get("category"),
                             "summary_numbers_missing": missing, "summary": summary[:150]})
    return {
        "JSON 파싱 성공률": _rate(c["json_ok"], c["n"]),
        "JSON 그대로 파싱(코드블록 없음)": _rate(c["plain_ok"], c["n"]),
        "카테고리 정확도": _rate(c["cat_ok"], c["n"]),
        "긴급도 정확도": _rate(c["urg_ok"], c["n"]),
        "핵심어가 원문에 있는 비율": _rate(c["kw_in"], c["kw_n"]),
        # 요약에 숫자가 하나도 없으면 분모에 들어가지 않습니다. (건수는 '맞음/전체' 의 전체 = 요약 속 숫자 개수)
        "요약 속 숫자가 원문·제목에 있는 비율": _rate(c["num_in"], c["num_n"]),
        "_생성 잘림 건수": c["truncated"],
        "_틀린 예": mistakes,
    }


def mode1_answers(gen) -> list[dict]:
    """모드 1(문의) - 학습하지 않은 모드가 망가지지 않았는지 눈으로 확인할 답변."""
    system = {"role": "system", "content": prompts.agent_system_prompt(prompts.AGENT_MODE_INQUIRY)}
    rows = []
    for q in MODE1_QUESTIONS:
        out = gen([system, {"role": "user", "content": prompts.agent_user_message(q)}], tools=None)
        rows.append({"질문": q, "답변": out["text"][:300], "도구": out["tool"]})
    return rows


def compare_table(before: dict, after: dict) -> list[dict]:
    """학습 전·후 지표 비교표. before/after 는 {"chat": {...}, "analysis": {...}} (evaluate_* 결과)."""
    rows = []
    for task, metrics in after.items():
        for name, a in metrics.items():
            if name.startswith("_"):
                continue
            b = (before.get(task) or {}).get(name) or {}
            bv, av = b.get("value"), a.get("value")
            rows.append({
                "과제": task, "지표": name,
                "학습 전": bv, "학습 후": av,
                "변화(%p)": round((av - bv) * 100, 1) if isinstance(av, (int, float)) and isinstance(bv, (int, float)) else None,
                "건수": a.get("total"),
            })
    return rows


def test_split(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["split"] == "test"]


def summary_table(metrics: dict) -> list[dict]:
    """노트북 표시용 - '_' 로 시작하는 상세 항목은 뺌."""
    return [{"지표": k, "값": v["value"], "맞음/전체": f"{v['hit']}/{v['total']}"}
            for k, v in metrics.items() if not k.startswith("_")]


# =============================================================================
# 명령줄 - Ollama / llama-server 평가
# =============================================================================
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Ollama 또는 llama-server 에 올린 모델을 평가셋으로 평가합니다.")
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--ollama", help="Ollama 모델 이름 (예: complaint-gemma4:latest)")
    target.add_argument("--llama-server", help="llama-server 주소 (예: http://127.0.0.1:8080)")
    ap.add_argument("--url", default="http://127.0.0.1:11434", help="[ollama] Ollama 주소")
    ap.add_argument("--lora-scale", type=float, default=1.0, help="[llama-server] 어댑터 세기. 1=학습 모델, 0=베이스 모델")
    ap.add_argument("--lora-id", type=int, default=0, help="[llama-server] 어댑터 순번 (첫 번째 --lora 가 0)")
    ap.add_argument("--limit", type=int, default=300, help="챗봇 평가 레코드 수 (0 이면 전부)")
    ap.add_argument("--num-ctx", type=int, default=8192, help="[ollama] 컨텍스트 길이 (llama-server 는 서버 실행 옵션 -c 를 씀)")
    a = ap.parse_args(argv)

    if a.ollama:
        gen, name, label = ollama_generator(a.ollama, a.url, a.num_ctx), a.ollama, "Ollama"
        out_name = f"eval_ollama_{a.ollama.replace(':', '_').replace('/', '_')}.json"
    else:
        adapters = check_llama_server(a.llama_server, a.lora_id)
        print(f"llama-server 어댑터: {adapters} -> 이번 평가 세기 {a.lora_scale}")
        gen = llama_server_generator(a.llama_server, a.lora_scale, a.lora_id)
        name, label = f"llama-server lora{a.lora_id}x{a.lora_scale:g}", f"llama-server x{a.lora_scale:g}"
        out_name = f"eval_llama_server_lora{a.lora_id}_scale{a.lora_scale:g}.json"
    started = time.time()
    result: dict = {"model": name, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"), "eval_version": EVAL_VERSION}
    chat = test_split(records_mod.load_chat_records())
    result["chat"] = evaluate_chat(gen, chat, a.limit or None, label=label)
    if common.ANALYSIS_RECORDS.exists():
        analysis = test_split(records_mod.load_analysis_records())
        if analysis:
            result["analysis"] = evaluate_analysis(gen, analysis, a.limit or None, label=label)
    result["mode1"] = mode1_answers(gen)

    for task in ("chat", "analysis"):
        if task in result:
            print(f"\n[{task}]")
            for row in summary_table(result[task]):
                print(f"  {row['지표']:<24} {row['값']}  ({row['맞음/전체']})")
    print("\n[모드 1 답변]")
    for row in result["mode1"]:
        print(f"  Q: {row['질문']}\n  A: {row['답변']}")
    out = common.OUTPUT_DIR / out_name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n저장: {out} ({time.time() - started:.0f}초)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
