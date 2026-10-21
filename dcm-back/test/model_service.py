import httpx

OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"
MODEL_NAME = "qwen2.5:7b-instruct"

def call_ollama(question: str, docu: str) -> str:
    system_instructions = """
        역할: 사용자의 질문에 답변해주는 AI 어시스턴트
        응답 언어: 입력 문서의 언어와 관계없이 반드시 한국어로 응답합니다.
        작업: 아래에 제공된 [참고 문서]를 기반으로 사용자의 질문에 답변합니다.
        제약사항:
        - 원문에 없는 내용을 임의로 추가하거나 추측하지 않습니다.
        - 참고 문서에 답변 내용이 없는 경우 "문서에서 관련 내용을 찾을 수 없습니다."라고 답변합니다.
        출력 요구사항:
        - 자연스럽고 전문적인 한국어로 작성합니다.
        - 출력 내용은 문장별로 개행을 추가합니다.
    """
    
    # [핵심] user 메시지 안에 참고할 문서(docu)와 질문(question)을 조합합니다.
    user_content = f"""[참고 문서]
{docu}

[질문]
{question}"""

    payload = {
        "model": MODEL_NAME, 
        "messages": [
            { "role": "system", "content": system_instructions },
            { "role": "user", "content": user_content } 
        ],
        "stream": False,
        "think": False,
    }

    try:
        response = httpx.post(OLLAMA_CHAT_URL, json=payload, timeout=2400.0)
        response.raise_for_status()
        data = response.json()
        print(f"prompt tokens: {data.get('prompt_eval_count')}, "
              f"gen tokens: {data.get('eval_count')}")
        return data["message"]["content"]

    except httpx.HTTPError as e:
        raise RuntimeError(f"Ollama call failed: {e}")