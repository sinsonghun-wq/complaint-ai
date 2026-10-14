"""
LLM 에 보내는 프롬프트 모음 - 챗봇(agent.py)과 민원 분석(ai.py)의 프롬프트는 이 파일에서만 관리합니다.

학습 코드(model-training)도 이 파일을 그대로 가져다 학습 샘플을 만듭니다.
그래서 여기 문장을 한 글자라도 바꾸면 이미 학습한 모델과 프롬프트가 달라져 학습 효과가 떨어집니다.
바꾼 뒤에는 다시 학습하세요.

정답(라벨) 기준은 model-training/data/label_guide.md 와 같습니다.
"""

from __future__ import annotations

from . import categories as category_defs

PROMPT_VERSION = "complaintai-ko-v6"   # v3: 프롬프트 분리·confidence/reason 제거·안내 문구 고정 / v4: 짧은 원문의 summary·keywords 개수 규칙 / v5: keywords 는 제목이 아닌 원문에서(띄어쓰기 그대로) / v6: selectComplaintT 의 is_described 인자 제거(content 가 비었는지로 판단)


# =============================================================================
# 챗봇 (agent.py)
# =============================================================================
AGENT_MODE_INTAKE = 0    # 접수·조회 (학습 대상)
AGENT_MODE_INQUIRY = 1   # 문의 (학습하지 않음)

