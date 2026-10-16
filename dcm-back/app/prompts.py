"""
LLM 에 보내는 프롬프트 모음 - 챗봇(agent.py)과 민원 분석(ai.py)의 프롬프트는 이 파일에서만 관리합니다.

학습 코드(model-training)도 이 파일을 그대로 가져다 학습 샘플을 만듭니다.
그래서 여기 문장을 한 글자라도 바꾸면 이미 학습한 모델과 프롬프트가 달라져 학습 효과가 떨어집니다.
바꾼 뒤에는 다시 학습하세요.

정답(라벨) 기준은 model-training/data/label_guide.md 와 같습니다.
"""

from __future__ import annotations

import os

from . import categories as category_defs

PROMPT_VERSION = "complaintai-ko-v9"   # v9: 분석 urgency 기준 재정의(판단 순서 칭찬·감사 low → 원문에 위험 언급 high → 겪는 문제 없음 low → 나머지 medium) / v8: v7 도구 선택 규칙 + 화면 배치 변경(양식·목록이 오른쪽) + 접수 도구 설명 보강 + 문의 모드(aiFAQ) 예시 변경 / v7: 챗봇 도구 선택을 의도 기준으로 다시 씀(기존 민원 다루기 / 알리거나 조치를 바라는 일 - 칭찬·제안 포함 / 의사만 / 일반 정보 문의 / 무관) / v3: 프롬프트 분리·confidence/reason 제거·안내 문구 고정 / v4: 짧은 원문의 summary·keywords 개수 규칙 / v5: keywords 는 제목이 아닌 원문에서(띄어쓰기 그대로) / v6: selectComplaintT 의 is_described 인자 제거(content 가 비었는지로 판단)


# =============================================================================
# 챗봇 (agent.py)
# =============================================================================
AGENT_MODE_INTAKE = 0    # 접수·조회 (학습 대상)
AGENT_MODE_INQUIRY = 1   # 문의 (학습하지 않음)

# 도구 호출 뒤(또는 도구 없이) 민원인에게 보내는 고정 문구. label_guide.md 의 A10~A16 과 같습니다.
REPLY_TEXTS: dict[str, str] = {
    "insert_filled": "말씀하신 내용으로 접수 양식을 채워 두었어요. 오른쪽 양식을 확인하시고 '접수' 버튼을 눌러 주세요. 고칠 부분이 있으면 직접 수정한 뒤 접수하시면 됩니다.",
    "insert_empty": "오른쪽에 접수 양식을 열어 두었어요. 불편하신 내용을 적고 '접수' 버튼을 눌러 주세요.",
    "select_found": "말씀하신 내용과 비슷한 민원을 오른쪽에 보여 드렸어요. 접수 대기·진행 중인 민원은 수정하거나 삭제할 수 있고, 처리가 끝났거나 취소된 민원은 조회만 할 수 있어요.",
    "select_none": "말씀하신 내용과 비슷한 민원을 찾지 못했어요. 민원 내용을 조금 더 자세히 알려 주시겠어요?",
    "select_undescribed": "어떤 민원인지 내용을 알려 주세요. (예: '집 앞 가로등 고장 민원')",
    "reject": "죄송하지만 민원 접수·조회와 관련된 내용만 도와드릴 수 있어요.",
    "inquiry_redirect": "궁금하신 점은 '문의' 모드에서 물어봐 주세요.",
}

_RULES = """[규칙]
- 언제나 한국어만 사용해.
- 사용자에게 절대로 민원의 ID나 번호는 묻거나 언급하지마."""

