import ast
import asyncio
import json
from fastapi import FastAPI, Depends
from pydantic import BaseModel
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from langchain_ollama import ChatOllama
from langchain.agents import create_agent
from contextlib import asynccontextmanager

from .logger import logger
from .ai import LLM_MODEL, embeddingForSelect
from .db import fetch_all
from langgraph.checkpoint.memory import MemorySaver # 1. 메모리 세이버 임포트


ROW_LIMIT =5

class ChatRequestBody(BaseModel):
    content: str

class ChatResponseBody(BaseModel):
    code: int
    message: str = None
    result: list = []  

# --- 툴 정의 ---
@tool
async def aiFAQ() -> dict:
    """민원 처리와 관련 없는 질문에 대해서는 LLM 모델이 직접 답변합니다.
    반환값은 반드시 {"code":0, "result"=[]}와 같은 형식이어야합니다.
    """
    return {"code": 0, "result": []}

@tool
def insertRequest(title:str,content:str)-> dict:
    """
    사용자가 민원 접수를 원하거나 접수 의사를 표현할 경우 접수를 진행할 수 있는 form을 좌측에 띄워줍니다.
    form은 민원의 제목을 나타내는 title과 민원의 내용을 나타내는 content로 이루어져 있습니다.
    만약, 민원의 content만 받은 경우 title은 content를 요약한 내용으로 추론하여 설정합니다.
    사용자가 구체적인 접수의 내용을 입력했는지에 따라 다른 방식으로 민원인을 돕습니다.
    
    만약, 사용자가 구체적인 접수의 내용을 입력했다면 좌측에 title과 content로 구성된 form을 채워주고 민원인에게 보여줍니다.
    반대로, 사용자가 구체적인 접수의 내용을 입력하지 않았다면 title과 content는 공백('')이됩니다.
    그런 경우에는 비어있는 form을 좌측에 보여줍니다.
    
    사용자에게 생성된 form을 확인하고, 마음에 들면 '접수' 버튼을 눌러 접수를 진행해달라고 안내합니다.
    마음에 들지 않을 경우 form을 직접 수정한 후 접수를 진행할 수 있음을 안내합니다.
    반환값은 반드시 {"code":1, "result"=[{"title":title, "content":content}]}와 같은 형식이어야합니다.
    """


    return {"code": 1, "result": [{"title":title, "content":content}]}
@tool
def responseComplaintT(message: str)-> dict:
    """
    이 툴은 반드시 서버의 메시지일 경우에만 호출합니다.
    서버의 메시지는 반드시 [서버]라는 문자열로 시작합니다.
    message는 민원인이 민원처리를 한 결과를 나타냅니다. 이 message 내용을 읽고 민원인에게 처리 결과를 알려줍니다.
    반환값은 반드시 {"code":3, "message": message, "result"=[]}와 같은 형식이어야합니다.
    """
    return {"code": 3, "message": message, "result": []}