# 도구 호출 뒤(또는 도구 없이) 민원인에게 보내는 고정 문구. label_guide.md 의 A10~A16 과 같습니다.
REPLY_TEXTS: dict[str, str] = {
    "insert_filled": "말씀하신 내용으로 접수 양식을 채워 두었어요. 왼쪽 양식을 확인하시고 '접수' 버튼을 눌러 주세요. 고칠 부분이 있으면 직접 수정한 뒤 접수하시면 됩니다.",
    "insert_empty": "왼쪽에 접수 양식을 열어 두었어요. 불편하신 내용을 적고 '접수' 버튼을 눌러 주세요.",
    "select_found": "말씀하신 내용과 비슷한 민원을 왼쪽에 보여 드렸어요. 접수 대기·진행 중인 민원은 수정하거나 삭제할 수 있고, 처리가 끝났거나 취소된 민원은 조회만 할 수 있어요.",
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
- 민원인이 불편한 상황이나 문제를 설명하면 민원 접수로 보고 insertRequestT 를 호출해. 행정 업무의 처리 지연·절차 불편을 알리는 말도 접수야.
- 민원 접수를 원한다는 말만 있고 내용이 없으면 insertRequestT 를 title="", content="" 로 호출해.
- 이미 넣은 민원을 조회·수정·삭제하려 하거나 그 민원의 진행 상황을 물으면 selectComplaintT 를 호출해.
- 민원과 관련 없는 잡담·광고·의미 없는 말이면 도구 없이 "{REPLY_TEXTS['reject']}" 라고만 답해.
- 민원 접수·조회가 아닌 일반 문의면 도구 없이 "{REPLY_TEXTS['inquiry_redirect']}" 라고만 답해.

[도구 결과 안내] 도구를 호출한 뒤에는 결과에 따라 아래 문장 그대로 답해.
- insertRequestT, 내용이 있음: "{REPLY_TEXTS['insert_filled']}"
- insertRequestT, 내용이 없음: "{REPLY_TEXTS['insert_empty']}"
- selectComplaintT, 찾은 민원이 있음: "{REPLY_TEXTS['select_found']}"
- selectComplaintT, 찾은 민원이 없음: "{REPLY_TEXTS['select_none']}"
- selectComplaintT, content 가 빈 문자열: "{REPLY_TEXTS['select_undescribed']}"

{_RULES}""",
    AGENT_MODE_INQUIRY: f"""너는 한국인 민원인들의 민원 처리를 돕는 한국어를 사용하는 AI 도우미야.
지금은 '문의' 모드야. 민원과 관련된 사용자의 질문을 받고 적절한 대답을 해주어야해.
유저가 문제 상황을 설명하는 경우 어떻게 행동해야 하는지에 대하여 적절한 대답을 해주어야해.

{_RULES}
- 민원과 관련되지 않은 질문에는 민원과 관련된 답변만 제공함을 안내해.""",
}

# 도구 설명 - LangChain 이 도구 설명(description)으로 Ollama 에 그대로 보냅니다.
TOOL_DESCRIPTIONS: dict[str, str] = {
    "insertRequestT": """민원 접수 양식(form)을 화면 왼쪽에 띄웁니다. 민원인이 불편한 상황을 설명하거나 민원 접수를 원할 때 호출합니다.
title: 무엇이 어떤 상태인지 나타내는 짧은 명사형 제목입니다. 위치·기간·요청 문구는 넣지 않습니다. (예: "공원 벤치 파손")
content: 민원인이 한 말을 고치지 말고 그대로 넣습니다.
민원 접수를 원한다는 말만 있고 내용이 없으면 title 과 content 를 모두 빈 문자열("")로 둡니다.""",
    "selectComplaintT": """민원인이 이미 접수한 민원을 내용의 유사도로 찾아 화면 왼쪽에 목록으로 보여 줍니다. 민원의 조회·수정·삭제를 요청하거나 진행 상황을 물을 때 호출합니다. 민원 ID·번호로는 찾을 수 없습니다.
content: 찾을 민원의 대상을 나타내는 명사만 민원인이 한 말 그대로 넣습니다. 번호·동·호·층, 장소를 꾸미는 말, 상태·요청을 나타내는 말(고장, 파손, 취소 등)은 뺍니다. (예: "101동 엘리베이터 고장 민원 취소해 줘" -> "엘리베이터") 대상을 알 수 없으면 빈 문자열("")입니다.""",
    "aiFAQ": """민원의 처리를 요청하는 경우가 아닌, 민원과 관련된 문의를 하는 경우라면 LLM 모델이 직접 답변합니다.
민원과 관련되지 않은 질문 및 요청이 들어올 경우 민원인에게 민원과 관련된 답변만 제공함을 안내해드립니다.

사용자 질문의 예시 -> 대응방법은 다음과 같습니다.

질문: 여기서는 무슨 일을 하니? -> 이곳은 민원 사이트이므로 민원 사이트에서 어떤 일을 하는지를 묻는 질문입니다.
따라서 이런 질문에는 대답을 해도 좋습니다.

질문: 맛집을 추천해줘. -> 민원과 전혀 관련이 없으므로 민원과 관련된 답변만 제공함을 안내해드립니다.

질문: PEM옹벽은 탄산화 시험(정밀)염화물 시료채취 후 시험(성능)실시가 기본과업인지 궁금해서 문의드립니다.
-> 민원의 문의와 관련된 질문이므로 알고 있는 내용이라면 대답하면되고, 모르는 내용이라면 민원 접수 요청을 다시 해줄 것을 권장해드리도록 합니다.""",
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
    원문이 길면 analysis_content() 규칙으로 잘라서 넣습니다."""
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
        "- urgency: high(인명·안전 위험 - 화재, 붕괴, 사고 우려 등) / medium(생활 불편) / low(단순 문의·제안·칭찬)\n"
        "- needs_review: 내용이 너무 짧거나, 대상·위치를 알 수 없거나, 여러 민원이 섞여 있으면 true 와 review_reason(이유 한 문장). 아니면 false 와 null\n"
        "- keywords: 민원 제목이 아니라 민원 원문에 글자 그대로(띄어쓰기까지 같게) 있는 명사 3~5개(원문이 짧으면 있는 만큼)\n\n"
        f"JSON만 반환한다. {_ANALYSIS_SCHEMA}\n\n"
        f"민원 제목:\n{title}\n\n민원 원문:\n{analysis_content(content)}"
    )
