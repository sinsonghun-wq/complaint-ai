import ast
import asyncio
import json
from fastapi import FastAPI
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from langchain.agents import create_agent
from contextlib import asynccontextmanager

from app.models import ChatRequestBody, ChatResponseBody

from . import prompts
from .logger import logger
from . import llm as llm_backend
from .ai import embeddingForSelect
from .db import fetch_all
from langgraph.checkpoint.memory import MemorySaver

ROW_LIMIT = 2

# --- 툴 정의 ---
# 도구 설명(LLM 이 읽는 문장)은 prompts.TOOL_DESCRIPTIONS 에서 관리합니다.
# LangChain 은 함수 docstring 을 도구 설명으로 쓰므로, 설명을 넣은 뒤에 tool() 로 감쌉니다.
async def aiFAQ(content: str, is_described: bool) -> dict:
    rows = []
    vector = await embeddingForSelect(content) if is_described  else None
    if vector:
        rows = fetch_all(
            f"""
                SELECT SUBSTRING(content, 1, 30000) AS content
                FROM department_documents 
                WHERE deleted_at IS NULL 
                ORDER BY embedding <=> %s::vector 
                LIMIT {ROW_LIMIT};
            """,
            (str(vector),)
        )

    return {"code": 0, "result": rows}

def insertRequestT(title: str, content: str) -> dict:
    return {"code": 1, "result": [{"title": title, "content": content}]}

async def selectComplaintT(content: str, config: RunnableConfig) -> dict:
    configurable = config.get("configurable", {})
    user_id = configurable.get("user_id")
    
    rows = []
    # content 가 비어 있으면(찾을 대상을 말하지 않음) 검색하지 않고 빈 결과를 돌려줍니다.
    vector = await embeddingForSelect(content) if content.strip() and user_id else None
    if vector:
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


for _fn in (aiFAQ, insertRequestT, selectComplaintT):
    _fn.__doc__ = prompts.TOOL_DESCRIPTIONS[_fn.__name__]
aiFAQ = tool(aiFAQ)
insertRequestT = tool(insertRequestT)
selectComplaintT = tool(selectComplaintT)


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
            # agent_mode 1: aiFAQ
            if agent_mode == 0:
                active_tools = [tools_dict["insertRequestT"], tools_dict["selectComplaintT"]]
            elif agent_mode == 1:
                active_tools = [tools_dict["aiFAQ"]]

            # 동적 툴이 바인딩된 에이전트 생성 (매 요청마다 모드별 최적화)
            memory = MemorySaver()
            current_system_prompt = prompts.agent_system_prompt(agent_mode)

            agent_executor = create_agent(llm, active_tools, checkpointer=memory, system_prompt=current_system_prompt)

            config = {"configurable": {"user_id": user_id, "thread_id": user_id}}
            
            # 모드는 시스템 프롬프트에 들어 있으므로 사용자 메시지에는 질문만 넣습니다. (prompts.agent_user_message)
            injected_query = prompts.agent_user_message(query)
            
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

             #aiFAQ의 경우 agent가 반환값을 볼 필요는 있지만, 유저한테까지 갈 필요는 없으므로 필터링
            if final_code ==0:
                final_result_list =[]
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
    }
    
    # 챗봇 모델 - .env 의 LLM_BACKEND 에 따라 ChatOllama 또는 ChatOpenAI(llama-server + 어댑터)
    # (Ollama 일 때 num_ctx 를 민원 분석과 같게 맞추는 것도 llm.make_chat_model 안에서 합니다)
    await llm_backend.check_backend()
    llm = llm_backend.make_chat_model(llm_backend.TASK_CHAT, temperature=0.2)

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