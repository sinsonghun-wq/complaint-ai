import asyncio
import logging
import re
from fastapi import FastAPI, Depends
from pydantic import BaseModel
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from langchain_ollama import ChatOllama
from langchain.agents import create_agent
from contextlib import asynccontextmanager

from .ai import LLM_MODEL, embeddingForSelect
from .db import fetch_all
from langgraph.checkpoint.memory import MemorySaver # 1. 메모리 세이버 임포트


# 로거 설정
logger = logging.getLogger("test")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

class ChatRequestBody(BaseModel):
    content: str

class ChatResponseBody(BaseModel):
    code: int
    message: str = None
    result: list = []  

# --- 툴 정의 ---
@tool
async def aiFAQ() -> ChatResponseBody:
    """민원인의 일반적인 질의에는 LLM 모델이 직접 답변합니다."""
    return ChatResponseBody(code=0)

@tool
def insertRequest():
    """
    사용자가 민원 접수를 원하거나 접수 의사를 표현할 경우 접수를 진행할 수 있는 form을 좌측에 띄워줍니다.
    사용자에게 좌측의 form을 작성하여 접수를 진행해달라는 안내문구를 제공합니다.
    """
    return ChatResponseBody(code=1)

@tool
def responseComplaintT(message: str):
    """
    이 툴은 반드시 서버의 메시지일 경우에만 호출합니다.
    서버의 메시지는 반드시 [서버]라는 문자열로 시작합니다.
    message는 민원인이 민원처리를 한 결과를 나타냅니다. 이 message 내용을 읽고 민원인에게 처리 결과를 알려줍니다.
    """
    return ChatResponseBody(code=3, message=message)

@tool
async def selectComplaintT(content: str, is_described: bool, config: RunnableConfig):
    """
        민원인이 민원의 조회, 수정, 삭제를 요청한 경우에 관련 민원을 조회합니다.
        민원인이 어떤 민원을 처리하고 싶은지 묘사했다면, 그 값은 content가 됩니다.
        is_described는 민원인이 처리하고자 하는 민원에 대한 자세한 묘사나 설명이 존재하는지를 나타내는 bool 값입니다.
        content의 내용이 민원의 내용을 구체적으로 묘사한 형태일 경우 True이고, 아닐경우 false입니다.
        예를 들어 단순히 민원인이 '민원 삭제할래'라고 말했다면 이건 어떤 민원을 삭제할지 모르므로 is_described는 false가 되어야 합니다.
        is_described이 True일 경우에는 민원 내용을 유사도 검색을 통해 DB에서 관련 민원들을 최대 3개까지 가져옵니다.
        가져온 민원 목록은 민원인이 보고있는 화면 좌측에 보여지는데, 그 목록은 수정 및 삭제가 가능한 목록입니다.
        따라서, 민원인에게 좌측의 민원 목록을 통해 수정 및 삭제가 가능함을 안내합니다.
        is_described이 False일 경우에는 아래 내용을 따릅니다.
        만약 민원인이 어떤 민원을 처리하고 싶은지 묘사하지 않았다면 처리하고 싶은 민원의 내용만 묻습니다.
        내용의 유사도에 대한 검색만 지원하고 ID를 이용한 검색은 지원하지 않기 때문에 절대로 ID및 번호는 묻지 않습니다.
        is_described이 False일 경우라도 올바른 code값을 제공해야하므로 여전히 툴을 사용합니다.
        
    """
    configurable = config.get("configurable", {})
    user_id = configurable.get("user_id")
    
    rows = []
    if is_described and user_id:
        vector = await embeddingForSelect(content)
        rows = fetch_all(
            "SELECT id, title, content, category, created_at, complaint_status FROM complaints WHERE owner_user_id=%s ORDER BY 1-(embedding <=> %s::vector) LIMIT 3;",
            (user_id, str(vector))
        )
    return ChatResponseBody(code=2, result=rows)


# --- 큐 및 워커 시스템 ---
request_queue = asyncio.Queue()
worker_task = None

async def llm_worker(agent_executor):
    """큐에서 민원인의 요청을 하나씩 꺼내 순서대로 처리하는 백그라운드 워커"""
    while True:
        try:
            user_id, query, future = await request_queue.get()
            logger.info(f"--- [처리 시작] 유저 ID: {user_id} | 질문: {query} ---")
            
            config = {"configurable": {"user_id": user_id,"thread_id": user_id}}
            
            # 기본값 설정
            final_code = 0
            final_message = ""
            final_result_list = []
            
            # astream_events를 통해 에이전트 실행 과정 모니터링
            async for event in agent_executor.astream_events(
                {"messages": [("human", query)]}, 
                config=config,
                version="v2"
            ):
                kind = event["event"]
                
                # 1. AI 모델의 최종 텍스트 응답 캡처 -> message로 사용
                if kind == "on_chat_model_end":
                    output_message = event["data"].get("output")
                    if hasattr(output_message, "content") and output_message.content:
                        # 툴 호출만을 위한 빈 메시지가 아니라 실제 대화 텍스트인 경우
                        if not getattr(output_message, "tool_calls", None):
                            final_message = output_message.content
                
                # 2. 툴 실행 결과 캡처 -> code 및 result 추출 (다양한 데이터 구조 대응)
                elif kind == "on_tool_end":
                    tool_output = event["data"].get("output")
                    tool_name = event.get("name")
                    logger.info(f"[{user_id}] Tool 호출됨 [{tool_name}] - 원본 반환값: {tool_output}")
                    
                    # tool_output 형태에 따른 안전한 데이터 추출
                    code, res = parse_tool_output(tool_output)
                    if code is not None:
                        final_code = code
                    if res is not None:
                        final_result_list = res

            # 만약 대화 중 on_chat_model_end로 message가 잡히지 않았다면 툴의 결과나 기본 메시지 보완
            if not final_message:
                final_message = "요청이 정상적으로 처리되었습니다."

            # 최종 ChatResponseBody 조립
            final_response = ChatResponseBody(
                code=final_code,
                message=final_message,
                result=final_result_list
            )
            
            logger.info(f"--- [처리 완료] 유저 ID: {user_id} | 응답: {final_response} ---\n")
            
            if future and not future.done():
                future.set_result(final_response)
                
        except Exception as e:
            logger.error(f"[{user_id}] 처리 중 오류 발생: {e}")
            if 'future' in locals() and future and not future.done():
                future.set_exception(e)
        finally:
            request_queue.task_done()


