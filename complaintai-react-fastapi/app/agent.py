import asyncio
from typing import AsyncGenerator
from langchain_core.tools import tool, InjectedToolArg
from langchain_core.runnables import RunnableConfig
from langchain_ollama import ChatOllama
from langchain.agents import create_agent

from .ai import LLM_MODEL, embeddingForSelect
from .db import fetch_all
import logging

# 1. 로거 생성
logger = logging.getLogger("test")
logger.setLevel(logging.DEBUG)

if not logger.handlers:
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

@tool
async def aiFAQ():
    """민원인이 민원처리와 관련된 요청을 하는 경우가 아니라면 LLM 모델이 직접 답변합니다."""
    return {"code": 0}

@tool
def insertRequest():
    """민원인이 민원을 요청하는 것으로 보인다면 접수를 진행할 수 있도록 접수를 진행할 수 있는 화면을 좌측에 띄워줍니다."""
    return {"code": 1}

@tool
def responseComplaintT(message: str):
    """
        message는 민원인이 민원처리를 한 결과를 나타냅니다.
        이 message 내용을 읽고 민원인에게 처리 결과를 알려줍니다.
    """
    return {"message": message, "code": 3}

@tool
async def selectComplaintT(content: str, is_described: bool, config: RunnableConfig):
    """
    민원인이 민원의 조회, 수정, 삭제를 요청한 경우에만 관련 민원을 조회합니다.
    content: 민원인이 찾고 싶어하는 민원에 대한 묘사. 만약, 묘사가 없을 경우 None
    is_described: content가 None이면 false, None이 아니면 True
    만약, 민원인이 어떤 민원을 처리하고 싶은지 묘사하지 않았다면 처리하고 싶은 민원의 내용을 알려달라고 요청합니다.
    """
    # config의 configurable 안에서 현재 요청을 보낸 user_id를 안전하게 가져옵니다.
    configurable = config.get("configurable", {})
    user_id = configurable.get("user_id")
    
    rows = []
    if is_described and user_id:
        vector = await embeddingForSelect(content)
        rows = fetch_all(
            "SELECT id, title, content FROM complaints WHERE owner_user_id=%s ORDER BY embedding <=> %s::vector LIMIT 3;",
            (user_id, str(vector))
        )

    return {"result": rows, "code": 2}


# --- 다중 사용자 순차 처리를 위한 큐 및 워커 시스템 ---

# 요청을 담을 비동기 큐
request_queue = asyncio.Queue()

async def llm_worker(agent_executor):
    """큐에서 민원인의 요청을 하나씩 꺼내 순서대로 처리하는 워커"""
    while True:
        user_id, query, response_callback = await request_queue.get()
        try:
            logger.info(f"--- [처리 시작] 유저 ID: {user_id} | 질문: {query} ---")
            
            # 사용자별 세션/컨텍스트를 config에 담아 에이전트에 전달
            config = {"configurable": {"user_id": user_id}}
            
            async for step in agent_executor.astream(
                {"messages": [("human", query)]}, 
                config=config,
                stream_mode="values"
            ):
                messages = step.get("messages", [])
                if messages:
                    latest_message = messages[-1]
                    
                    # AIMessage이고, tool_calls가 포함되어 있는지 확인
                    if hasattr(latest_message, "tool_calls") and latest_message.tool_calls:
                        for tool_call in latest_message.tool_calls:
                            tool_name = tool_call.get("name")
                            tool_content = tool_call.get("content")  
                            logger.info(f"[{user_id}] Tool 호출됨 [{tool_name}] - 반환값: {tool_content}")
                    latest_message.pretty_print()
                
            logger.info(f"--- [처리 완료] 유저 ID: {user_id} ---\n")
            
            if response_callback:
                response_callback("success") # 필요시 처리 완료 콜백 호출
                
        except Exception as e:
            logger.error(f"[{user_id}] 처리 중 오류 발생: {e}")
        finally:
            request_queue.task_done()


async def enqueue_chat_request(user_id: str, query: str):
    """외부(API 등)에서 유저의 요청을 받아 큐에 집어넣는 함수"""
    await request_queue.put((user_id, query, None))


async def main():
    tools = [aiFAQ, insertRequest, selectComplaintT, responseComplaintT]

    llm = ChatOllama(
        model=LLM_MODEL,
        temperature=0,
    )

    agent_executor = create_agent(llm, tools, system_prompt="""
        너는 한국인 민원인들의 민원 처리를 돕는 한국어를 사용하는 AI 도우미야.
        민원인들과 대화할때는 언제나 항상 한국어만 사용해야해.
        사용자는 너를 화면의 우측에 있는 챗봇으로 인식해.
        민원인은 민원 처리 이외에도 여러가지 메시지를 보내는데, 그럴땐 aiFAQ Tool을 사용하면 돼.
        민원인이 확실히 민원 접수를 요청한다고 판단되면 insertRequest Tool을 사용하면 돼.
        민원인이 민원의 접수,수정,삭제 요청을 할 경우 selectComplaintT Tool을 사용하면 돼.
        민원 처리가 이루어진 이후에는 서버의 응답을 받는 경우가 있는데, 그런 경우에는 responseComplaintT를 사용하면 돼.
        어떤 도구를 써야할지 모르겠으면 무조건 aiFAQ Tool을 쓰도록 해.
    """)

    # 백그라운드에서 큐를 순차적으로 처리할 워커 실행
    worker_task = asyncio.create_task(llm_worker(agent_executor))

    # 테스트를 위한 여러 명의 유저 시뮬레이션
    user_a = "00000000-0000-4000-9000-000000000001"
    user_b = "00000000-0000-4000-9000-000000000002"

    # 동시에 여러 요청이 들어왔다고 가정하고 큐에 순서대로 적재
    await enqueue_chat_request(user_a, "한강에 왔는데 누가 쓰레기를 버렸어요.")
    await enqueue_chat_request(user_b, "민원을 수정하고 싶어요.")
    await enqueue_chat_request(user_a, "과거에 한라산에서 불을 피웠던 민원을 수정하고 싶어요.")

    # 큐에 있는 모든 작업이 끝날 때까지 대기
    await request_queue.join()
    
    # 워커 태스크 종료
    worker_task.cancel()

if __name__ == '__main__':
    asyncio.run(main())