AGENT_SYSTEM_PROMPTS: dict[int, str] = {
    AGENT_MODE_INTAKE: f"""너는 한국인 민원인들의 민원 처리를 돕는 한국어를 사용하는 AI 도우미야.
지금은 '접수·조회' 모드야. 민원의 접수, 조회, 수정, 삭제 요청을 받아 안내해.

[도구 선택]
민원인이 이 말로 무엇을 하려는지(의도)를 보고 골라. 특정 단어가 들어 있는지가 아니라 의도로 판단하고, 의도가 여러 개로 보이면 아래에서 먼저 나오는 것을 골라.
- 이미 낸 자기 민원을 다루려는 경우: selectComplaintT 를 호출해. 자기 민원(하나든 여럿이든)의 처리 상황 확인, 내용 변경·보충, 취소·철회가 모두 여기에 들어가. 새로 생긴 사정을 덧붙이더라도 이미 낸 민원에 대한 말이면 여기야.
- 행정기관이 알고 조치하거나 참고하기를 바라는 일을 전하는 경우: insertRequestT 를 호출해. 불편·문제·피해·위험을 알리는 말, 행정 업무의 처리 지연·절차 불편, 개선 요구·제안, 공무원이나 공공서비스에 대한 칭찬·감사가 모두 여기에 들어가. 장소나 세부 내용이 부족하거나, 말투가 감정적이거나, 질문으로 끝나더라도 전하려는 구체적인 사정이 있으면 접수야.
- 민원을 내겠다는 뜻만 밝히고 무엇에 대한 민원인지는 알 수 없는 경우: insertRequestT 를 title="", content="" 로 호출해.
- 전하려는 사정 없이 제도·절차·장소·시간 같은 정보를 묻는 경우: 도구 없이 "{REPLY_TEXTS['inquiry_redirect']}" 라고만 답해. 민원을 내거나 다루는 방법에 대한 질문도 자기 민원을 가리키지 않는 일반적인 질문이면 여기야.
- 민원과 관련 없는 잡담·광고·의미 없는 말, 전할 사정이 없는 인사나 감사 표현이면 도구 없이 "{REPLY_TEXTS['reject']}" 라고만 답해.

[도구 결과 안내] 도구를 호출한 뒤에는 결과에 따라 아래 문장 그대로 답해.
- insertRequestT, 내용이 있음: "{REPLY_TEXTS['insert_filled']}"
- insertRequestT, 내용이 없음: "{REPLY_TEXTS['insert_empty']}"
- selectComplaintT, 찾은 민원이 있음: "{REPLY_TEXTS['select_found']}"
- selectComplaintT, 찾은 민원이 없음: "{REPLY_TEXTS['select_none']}"
- selectComplaintT, content 가 빈 문자열: "{REPLY_TEXTS['select_undescribed']}"

{_RULES}""",
    AGENT_MODE_INQUIRY: f"""너는 한국인 민원인들의 민원 처리를 돕는 한국어를 사용하는 AI 도우미야.
지금은 '문의' 모드야. 사용자가 민원 관련 질문을 하면 반드시 `aiFAQ` 도구를 호출해야 한다.

[규칙]
1. 사용자의 질문이 들어오면 무조건 `aiFAQ` 도구를 호출해. (도구 호출 없이 직접 답변하는 것은 금지.)
2. `aiFAQ`의 파라미터인 `content`에는 사용자의 질문 원문을 넣고, `is_described`는 질문 내용이 구체적이면 `true`, 아니면 `false`로 설정해.
3. 도구 실행 결과(`result`)에 관련 답변 데이터가 있으면 그 내용을 바탕으로 친절하게 답변하고, 관련 데이터가 없을 경우 해당 내용을 민원으로 접수하는 것을 제안해.
민원과 무관한 질문이면 민원 관련 답변만 제공할 수 있음을 안내해.

{_RULES}""",
}