def parse_tool_output(tool_output):
    """툴 출력이 객체, 딕셔너리, ToolMessage, 문자열 등 어떤 형태든 안전하게 code와 result를 추출하는 함수"""
    code = 0
    result = []
    
    if not tool_output:
        return code, result

    # 1. ChatResponseBody 객체인 경우
    if isinstance(tool_output, ChatResponseBody):
        return tool_output.code, tool_output.result if tool_output.result is not None else []
    
    # 2. 딕셔너리인 경우
    if isinstance(tool_output, dict):
        return tool_output.get("code", 0), tool_output.get("result", [])
        
    # 3. code 속성을 직접 가진 객체인 경우
    if hasattr(tool_output, "code"):
        return getattr(tool_output, "code", 0), getattr(tool_output, "result", [])
        
    # 4. ToolMessage 등 .content 속성을 가진 객체이거나 문자열인 경우
    content = None
    if hasattr(tool_output, "content"):
        content = tool_output.content
    elif isinstance(tool_output, str):
        content = tool_output
        
    if content is not None:
        if isinstance(content, dict):
            return content.get("code", 0), content.get("result", [])
        elif isinstance(content, str):
            # 정규식을 사용하여 content 문자열 안에서 code=숫자 패턴 추출 (예: 'code=1 ...')
            code_match = re.search(r'code\s*=\s*(\d+)', content)
            if code_match:
                code = int(code_match.group(1))
                
            # result 리스트 추출 시도 (필요시 정규식 보강 가능)
            if "result=[]" in content or "result=None" in content:
                result = []
                
    return code, result


async def enqueue_chat_request(user_id: str, query: str) -> ChatResponseBody:
    """API에서 요청을 받아 큐에 넣고 결과(Future)를 기다림"""
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    await request_queue.put((user_id, query, future))
    return await future


# --- FastAPI 생명주기(Lifespan) 설정 ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. 서버 시작 시 실행 (에이전트 초기화 및 워커 백그라운드 태스크 실행)
    global worker_task
    tools = [aiFAQ, insertRequest, selectComplaintT, responseComplaintT]
    #기존에 쓰던 qwen2.5:7b-instruct의 경우 추론이 실패한 경우가 많아 다양한 모델로 테스트중
    llm = ChatOllama(model="qwen3:8b", temperature=0.1)

    memory = MemorySaver()

    agent_executor = create_agent(llm, tools,checkpointer=memory ,system_prompt="""
        너는 한국인 민원인들의 민원 처리를 돕는 한국어를 사용하는 AI 도우미야.
        사용자는 너를 화면의 우측에 있는 챗봇으로 인식해.
        민원인들과 대화할 때는 언제나 항상 한국어만 사용해야 해.

        민원인들이 민원의 접수, 조회, 수정, 삭제를 원할 경우 네가 직접 민원 처리를 진행할수는 없고
        네 역할은 민원 처리를 위한 form을 띄워주거나, 조회된 민원 목록이 좌측에 나타났음을 알려주는것이야.
        사용자에게 절대로 민원의 id 및 번호에 대한 언급은 하면 안돼.
        
        [툴 사용 규칙]
        1. insertRequest: 민원인이 "민원 접수", "접수하고 싶어", "접수를 원해요" 등 새로운 민원 접수를 명시적으로 원할 경우 **반드시** 이 툴을 호출해야 합니다.
        2. selectComplaintT: 민원인이 민원의 조회, 수정, 삭제를 요청할 경우에는 반드시 이 툴을 사용합니다.
        만약 민원 조회에 성공했을 경우 민원 목록에는 수정 및 삭제 버튼이 포함되어 있으므로 사용자가 수정 및 삭제를 원하면 좌측의 민원 목록에서 수정 및 삭제 처리가 가능하다는 안내를 제공해줘야합니다.
        3. responseComplaintT: 서버로부터 민원 처리 결과 메시지가 전달된 경우에만 호출합니다. 주로 민원의 접수, 수정, 삭제가 완료된 경우 호출합니다.
        4. aiFAQ: 위 세 가지 경우에 해당하지 않는 일반적인 질문이나 대화일 때만 호출합니다.
    """)
    
    worker_task = asyncio.create_task(llm_worker(agent_executor))
    logger.info("--- LLM 백그라운드 워커가 시작되었습니다. ---")
    
    yield
    
    # 2. 서버 종료 시 실행 (워커 태스크 정리)
    if worker_task:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            logger.info("--- LLM 백그라운드 워커가 종료되었습니다. ---")