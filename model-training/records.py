"""
학습 데이터 검증기 - data/label_guide.md 의 '자동 검증' 규칙을 코드로 옮긴 것입니다.

    python model-training/records.py                         # data/ 의 chat_records.jsonl, analysis_records.jsonl 검사
    python model-training/records.py data/chat_records.jsonl # 파일을 지정해서 검사

파일 이름으로 종류를 구분합니다. (chat_records*.jsonl = 과제 1, analysis_records*.jsonl = 과제 2·3)
오류가 하나라도 있으면 종료 코드 1 로 끝나므로, 학습 전에 반드시 통과시키세요.

학습 코드는 load_chat_records() / load_analysis_records() / assign_split() 을 가져다 씁니다.
카테고리·안내 문구·분석 필드는 dcm-back/app 의 categories.py / prompts.py 를 그대로 읽으므로, 앱과 기준이 같습니다.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
sys.path.insert(0, str(ROOT.parent / "dcm-back"))

from app import categories, prompts  # noqa: E402  (dcm-back/app - 외부 패키지 없이 읽힘)

SPLITS = ("train", "val", "test")
SPLIT_RATIOS = (0.7, 0.15, 0.15)          # backend training/records.py 와 같은 비율·같은 해시

CHAT_TOOLS = ("insertRequestT", "selectComplaintT")
CHAT_REPLY_KINDS = ("reject", "inquiry_redirect")      # 도구 없이 답하는 경우 (label_guide A15, A16)
CHAT_FIELDS = {"id", "group", "text", "tool", "arguments", "reply_kind", "source", "split", "tags"}

ANALYSIS_FIELDS = {"id", "group", "title", "content", "output", "source", "split", "tags"}
TITLE_MAX_CHARS = 20
KEYWORDS_RANGE = (1, 5)            # 3~5개 권장, 원문이 짧으면 1~2개 허용 (label_guide C6)
KEY_POINTS_RANGE = (1, 3)


# =============================================================================
# 읽기 / 분할
# =============================================================================
def read_jsonl(path: Path) -> tuple[list[dict], list[str]]:
    rows, errors = [], []
    for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"{path.name}:{no} JSON 형식 오류 - {exc}")
            continue
        if not isinstance(row, dict):
            errors.append(f"{path.name}:{no} 한 줄이 JSON 객체가 아닙니다")
            continue
        row["_line"] = no
        rows.append(row)
    return rows, errors


def assign_split(record: dict) -> str:
    """split 을 지정했으면 그대로, 아니면 group 해시로 정합니다. (데이터를 더 넣어도 기존 배정이 바뀌지 않음)"""
    if record.get("split") in SPLITS:
        return record["split"]
    group = str(record.get("group") or record["id"])
    bucket = int(hashlib.sha1(group.encode("utf-8")).hexdigest(), 16) % 10_000 / 10_000
    if bucket < SPLIT_RATIOS[0]:
        return "train"
    if bucket < SPLIT_RATIOS[0] + SPLIT_RATIOS[1]:
        return "val"
    return "test"


# =============================================================================
# 과제 1 - 챗봇 모드 0 (chat_records.jsonl)
# =============================================================================
def check_chat(row: dict) -> list[str]:
    e: list[str] = []
    text = row.get("text")
    if not isinstance(text, str) or not text.strip():
        e.append("text 가 비어 있습니다")
        return e
    extra = set(row) - CHAT_FIELDS - {"_line"}
    if extra:
        e.append(f"알 수 없는 필드 {sorted(extra)}")
    tool, args = row.get("tool"), row.get("arguments")

    if tool is None:
        if args is not None:
            e.append("tool 이 null 이면 arguments 도 null 이어야 합니다")
        if row.get("reply_kind") not in CHAT_REPLY_KINDS:
            e.append(f"tool 이 null 이면 reply_kind 는 {CHAT_REPLY_KINDS} 중 하나여야 합니다")
        return e

    if tool not in CHAT_TOOLS:
        return [f"tool 값 '{tool}' 이 허용 목록 {CHAT_TOOLS} 에 없습니다"]
    if "reply_kind" in row:
        e.append("도구를 부르는 레코드에는 reply_kind 를 적지 않습니다 (도구 결과로 정해짐)")
    if not isinstance(args, dict):
        return e + ["arguments 가 객체가 아닙니다"]

    if tool == "insertRequestT":
        if set(args) != {"title", "content"}:
            e.append(f"insertRequestT 인자는 title, content 두 개여야 합니다 (지금: {sorted(args)})")
            return e
        title, content = args["title"], args["content"]
        if not isinstance(title, str) or not isinstance(content, str):
            return e + ["title, content 는 문자열이어야 합니다"]
        if content == "" or title == "":
            if not (content == "" and title == ""):
                e.append("내용이 없는 접수(A3)는 title 과 content 가 모두 \"\" 여야 합니다")
        elif content != text:
            e.append("insertRequestT.content 는 사용자 발화(text)와 글자까지 같아야 합니다 (A7)")

    else:  # selectComplaintT
        if set(args) != {"content"}:
            e.append(f"selectComplaintT 인자는 content 하나여야 합니다 (지금: {sorted(args)})")
            return e
        content = args["content"]
        if not isinstance(content, str):
            return e + ["content 는 문자열이어야 합니다"]
        if content and content not in text:
            e.append(f"selectComplaintT.content '{content}' 가 발화에 글자 그대로 없습니다 (A8)")
    return e


# =============================================================================
# 과제 2·3 - 민원 분석 (analysis_records.jsonl)
# =============================================================================
def check_analysis(row: dict) -> list[str]:
    e: list[str] = []
    extra = set(row) - ANALYSIS_FIELDS - {"_line"}
    if extra:
        e.append(f"알 수 없는 필드 {sorted(extra)}")
    for key in ("title", "content"):
        if not isinstance(row.get(key), str) or not row[key].strip():
            e.append(f"{key} 가 비어 있습니다")
    out = row.get("output")
    if not isinstance(out, dict):
        return e + ["output 이 객체가 아닙니다"]
    if set(out) != set(prompts.ANALYSIS_FIELDS):
        missing = sorted(set(prompts.ANALYSIS_FIELDS) - set(out))
        unknown = sorted(set(out) - set(prompts.ANALYSIS_FIELDS))
        return e + [f"output 필드가 다릅니다 (빠짐 {missing}, 남음 {unknown}) (C1)"]

    # 모델은 잘린 원문만 보므로, 원문에 '있다'는 검사도 잘린 원문 기준입니다.
    seen = prompts.analysis_content(row.get("content") or "")
    title = out["title"]
    if not isinstance(title, str) or not title.strip() or len(title) > TITLE_MAX_CHARS:
        e.append(f"output.title 은 1~{TITLE_MAX_CHARS}자 문자열이어야 합니다 (C2)")
    if not isinstance(out["summary"], str) or not out["summary"].strip():
        e.append("output.summary 가 비어 있습니다 (C3)")
    else:
        missing = summary_numbers_missing(row.get("title") or "", row.get("content") or "", out["summary"])
        if missing:
            e.append(f"output.summary 의 숫자 {missing} 가 (잘린) 원문·입력 제목에 없습니다 (C3)")
    kp = out["key_points"]
    if not (isinstance(kp, list) and KEY_POINTS_RANGE[0] <= len(kp) <= KEY_POINTS_RANGE[1]
            and all(isinstance(x, str) and x.strip() for x in kp)):
        e.append(f"output.key_points 는 비어 있지 않은 문자열 {KEY_POINTS_RANGE[0]}~{KEY_POINTS_RANGE[1]}개여야 합니다 (C4)")
    if out["category"] not in categories.NAMES:
        e.append(f"output.category '{out['category']}' 가 7종이 아닙니다 (C5)")
    kw = out["keywords"]
    if not (isinstance(kw, list) and KEYWORDS_RANGE[0] <= len(kw) <= KEYWORDS_RANGE[1] and all(isinstance(x, str) and x for x in kw)):
        e.append(f"output.keywords 는 문자열 {KEYWORDS_RANGE[0]}~{KEYWORDS_RANGE[1]}개여야 합니다 (C6)")
    else:
        absent = [x for x in kw if x not in seen]
        if absent:
            e.append(f"output.keywords {absent} 가 (잘린) 원문에 글자 그대로 없습니다 (C6)")
    if out["urgency"] not in prompts.URGENCY_VALUES:
        e.append(f"output.urgency 는 {prompts.URGENCY_VALUES} 중 하나여야 합니다 (C7)")
    review, reason = out["needs_review"], out["review_reason"]
    if not isinstance(review, bool):
        e.append("output.needs_review 는 true/false 여야 합니다 (C8)")
    elif review and not (isinstance(reason, str) and reason.strip()):
        e.append("needs_review 가 true 면 review_reason 에 이유를 적어야 합니다 (C8)")
    elif not review and reason is not None:
        e.append("needs_review 가 false 면 review_reason 은 null 이어야 합니다 (C8)")
    return e


# =============================================================================
# 요약 속 숫자 (label_guide C3) - 검증기와 평가(evaluate.py)가 같이 씁니다
# =============================================================================
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def _norm_number(token: str) -> str:
    """'1,500' -> '1500', '03' -> '3' (자리 구분 쉼표·앞의 0 차이는 같은 숫자로 봄)"""
    token = token.replace(",", "")
    if "." in token:
        whole, frac = token.split(".", 1)
        return f"{whole.lstrip('0') or '0'}.{frac}"
    return token.lstrip("0") or "0"


def numbers_in(text: str) -> list[str]:
    """글 속 아라비아 숫자 덩어리 (정규화). '50만원' -> ['50'], '2026-03-05' -> ['2026', '3', '5']"""
    return [_norm_number(t) for t in _NUMBER.findall(text or "")]


def summary_numbers_missing(title: str, content: str, summary: str) -> list[str]:
    """
    summary 에 나온 숫자 중 (모델이 보는) 입력 제목 + 잘린 원문에 없는 것.
    숫자 덩어리 단위로 비교하므로 원문 '500' 이 요약의 '50' 을 통과시키지 않습니다.
    한글 수사(세 번, 이틀)는 대상이 아닙니다.
    """
    have = set(numbers_in(f"{title} {prompts.analysis_content(content)}"))
    return [n for n in numbers_in(summary) if n not in have]


# =============================================================================
# 정답 종류 (학습·평가 데이터를 종류별로 고르게 고를 때의 기준)
# =============================================================================
CHAT_KINDS = ("insert", "insert_empty", "select", "select_empty", "reject", "inquiry_redirect")
CHAT_KIND_NAMES = {"insert": "접수", "insert_empty": "접수(내용 없음)", "select": "조회(대상 있음)",
                   "select_empty": "조회(대상 없음)", "reject": "거절", "inquiry_redirect": "문의 안내"}


def chat_kind(r: dict) -> str:
    """챗봇 레코드의 정답 종류 6가지 (CHAT_KINDS)."""
    tool, args = r.get("tool"), r.get("arguments") or {}
    if tool == "insertRequestT":
        return "insert" if args.get("content") else "insert_empty"
    if tool == "selectComplaintT":
        return "select" if args.get("content") else "select_empty"
    return r.get("reply_kind") or "?"


def analysis_kind(r: dict) -> str:
    """분석 레코드의 정답 종류 = 카테고리 7종."""
    return (r.get("output") or {}).get("category") or "?"


def _hash_order(rows: list[dict], salt: str = "") -> list[dict]:
    return sorted(rows, key=lambda r: hashlib.sha1(f"{salt}{r['id']}".encode("utf-8")).hexdigest())


def balanced_quota(sizes: dict[str, int], n: int, max_repeat: int = 1) -> dict[str, int]:
    """
    n 자리를 종류별로 똑같이 나눕니다. 한 종류는 (건수 x max_repeat) 까지만 받을 수 있고,
    모자라서 못 채운 자리는 남은 종류들에게 다시 똑같이 나눕니다. (물 채우기 방식)
    나누어떨어지지 않는 나머지는 이름 순으로 1개씩.
    """
    cap = {k: v * max(1, max_repeat) for k, v in sizes.items() if v > 0}
    n = min(n, sum(cap.values()))
    quota = {k: 0 for k in sizes}
    open_kinds = sorted(cap)
    left = n
    while left > 0 and open_kinds:
        share, extra = divmod(left, len(open_kinds))
        if share == 0:                      # 남은 자리가 종류 수보다 적음 -> 이름 순으로 1개씩
            for k in open_kinds[:extra]:
                quota[k] += 1
            break
        for k in open_kinds:
            quota[k] += min(share, cap[k] - quota[k])
        left = n - sum(quota.values())
        open_kinds = [k for k in open_kinds if quota[k] < cap[k]]
    return quota


def pick_balanced(rows: list[dict], n: int | None, key, max_repeat: int = 1) -> list[dict]:
    """
    n 개를 정답 종류(key(r))별로 같은 비율이 되게 고릅니다. 500개든 1,000개든 전부든 비율이 같습니다.
      - n 이 없거나 0 이면 n = 전체 건수 (전체 크기는 그대로 두고 비율만 맞춤 - 많은 종류는 덜 쓰고 적은 종류는 반복)
      - 종류마다 id 해시 순서로 앞에서부터 고르므로, 같은 n 이면 항상 같은 레코드 (학습 전후·HF/Ollama 비교 공정)
        n 을 늘리면 앞서 고른 레코드는 그대로 두고 뒤에 더 붙습니다.
      - 어떤 종류의 건수가 몫보다 적으면 그 종류를 max_repeat 번까지 반복해 채웁니다. (1 이면 반복 없음 - 평가용)
        그래도 모자라면 남은 자리는 다른 종류들이 나눠 가집니다. (이때만 비율이 조금 벗어남)
    돌려주는 순서는 종류가 섞이도록 해시 순서입니다.
    """
    groups: dict[str, list[dict]] = {}
    for r in _hash_order(rows):
        groups.setdefault(key(r), []).append(r)
    n = n or len(rows)
    quota = balanced_quota({k: len(g) for k, g in groups.items()}, n, max_repeat)
    chosen: list[tuple[str, dict]] = []
    for k, g in groups.items():
        for i in range(quota[k]):
            r = g[i % len(g)]
            chosen.append((f"{i // len(g)}|{r['id']}", r))     # 반복분은 다른 순서 키 -> 같은 레코드가 붙어 나오지 않음
    chosen.sort(key=lambda x: hashlib.sha1(x[0].encode("utf-8")).hexdigest())
    return [r for _, r in chosen]


def kind_counts(rows: list[dict], key) -> dict[str, int]:
    return dict(sorted(Counter(key(r) for r in rows).items()))


# =============================================================================
# 파일 단위 검사
# =============================================================================
def _kind(path: Path) -> str:
    if path.name.startswith("chat_records"):
        return "chat"
    if path.name.startswith("analysis_records"):
        return "analysis"
    raise SystemExit(f"{path.name}: 파일 이름이 chat_records*.jsonl 또는 analysis_records*.jsonl 이어야 합니다")


def validate_file(path: Path) -> tuple[list[dict], list[str]]:
    rows, errors = read_jsonl(path)
    check = check_chat if _kind(path) == "chat" else check_analysis
    ids: Counter = Counter()
    for row in rows:
        rid = row.get("id")
        if not isinstance(rid, str) or not rid.strip():
            errors.append(f"{path.name}:{row['_line']} id 가 없습니다")
            continue
        ids[rid] += 1
        if row.get("split") is not None and row["split"] not in SPLITS:
            errors.append(f"{path.name}:{row['_line']} [{rid}] split 은 {SPLITS} 중 하나여야 합니다")
        errors += [f"{path.name}:{row['_line']} [{rid}] {msg}" for msg in check(row)]
    errors += [f"{path.name}: id '{rid}' 가 {n}번 나옵니다" for rid, n in ids.items() if n > 1]
    return rows, errors


def _load(path: Path) -> list[dict]:
    rows, errors = validate_file(path)
    if errors:
        raise ValueError(f"{path.name} 검증 실패 {len(errors)}건 - python model-training/records.py 로 확인하세요\n" + "\n".join(errors[:20]))
    for row in rows:
        row.pop("_line", None)
        row["split"] = assign_split(row)
    return rows


def load_chat_records(path: Path = DATA_DIR / "chat_records.jsonl") -> list[dict]:
    return _load(path)


def load_analysis_records(path: Path = DATA_DIR / "analysis_records.jsonl") -> list[dict]:
    return _load(path)


def _summary(path: Path, rows: list[dict]) -> str:
    split = Counter(assign_split(r) for r in rows)
    if _kind(path) == "chat":
        kinds = Counter(CHAT_KIND_NAMES.get(chat_kind(r), "?") for r in rows)
    else:
        kinds = Counter(r.get("output", {}).get("category", "?") for r in rows)
    parts = ", ".join(f"{k} {v}" for k, v in kinds.most_common())
    return f"{path.name}: {len(rows)}건 | {parts} | train {split['train']} / val {split['val']} / test {split['test']}"


def main(argv: list[str]) -> int:
    paths = [Path(p) for p in argv] or [p for p in (DATA_DIR / "chat_records.jsonl", DATA_DIR / "analysis_records.jsonl") if p.exists()]
    if not paths:
        print("검사할 파일이 없습니다. (data/chat_records.jsonl, data/analysis_records.jsonl)")
        return 1
    failed = False
    for path in paths:
        rows, errors = validate_file(path)
        print(_summary(path, rows))
        if errors:
            failed = True
            print(f"  오류 {len(errors)}건")
            for msg in errors[:50]:
                print("  - " + msg)
            if len(errors) > 50:
                print(f"  ... 외 {len(errors) - 50}건")
        else:
            print("  오류 없음")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