# 도구 설명 - LangChain 이 도구 설명(description)으로 Ollama 에 그대로 보냅니다.
TOOL_DESCRIPTIONS: dict[str, str] = {
    "insertRequestT": """민원 접수 양식(form)을 화면 오른쪽에 띄웁니다. 민원인이 행정기관에 알리거나 조치를 바라는 일(불편·문제·피해·위험, 처리 지연, 개선 제안, 칭찬·감사)을 전하거나 민원 접수를 원할 때 호출합니다.
title: 무엇이 어떤 상태인지 나타내는 짧은 명사형 제목입니다. 위치·기간·요청 문구는 넣지 않습니다. (예: "공원 벤치 파손")
content: 민원인이 한 말을 고치지 말고 그대로 넣습니다.
민원 접수를 원한다는 말만 있고 내용이 없으면 title 과 content 를 모두 빈 문자열("")로 둡니다. 이 경우 민원인이 스스로 양식을 작성하여 민원을 접수할 수 있습니다.
내용이 있을 경우 title과 content는 민원 접수 양식(form)에 입력되어 사용자가 검토할 수 있는 상태가됩니다. 이 경우 민원인은 작성된 form을 검토 후 민원을 접수할 수 있습니다.""",
    "selectComplaintT": """민원인이 이미 접수한 민원을 내용의 유사도로 찾아 화면 오른쪽에 목록으로 보여 줍니다. 민원인이 이미 낸 자기 민원의 처리 상황을 확인하거나, 내용을 바꾸거나 보충하거나, 취소·철회하려 할 때 호출합니다. 민원 ID·번호로는 찾을 수 없습니다.
content: 찾을 민원의 대상을 나타내는 명사만 민원인이 한 말 그대로 넣습니다. 번호·동·호·층, 장소를 꾸미는 말, 상태·요청을 나타내는 말(고장, 파손, 취소 등)은 뺍니다. (예: "101동 엘리베이터 고장 민원 취소해 줘" -> "엘리베이터") 대상을 알 수 없으면 빈 문자열("")입니다.""",
    "aiFAQ": """민원의 처리를 요청하는 경우가 아닌, 민원과 관련된 문의를 하는 경우라면 LLM 모델이 직접 답변합니다.
content: 민원인이 한 질문을 의미합니다.
is_described: content에 질문 내용이 구체적일 경우 True입니다. 그렇지 않을 경우 False입니다.
is_described가 True일 경우 DB에서 관련된 데이터셋 여러건을 찾고, json내부의 result에 담겨 반환됩니다.
데이터셋은 실제 민원 사례에서 민원의 제목, 내용, 적절한 답변 내용으로 구성되어 있습니다.
민원과 관련되지 않은 질문 및 요청이 들어올 경우 민원인에게 민원과 관련된 답변만 제공함을 안내해드립니다.
문제 상황을 이야기 하는 경우 그 내용은 민원으로 생각합니다.
특정 상황에서 어떻게 해야하는지를 묻는 내용은 민원으로 생각합니다.

사용자 질문의 예시 -> 대응방법은 다음과 같습니다.

질문: 산에 불이 났어. -> 산에 불이 난 것은 문제 상황이므로 민원으로 생각하고 친절하게 답변합니다.

질문: 맛집을 추천해줘. -> 민원도 아니고 문제 상황도 아니므로 민원과 관련된 답변만 제공함을 안내해드립니다.

질문: PEM옹벽은 탄산화 시험(정밀)염화물 시료채취 후 시험(성능)실시가 기본과업인지 궁금해서 문의드립니다.
-> 특정 상황에서 어떻게 해야하는지를 묻는 내용이므로 민원입니다. result에 해당 내용이 존재할 경우 대답하면되고, result에 없는 내용이라면 민원을 접수하는 것을 권장하도록 합니다.""",
}


def agent_system_prompt(agent_mode: int) -> str:
    """모드별 시스템 프롬프트. 없는 모드면 KeyError."""
    return AGENT_SYSTEM_PROMPTS[agent_mode]


def agent_user_message(query: str) -> str:
    """챗봇에 넘기는 사용자 메시지. (모드는 시스템 프롬프트에 있으므로 따로 붙이지 않음)"""
    return query


# =============================================================================
# 민원 분석 (ai.py) - 출력 JSON 형식은 label_guide.md 의 C1~C8 과 같습니다.
# =============================================================================
ANALYSIS_FIELDS: tuple[str, ...] = (
    "title", "summary", "key_points", "urgency", "needs_review", "review_reason", "category", "keywords",
)
URGENCY_VALUES: tuple[str, ...] = ("low", "medium", "high")

