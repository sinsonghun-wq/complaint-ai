import asyncio
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from app import agent
from app.models import ChatRequestBody, ChatResponseBody


class ChatDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_disconnect_cancels_queued_reply_instead_of_leaving_it_running(self):
        from app.routers import complaints
        cancelled=asyncio.Event()
        async def pending(*args):
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        request=Mock(is_disconnected=AsyncMock(return_value=True))
        with patch.object(complaints,'enqueue_chat_request',side_effect=pending):
            with self.assertRaises(HTTPException) as error:
                await complaints.chatAI(ChatRequestBody(content='문의',agent_mode=1),request,{'owner_id':'test'})
        self.assertEqual(error.exception.status_code,499);self.assertTrue(cancelled.is_set())

    async def start_worker(self, process):
        for name,value in [('request_queue',asyncio.Queue()),('CHAT_REQUEST_TIMEOUT_SECONDS',0.08),('process_chat_request',process)]:
            context=patch.object(agent,name,value);context.start();self.addCleanup(context.stop)
        task=asyncio.create_task(agent.llm_worker(None,{}))
        context=patch.object(agent,'worker_task',task);context.start();self.addCleanup(context.stop)
        async def stop():
            task.cancel();await asyncio.gather(task,return_exceptions=True)
        self.addAsyncCleanup(stop)
        return task

    async def test_stuck_request_times_out_and_the_next_request_still_runs(self):
        cancelled=asyncio.Event()
        async def process(llm,tools,user,body):
            if body.content=='slow':
                try:await asyncio.Event().wait()
                finally:cancelled.set()
            return ChatResponseBody(code=0,message='완료',result=[])
        await self.start_worker(process)
        with self.assertRaises(HTTPException) as error:
            await agent.enqueue_chat_request('test',ChatRequestBody(content='slow',agent_mode=1))
        self.assertEqual(error.exception.status_code,504)
        result=await agent.enqueue_chat_request('test',ChatRequestBody(content='fast',agent_mode=1))
        self.assertEqual(result.message,'완료')
        self.assertTrue(cancelled.is_set())
        await asyncio.wait_for(agent.request_queue.join(),0.2)

    async def test_disconnected_client_cancels_work_and_does_not_block_the_queue(self):
        started=asyncio.Event();cancelled=asyncio.Event()
        async def process(llm,tools,user,body):
            if body.content=='slow':
                started.set()
                try:await asyncio.Event().wait()
                finally:cancelled.set()
            return ChatResponseBody(code=0,message='완료',result=[])
        await self.start_worker(process)
        task=asyncio.create_task(agent.enqueue_chat_request('test',ChatRequestBody(content='slow',agent_mode=1)))
        await started.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        result=await agent.enqueue_chat_request('test',ChatRequestBody(content='fast',agent_mode=1))
        self.assertTrue(cancelled.is_set());self.assertEqual(result.message,'완료')

    async def test_expired_queued_request_never_runs_the_model(self):
        calls=[]
        async def process(*args):calls.append(args)
        await self.start_worker(process)
        future=asyncio.get_running_loop().create_future()
        await agent.request_queue.put(('test',ChatRequestBody(content='expired',agent_mode=1),future,time.monotonic()-1))
        with self.assertRaises(HTTPException) as error:await future
        self.assertEqual(error.exception.status_code,504);self.assertEqual(calls,[])

    async def test_processing_error_is_reported_and_worker_stays_available(self):
        async def process(llm,tools,user,body):
            if body.content=='error':raise ValueError('test failure')
            return ChatResponseBody(code=0,message='완료',result=[])
        task=await self.start_worker(process)
        with self.assertRaises(HTTPException) as error:
            await agent.enqueue_chat_request('test',ChatRequestBody(content='error',agent_mode=1))
        self.assertEqual(error.exception.status_code,503);self.assertFalse(task.done())
        result=await agent.enqueue_chat_request('test',ChatRequestBody(content='fast',agent_mode=1))
        self.assertEqual(result.message,'완료')
