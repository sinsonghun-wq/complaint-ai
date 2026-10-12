import ast
import asyncio
import json
from fastapi import FastAPI
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from langchain_ollama import ChatOllama
from langchain.agents import create_agent
from contextlib import asynccontextmanager

from app.models import ChatRequestBody, ChatResponseBody

from .logger import logger
from .ai import LLM_MODEL, embeddingForSelect
from .db import fetch_all
from langgraph.checkpoint.memory import MemorySaver

ROW_LIMIT = 5

# --- 툴 정의 ---
@tool
async def aiFAQ() -> dict:
    """일반적인 문의나 민원과 관련 없는 질문에 답변할 때 호출합니다.
    반환값은 반드시 {"code": 0, "result": []} 형식이어야 합니다.
    """
    return {"code": 0, "result": []}

@tool
def insertRequestT(title: str, content: str) -> dict:
    """사용자가 민원 접수를 원하거나 접수 의사를 표현할 경우 접수를 진행할 수 있는 form을 좌측에 띄워줍니다.
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
    return {"code": 1, "result": [{"title": title, "content": content}]}

@tool
def responseComplaintT(message: str) -> dict:
    """민원 처리 결과 메시지가 전달된 경우 이를 안내할 때 호출합니다.
    반환값은 반드시 {"code": 3, "message": message, "result": []} 형식이어야 합니다.
    """
    return {"code": 3, "message": message, "result": []}

@tool
async def selectComplaintT(content: str, is_described: bool, config: RunnableConfig) -> dict:
    """민원인이 민원의 조회, 수정, 삭제를 요청한 경우에 관련 민원을 조회합니다.
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

async def llm_worker(llm, tools_dict):
    while True:
        try:
            user_id, chatbody, future = await request_queue.get()
            query = chatbody.content
            agent_mode = chatbody.agent_mode

            logger.info(f"--- [처리 시작] 유저 ID: {user_id} | agent_mode:{agent_mode} | 질문: {query} ---")
            
            # agent_mode에 따라 사용할 수 있는 툴을 동적으로 필터링하여 바인딩
            # agent_mode 0: insertRequest, selectComplaintT
            # agent_mode 1: responseComplaintT
            # agent_mode 2: aiFAQ
            if agent_mode == 0:
                active_tools = [tools_dict["insertRequestT"], tools_dict["selectComplaintT"]]
            elif agent_mode == 1:
                active_tools = [tools_dict["responseComplaintT"]]
            else:
                active_tools = [tools_dict["aiFAQ"]]

            # 동적 툴이 바인딩된 에이전트 생성 (매 요청마다 모드별 최적화)
            memory = MemorySaver()
            current_system_prompt = f"""
            너는 한국인 민원인들의 민원 처리를 돕는 한국어를 사용하는 AI 도우미야.
            현재 실행 중인 모드(`agent_mode`)는 **{agent_mode}**야.

            agent_mode가 0일 경우: 민원의 접수, 조회, 수정, 삭제의 요청을 받고 안내해.
            민원의 접수일 경우 좌측에 form을 띄워주고 접수 내용도 존재한다면 추천 제목과 내용으로 form을 채워줄거야.
            민원인은 그 내용을 검토하고 제출 버튼을 눌러 접수할 수 있어.
            민원의 조회, 수정, 삭제의 경우 민원인이 묘사한 민원 내용을 기반으로 가장 유사하다고 판단되는 민원들을 가져와서
            목록으로 만들어 좌측에 띄워줄거야. 좌측의 목록을 통해 유저가 찾고자 하는 민원을 수정하거나 삭제할 수 있어.
            유저가 단순히 문제 상황을 설명하는 경우 민원의 접수로 판단하고 Tool insertRequestT를 호출해.
            유저가 문제 상황을 설명하고 수정 및 삭제를 요청할 경우에는 selectComplaintT를 사용해.

            agent_mode가 1일 경우: 받는 메시지는 서버의 민원 처리 결과 내용이고, 이걸 유저에게 알려줘야해.

            agent_mode가 2일 경우: 민원과 관련된 사용자의 질문을 받고 적절한 대답을 해주어야해.
            유저가 문제 상황을 설명하는 경우 어떻게 행동해야 하는지에 대하여 적절한 대답을 해주어야해.
            
            [규칙]
            - 언제나 한국어만 사용해.
            - 사용자에게 절대로 민원의 ID나 번호는 묻거나 언급하지마.
            - 민원과 관련되지 않은 질문에는 민원과 관련된 답변만 제공함을 안내해.
            """
            
            agent_executor = create_agent(llm, active_tools, checkpointer=memory, system_prompt=current_system_prompt)

            config = {"configurable": {"user_id": user_id, "thread_id": user_id}}
            
            # 쿼리에 현재 모드 정보를 명시적으로 주입
            injected_query = f"[현재 agent_mode: {agent_mode}]\n사용자 질문: {query}"
            
            final_code = 0
            final_message = ""
            final_result_list = []
            tool_message = None

            async for event in agent_executor.astream_events(
                {"messages": [("human", injected_query)]}, 
                config=config,
                version="v2"
            ):
                kind = event["event"]
                
                if kind == "on_chat_model_end":
                    output_message = event["data"].get("output")
                    if hasattr(output_message, "content") and output_message.content:
                        if not getattr(output_message, "tool_calls", None):
                            final_message = output_message.content
                
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

            if tool_message and not final_message:
                final_message = tool_message

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
    if isinstance(tool_output, dict):
        return tool_output
    
    content = getattr(tool_output, "content", tool_output)
    if isinstance(content, dict):
        return content
    
    if isinstance(content, str):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            try:
                res = ast.literal_eval(content)
                if isinstance(res, dict):
                    return res
            except Exception:
                pass

    return None

async def enqueue_chat_request(user_id: str, chat_body: ChatRequestBody) -> ChatResponseBody:
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    await request_queue.put((user_id, chat_body, future))
    return await future


# --- FastAPI 생명주기(Lifespan) 설정 ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global worker_task
    
    # 툴 딕셔너리 구성
    tools_dict = {
        "aiFAQ": aiFAQ,
        "insertRequestT": insertRequestT,
        "selectComplaintT": selectComplaintT,
        "responseComplaintT": responseComplaintT
    }
    
    llm = ChatOllama(model=LLM_MODEL, temperature=0.1)

    # 백그라운드 워커에 llm과 tools_dict 전달
    worker_task = asyncio.create_task(llm_worker(llm, tools_dict))
    logger.info("--- LLM 백그라운드 워커가 시작되었습니다. ---")
    
    yield
    
    if worker_task:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            logger.info("--- LLM 백그라운드 워커가 종료되었습니다. ---")