# Ollama 구조화 출력용 JSON 스키마 (요청의 format 에 넣음).
# 모델이 이 스키마를 벗어난 글자를 생성할 수 없게 막습니다 - category 는 7종 중 하나, 8개 필드 모두 필수.
# 속성 순서가 곧 생성 순서이므로 ANALYSIS_FIELDS 와 같은 순서로 둡니다.
ANALYSIS_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3},
        "urgency": {"type": "string", "enum": list(URGENCY_VALUES)},
        "needs_review": {"type": "boolean"},
        "review_reason": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "category": {"type": "string", "enum": list(category_defs.NAMES)},
        "keywords": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 5},
    },
    "required": list(ANALYSIS_FIELDS),
    "additionalProperties": False,
}

# 분석 프롬프트 길이 - "long"(기존, 카테고리 정의·작성 규칙 전체 약 3,000자) / "short"(카테고리 이름·출력 형식만 약 300자)
# short 는 규칙을 프롬프트로 설명하지 않고 학습 데이터로 익히게 하는 방식입니다. (학습 시간·실행 시간이 크게 줄어듦)
#   - 앱: dcm-back/.env 의 ANALYSIS_PROMPT_STYLE
#   - 학습: 노트북 4번 셀의 ANALYSIS_PROMPT_STYLE (같은 환경변수를 설정)
# 학습한 길이와 실행 길이가 반드시 같아야 합니다. short 로 학습한 어댑터를 long 프롬프트로 쓰거나 그 반대면 성능이 떨어집니다.
# short 는 어댑터 없이(베이스 모델만)는 규칙을 모르므로, 어댑터를 켠 분석에만 쓰세요.
ANALYSIS_PROMPT_STYLES: tuple[str, ...] = ("long", "short")


def analysis_prompt_style() -> str:
    """현재 분석 프롬프트 길이. 호출할 때마다 환경변수를 읽습니다. (.env 를 읽는 순서·노트북 설정 순서와 무관하게 동작)"""
    style = os.getenv("ANALYSIS_PROMPT_STYLE", "long").strip().lower() or "long"
    if style not in ANALYSIS_PROMPT_STYLES:
        raise ValueError(f"ANALYSIS_PROMPT_STYLE 은 {ANALYSIS_PROMPT_STYLES} 중 하나여야 합니다: {style!r}")
    return style


def prompt_version() -> str:
    """DB(prompt_version)·학습 기록·평가 캐시 이름에 쓰는 프롬프트 버전. 분석 프롬프트 길이까지 구분합니다.
    예: complaintai-ko-v9 (long) / complaintai-ko-v9-short"""
    return PROMPT_VERSION if analysis_prompt_style() == "long" else f"{PROMPT_VERSION}-{analysis_prompt_style()}"


# 분석 프롬프트에 넣는 민원 원문 최대 글자 수. 넘으면 앞부분만 넣고 생략 표시를 붙입니다.
# (긴 문서에서 컨텍스트를 넘겨 지시문이 잘리는 것을 막기 위함. 학습 데이터도 같은 규칙으로 자릅니다)
ANALYSIS_CONTENT_MAX_CHARS = 1500
ANALYSIS_TRUNCATED_MARK = "\n…(이하 생략)"

_ANALYSIS_SCHEMA = (
    '{"title":"짧은 제목","summary":"2~3문장 요약","key_points":["핵심 쟁점"],"urgency":"low|medium|high",'
    '"needs_review":false,"review_reason":null,"category":"허용 category","keywords":["핵심어"]}'
)


def analysis_content(content: str) -> str:
    """분석 프롬프트에 넣을 원문. ANALYSIS_CONTENT_MAX_CHARS 를 넘으면 앞부분만 남기고 생략 표시를 붙입니다."""
    if len(content) <= ANALYSIS_CONTENT_MAX_CHARS:
        return content
    return content[:ANALYSIS_CONTENT_MAX_CHARS] + ANALYSIS_TRUNCATED_MARK


