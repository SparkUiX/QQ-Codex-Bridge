import unittest
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from codex_client import CodexClient, CodexError
from shared_runtime import SharedProxy


class CodexClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_websocket_connection_state_detects_reader_failure(self):
        client = CodexClient(shared=True)
        client.ws = SimpleNamespace(closed=False)
        client.reader_task = SimpleNamespace(done=lambda: False)
        self.assertTrue(client.connected)
        client.reader_task = SimpleNamespace(done=lambda: True)
        self.assertFalse(client.connected)

    async def test_rate_limits_retries_with_fresh_connection(self):
        client = CodexClient()
        calls = []

        async def request(self, method, params=None, **kwargs):
            calls.append(method)
            if len(calls) == 1:
                raise CodexError("temporary network failure")
            return {"rateLimits": {"limitId": "codex"}}

        async def connect(self):
            return self

        async def close(self):
            pass

        with patch.object(CodexClient, "request", request), patch.object(CodexClient, "connect", connect), patch.object(CodexClient, "close", close):
            result = await client.rate_limits()
        self.assertEqual(result["rateLimits"]["limitId"], "codex")
        self.assertEqual(calls, ["account/rateLimits/read", "account/rateLimits/read"])

    async def test_shared_connection_requires_control_socket(self):
        client = CodexClient(shared=True)
        with patch.object(client, "shared_socket") as socket, patch("asyncio.create_subprocess_exec") as spawn:
            socket.return_value.exists.return_value = False
            with self.assertRaisesRegex(CodexError, "控制套接字"):
                await client.connect()
            spawn.assert_not_called()

    async def test_new_thread_starts_first_turn_without_resume(self):
        client = CodexClient()
        calls = []

        async def request(method, params=None, **_kwargs):
            calls.append(method)
            if method == "thread/start":
                return {"thread": {"id": "new-thread"}}
            if method == "turn/start":
                return {"turn": {"id": "first-turn"}}
            raise AssertionError(f"unexpected method: {method}")

        client.request = request
        client.read_thread = AsyncMock(return_value={"status": {"type": "idle"}})
        with patch.object(client, "_other_process_has_active_turn", return_value=False):
            thread = await client.start_thread("C:/project")
            turn_id, steered = await client.send(thread["id"], "你好")

        self.assertEqual((turn_id, steered), ("first-turn", False))
        self.assertEqual(calls, ["thread/start", "turn/start"])

    async def test_existing_thread_resumes_before_turn(self):
        client = CodexClient()
        calls = []

        async def request(method, params=None, **_kwargs):
            calls.append(method)
            if method == "turn/start":
                return {"turn": {"id": "next-turn"}}
            return {}

        client.request = request
        client.read_thread = AsyncMock(return_value={"status": {"type": "idle"}})
        with patch.object(client, "_other_process_has_active_turn", return_value=False):
            await client.send("existing-thread", "继续")

        self.assertEqual(calls, ["thread/resume", "turn/start"])

    async def test_fork_is_loaded_for_first_turn(self):
        client = CodexClient()
        calls = []
        async def request(method, params=None, **_kwargs):
            calls.append(method)
            if method == "thread/fork":
                return {"thread": {"id": "copy"}}
            if method == "turn/start":
                return {"turn": {"id": "turn"}}
            raise AssertionError(method)
        client.request = request
        client.read_thread = AsyncMock(return_value={"status": {"type": "idle"}})
        with patch.object(client, "_other_process_has_active_turn", return_value=False):
            fork = await client.fork_thread("source")
            await client.send(fork["id"], "继续")
        self.assertEqual(calls, ["thread/fork", "turn/start"])

    async def test_configured_shared_runtime_does_not_spawn_private_server(self):
        client = CodexClient(shared=True)
        with patch.dict(os.environ, {"CODEXQQ_APP_SERVER_URL": "ws://127.0.0.1:1"}), patch("asyncio.create_subprocess_exec") as spawn:
            with self.assertRaises(Exception):
                await client.connect()  # No test listener is required.
            spawn.assert_not_called()


class FakeSocket:
    closed = False

    def __init__(self):
        self.messages = []

    async def send_json(self, value):
        self.messages.append(value)


class SharedProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_response_to_requester_and_broadcasts_turn(self):
        proxy = SharedProxy()
        bridge, desktop = FakeSocket(), FakeSocket()
        proxy.clients.update((bridge, desktop))
        proxy.upstream = FakeSocket()
        proxy.upstream.send_str = AsyncMock()
        await proxy._from_client(bridge, {"id": 7, "method": "turn/start", "params": {"threadId": "t"}})
        forwarded = json.loads(proxy.upstream.send_str.await_args.args[0])
        await proxy._from_upstream({"id": forwarded["id"], "result": {"threadId": "t", "turn": {"id": "u"}}})
        self.assertEqual(bridge.messages[0]["id"], 7)
        self.assertEqual(desktop.messages[0]["method"], "turn/started")
        self.assertIs(proxy.turn_owners["u"], bridge)

    async def test_missing_approval_owner_is_rejected(self):
        proxy = SharedProxy()
        proxy.upstream = FakeSocket()
        proxy.upstream.send_str = AsyncMock()
        await proxy._from_upstream({"id": 9, "method": "item/commandExecution/requestApproval", "params": {"threadId": "t", "turnId": "u"}})
        response = json.loads(proxy.upstream.send_str.await_args.args[0])
        self.assertEqual(response["id"], 9)
        self.assertIn("error", response)


if __name__ == "__main__":
    unittest.main()