@tool
async def selectComplaintT(content: str, is_described: bool, config: RunnableConfig)-> dict:
    """
        민원인이 민원의 조회, 수정, 삭제를 요청한 경우에 관련 민원을 조회합니다.
        민원인이 어떤 민원을 처리하고 싶은지 묘사했다면, 그 값은 content가 됩니다.
        is_described는 민원인이 처리하고자 하는 민원에 대한 자세한 묘사나 설명이 존재하는지를 나타내는 bool 값입니다.
        content의 내용이 민원의 내용을 구체적으로 묘사한 형태일 경우 True이고, 아닐경우 false입니다.
        예를 들어 단순히 민원인이 '민원 삭제할래'라고 말했다면 이건 어떤 민원을 삭제할지 모르므로 is_described는 false가 되어야 합니다.
        is_described이 True일 경우에는 민원 내용을 유사도 검색을 통해 DB에서 조회된 민원들을 전부 가져옵니다.
        가져온 민원 목록은 민원인이 보고있는 화면 좌측에 보여집니다.
        그 목록의 민원중에 접수 대기중이거나 진행중인 민원의 경우 수정 및 삭제가 가능하지만, 이미 처리가 완료되거나 취소된 민원은 조회만 가능합니다.
        민원인에게 좌측의 위 내용을 전달하여 민원 처리를 돕도록 합니다.
        is_described이 False일 경우에는 아래 내용을 따릅니다.
        만약 민원인이 어떤 민원을 처리하고 싶은지 묘사하지 않았다면 처리하고 싶은 민원의 내용만 묻습니다.
        내용의 유사도에 대한 검색만 지원하고 ID를 이용한 검색은 지원하지 않기 때문에 절대로 ID및 번호는 묻지 않습니다.
        is_described이 False일 경우라도 올바른 code값을 제공해야하므로 여전히 툴을 사용합니다.
        반환값은 반드시 {"code":2,"result"=rows}와 같은 형식이어야합니다.
        위에서 언급한 rows는 id, title, content, category, created_at, complaint_status값이 담긴 튜플 형식입니다.
    """
    configurable = config.get("configurable", {})
    user_id = configurable.get("user_id")
    
    rows = []
    if is_described and user_id:
        vector = await embeddingForSelect(content)
        rows = fetch_all(
            f"""
                SELECT 
                    id, 
                    title, 
                    content, 
                    category, 
                    TO_CHAR(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at, 
                    complaint_status 
                FROM complaints 
                WHERE owner_user_id = %s 
                ORDER BY embedding <=> %s::vector 
                LIMIT {ROW_LIMIT};
            """,
            (user_id, str(vector))
        )
    return {"code": 2, "result": rows}


# --- 큐 및 워커 시스템 ---
request_queue = asyncio.Queue()
worker_task = None

async def llm_worker(agent_executor):
    while True:
        try:
            user_id, query, future = await request_queue.get()
            logger.info(f"--- [처리 시작] 유저 ID: {user_id} | 질문: {query} ---")
            
            config = {"configurable": {"user_id": user_id, "thread_id": user_id}}
            
            final_code = 0
            final_message = ""
            final_result_list = []
            tool_message = None

            async for event in agent_executor.astream_events(
                {"messages": [("human", query)]}, 
                config=config,
                version="v2"
            ):
                kind = event["event"]
                
                # 1. AI 모델의 최종 대화 텍스트 응답 캡처
                if kind == "on_chat_model_end":
                    output_message = event["data"].get("output")
                    if hasattr(output_message, "content") and output_message.content:
                        # 툴 호출만을 위한 메시지가 아니라 실제 사용자 답변일 경우에만 덮어씀
                        if not getattr(output_message, "tool_calls", None):
                            final_message = output_message.content
                
                # 2. 툴 실행 결과 캡처 (한 번 값이 잡히면 계속 유지)
                elif kind == "on_tool_end":
                    tool_output = event["data"].get("output")
                    tool_name = event.get("name")
                    logger.info(f"[{user_id}] Tool 호출됨 [{tool_name}] - 원본 반환값: {tool_output}")
                    
                    data = parse_tool_output_safe(tool_output)
                    if data:
                        if "code" in data:
                            final_code = data["code"]
                        if "result" in data and data["result"]:
                            final_result_list = data["result"]
                        if "message" in data and data["message"]:
                            tool_message = data["message"]

            # 3. 우선순위에 따른 최종 message 설정
            if tool_message and not final_message:
                final_message = tool_message
            elif not final_message:
                final_message = "요청이 정상적으로 처리되었습니다."

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

def parse_tool_output_safe(tool_output):
    """ToolMessage 반환값을 안전하게 dict로 변환"""
    # 1. 이미 dict 형태인 경우
    if isinstance(tool_output, dict):
        return tool_output
    
    # 2. ToolMessage 객체인 경우 content 추출
    content = getattr(tool_output, "content", tool_output)
    if isinstance(content, dict):
        return content
    
    # 3. 문자열 형태인 경우 (JSON or Python Dict 텍스트)
    if isinstance(content, str):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            try:
                # Python dict 문자열("{'code': 2, ...}") 형태 파싱 fallback
                res = ast.literal_eval(content)
                if isinstance(res, dict):
                    return res
            except Exception:
                pass

    return None


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
    llm = ChatOllama(model=LLM_MODEL, temperature=0.1)

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