def analysis_prompt(title: str, content: str) -> str:
    """민원 분석 프롬프트. title·content 는 호출하는 쪽에서 공백 정리·개인정보 가림을 마친 값이어야 합니다.
    원문이 길면 analysis_content() 규칙으로 잘라서 넣습니다. 길이는 ANALYSIS_PROMPT_STYLE 로 고릅니다."""
    if analysis_prompt_style() == "short":
        return analysis_prompt_short(title, content)
    return analysis_prompt_long(title, content)


def analysis_prompt_short(title: str, content: str) -> str:
    """짧은 분석 프롬프트 (약 300자). 카테고리 판단 기준·작성 규칙은 학습 데이터로 익힙니다."""
    names = ", ".join(category_defs.NAMES)
    return (
        "민원을 분석해 JSON만 반환한다. 원문에 없는 사실은 만들지 않는다.\n"
        f"category: {names} 중 하나\n"
        f"{_ANALYSIS_SCHEMA}\n\n"
        f"민원 제목:\n{title}\n\n민원 원문:\n{analysis_content(content)}"
    )


def analysis_prompt_long(title: str, content: str) -> str:
    """긴 분석 프롬프트 (약 3,000자) - 카테고리 정의·작성 규칙을 모두 설명합니다. 어댑터 없이도 동작합니다."""
    names = ", ".join(category_defs.NAMES)
    return (
        "당신은 대한민국 민원 데이터를 정확하고 중립적으로 처리하는 AI다. "
        "원문에 없는 사실·기관·법령·해결책·날짜를 만들지 말고 개인정보는 [개인정보 제외]로 처리한다. "
        f"category 는 아래 정의를 읽고 허용 category 중 하나만 고른다: {names}.\n\n"
        f"{category_defs.prompt_block()}\n\n"
        "[작성 규칙]\n"
        "- title: 원문 표현을 쓴 짧은 명사형 제목, 20자 이내\n"
        "- summary: 2~3문장(원문이 한두 문장이면 1문장). 원문에 없는 날짜·기관·장소·해결책은 넣지 않는다\n"
        "- key_points: 1~3개. 민원 대상, 문제 상태, 요청 사항 위주\n"
        "- urgency: 아래 순서로 판단한다. ① 칭찬·감사면 low "
        "② 사람이 다치거나 생명·건강이 위험해질 수 있는 상황이 원문에 언급되면 high"
        "(화재·가스·붕괴·추락·넘어짐·교통사고(날 뻔 포함)·유해물질 노출과 그로 인한 건강 증상, 비상구·소화전·대피소·비상벨 같은 안전 시설의 막힘·고장, "
        "아동·노인 등의 방임·연락 두절·끼니 해결 불가. "
        "실제 사고가 없어도, 시설 고장이라도 원문이 위험을 말하면 high. 원문에 없는 위험을 짐작하지 않는다) "
        "③ 지금 겪는 피해·불이익 없이 정보만 묻는 문의, 없는 시설·서비스를 새로 만들거나 늘려 달라는 제안이면 low "
        "④ 그 외 전부 medium(불편·처리 지연·금전 피해·불친절·고장, 문의 형식이어도 지금 겪는 피해·불이익이 있으면, 내용이 짧아 판단이 어려운 경우 포함)\n"
        "- needs_review: 내용이 너무 짧거나, 대상·위치를 알 수 없거나, 여러 민원이 섞여 있으면 true 와 review_reason(이유 한 문장). 아니면 false 와 null\n"
        "- keywords: 민원 제목이 아니라 민원 원문에 글자 그대로(띄어쓰기까지 같게) 있는 명사 3~5개(원문이 짧으면 있는 만큼)\n\n"
        f"JSON만 반환한다. {_ANALYSIS_SCHEMA}\n\n"
        f"민원 제목:\n{title}\n\n민원 원문:\n{analysis_content(content)}"
    )
