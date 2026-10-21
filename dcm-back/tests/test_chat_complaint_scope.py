import unittest
from unittest.mock import AsyncMock, patch

from app.agent import selectComplaintT


class ChatComplaintScopeTests(unittest.IsolatedAsyncioTestCase):
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
