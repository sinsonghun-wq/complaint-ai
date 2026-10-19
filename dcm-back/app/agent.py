import ast
import asyncio
import json
from fastapi import FastAPI
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from langchain_core.messages import ToolMessage
from langchain.agents import create_agent
from contextlib import asynccontextmanager
from langgraph.errors import GraphRecursionError

from app.models import ChatRequestBody, ChatResponseBody

from . import prompts
from .logger import logger
from . import llm as llm_backend
from .ai import embeddingForSelect
from .db import fetch_all
from langgraph.checkpoint.memory import MemorySaver

RAG_ROW_LIMIT = 1
COMPLAINT_ROW_LIMIT = 3

# --- 툴 정의 ---
async def aiFAQ(content: str, is_described: bool) -> dict:
    rows = []
    vector = await embeddingForSelect(content) if is_described  else None
    if vector:
        rows = fetch_all(
            f"""
                SELECT SUBSTRING(content, 1, 3000) AS content
                FROM department_documents 
                WHERE deleted_at IS NULL 
                ORDER BY embedding <=> %s::vector 
                LIMIT {RAG_ROW_LIMIT};
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
                LIMIT {COMPLAINT_ROW_LIMIT};
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

# 요청마다 새로 만들면 이전 대화가 사라지므로 전역으로 한 번만 만듭니다. (thread_id=user_id 로 유저별 기록 구분)
global_memory = MemorySaver()

async def llm_worker(llm, tools_dict):
    while True:
        future = None
        user_id = "unknown"
        try:
            user_id, chatbody, future = await request_queue.get()
            query = chatbody.content
            agent_mode = chatbody.agent_mode

            logger.info(f"--- [처리 시작] 유저 ID: {user_id} | agent_mode:{agent_mode} | 질문: {query} ---")
            
            # agent_mode에 따른 툴 분기
            if agent_mode == 0:
                active_tools = [tools_dict["insertRequestT"], tools_dict["selectComplaintT"]]
            elif agent_mode == 1:
                active_tools = [tools_dict["aiFAQ"]]
            else:
                active_tools = []

            # 동적 툴이 바인딩된 에이전트 생성 (매 요청마다 모드별 최적화)
            current_system_prompt = prompts.agent_system_prompt(agent_mode)

            agent_executor = create_agent(llm, active_tools, checkpointer=global_memory, system_prompt=current_system_prompt)

            # 모드와 상관없이 유저별로 대화 기록을 공유하기 위해 thread_id를 user_id로 고정
            config = {
                "configurable": {
                    "user_id": user_id, 
                    "thread_id": user_id
                },
                "recursion_limit": 10
            }
            
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
                        final_message = output_message.content
                
                elif kind == "on_tool_end":
                    tool_output = event["data"].get("output")
                    tool_name = event.get("name")
                    logger.info(f"[{user_id}] Tool 호출됨 [{tool_name}] - 원본 반환값: {str(tool_output)[:200]}")
                    
                    data = parse_tool_output_safe(tool_output)
                    if data:
                        if "code" in data:
                            final_code = data["code"]
                        if "result" in data and data["result"]:
                            final_result_list = data["result"]

            # 문서 내용은 그 답변에만 쓰고, 기록에는 짧은 문구만 남깁니다. (컨텍스트 초과 방지)
            if agent_mode == 1:
                state = await agent_executor.aget_state(config)
                stubs = [
                    ToolMessage(content="(이전 검색 결과 생략)", tool_call_id=m.tool_call_id, name=m.name, id=m.id)
                    for m in state.values.get("messages", [])
                    if isinstance(m, ToolMessage) and m.name == "aiFAQ" and m.content != "(이전 검색 결과 생략)"
                ]
                if stubs:
                    await agent_executor.aupdate_state(config, {"messages": stubs})

            if tool_message and not final_message:
                final_message = tool_message

            if final_code == 0:
                final_result_list = []
                
            final_response = ChatResponseBody(
                code=final_code,
                message=final_message,
                result=final_result_list
            )
            
            logger.info(f"--- [처리 완료] 유저 ID: {user_id} | 응답: {final_response} ---\n")
            
            if future and not future.done():
                future.set_result(final_response)
                
        except GraphRecursionError as e:
            logger.error(f"[{user_id}] LangGraph Recursion Limit 도달: {e}")
            final_response = ChatResponseBody(
                code=0,
                message="죄송합니다, 내용을 제대로 이해하지 못했습니다.",
                result=[]
            )
            if future and not future.done():
                future.set_result(final_response)
                
        except Exception as e:
            logger.error(f"[{user_id}] 처리 중 오류 발생: {e}")
            if future and not future.done():
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
    
    tools_dict = {
        "aiFAQ": aiFAQ,
        "insertRequestT": insertRequestT,
        "selectComplaintT": selectComplaintT,
    }
    
    await llm_backend.check_backend()
    llm = llm_backend.make_chat_model(llm_backend.TASK_CHAT, temperature=0.2)

    worker_task = asyncio.create_task(llm_worker(llm, tools_dict))
    logger.info("--- LLM 백그라운드 워커가 시작되었습니다. ---")
    
    yield
    
    if worker_task:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            logger.info("--- LLM 백그라운드 워커가 종료되었습니다. ---")