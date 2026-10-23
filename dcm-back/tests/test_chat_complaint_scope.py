import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.agent import selectComplaintT
from app.agent import llm_worker


class ChatComplaintScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_chat_worker_shutdown_does_not_complete_a_nonexistent_request(self):
        with patch('app.agent.request_queue', asyncio.Queue()) as queue:
            task = asyncio.create_task(llm_worker(None, {}))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            await asyncio.wait_for(queue.join(), timeout=0.2)

    async def test_own_complaint_tool_excludes_trash_and_cancelled_records(self):
        config = {"configurable": {"user_id": "owner-test"}}
        with patch("app.agent.embeddingForSelect", new=AsyncMock(return_value=[0.1] * 1024)), patch("app.agent.fetch_all", return_value=[]) as fetch:
            result = await selectComplaintT.coroutine("도로 보수", config)
        query, parameters = fetch.call_args.args
        self.assertIn("owner_user_id = %s", query)
        self.assertIn("deleted_at IS NULL", query)
        self.assertIn("complaint_status<>'취소'", query)
        self.assertEqual(parameters[0], "owner-test")
        self.assertEqual(result, {"code": 2, "result": []})
