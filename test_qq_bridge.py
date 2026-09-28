import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from qq_bridge import Bridge, inbound_key, referenced_index
from qqbot_agent_sdk import QQApiClient
from codex_client import CodexError
from codex_projects import Project, ProjectSnapshot
from qq_state import State


class FakeApi:
    def __init__(self):
        self.sent = []

    async def send_text(self, scope, user, content, **kwargs):
        self.sent.append((scope, user, content, kwargs))
        n = len(self.sent)
        return {"id": f"bot-{n}", "ext_info": {"ref_idx": f"ref-{n}"}}

    def next_msg_seq(self):
        return 1

    async def post_c2c_message(self, user, msg, *, keyboard):
        self.sent.append(("c2c", user, msg.content, {"keyboard": keyboard}))
        return {"id": f"bot-{len(self.sent)}"}

    build_text_body = staticmethod(QQApiClient.build_text_body)

    async def request(self, method, path, body):
        self.sent.append((method, path, body.get("content") or (body.get("markdown") or {}).get("content"), {"body": body}))
        return {"id": f"bot-{len(self.sent)}"}

    async def acknowledge_interaction(self, ident):
        pass


class FakeCodex:
    def __init__(self):
        self.calls = []
        self.threads_data = []

    async def threads(self):
        return self.threads_data

    async def send(self, thread_id, content, *, allow_steer):
        self.calls.append((thread_id, content, allow_steer))
        return "turn-1", False

    async def models(self):
        return [{"id": "model-a", "model": "model-a", "displayName": "Model A",
                 "supportedReasoningEfforts": [{"reasoningEffort": "medium"}, {"reasoningEffort": "high"}]}]

    async def start_thread(self, cwd, model=None):
        self.calls.append(("start", cwd, model))
        return {"id": "created-thread"}


def incoming(message_id, text, *, user="owner", ref=None):
    ext = [f"msg_idx=idx-{message_id}"]
    if ref:
        ext.append(f"ref_msg_idx={ref}")
    return {
        "id": message_id, "author": {"id": user, "user_openid": user, "bot": False},
        "content": text, "message_type": 0,
        "message_scene": {"ext": ext}, "timestamp": "2026-09-28T10:00:00+08:00",
    }


def interaction(ident, data, user="owner"):
    return {"id": ident, "user_openid": user, "chat_type": 1,
            "data": {"resolved": {"button_data": data}}}


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "state.sqlite3")
        self.api = FakeApi()
        self.codex = FakeCodex()
        self.bridge = Bridge(self.state, self.api, self.codex, "owner")

    async def asyncTearDown(self):
        self.state.close()
        self.tmp.cleanup()

    async def test_authorization_and_persistent_dedup(self):
        raw = incoming("q1", "Run tests", user="stranger")
        await self.bridge.on_message("C2C_MESSAGE_CREATE", raw)
        self.assertEqual(self.api.sent, [])
        self.state.put("selected_thread", "task-a")
        raw = incoming("q1", "Run tests")
        await self.bridge.on_message("C2C_MESSAGE_CREATE", raw)
        await self.bridge.on_message("C2C_MESSAGE_CREATE", raw)
        self.assertEqual(self.codex.calls, [("task-a", "Run tests", True)])
        self.assertEqual(len(self.api.sent), 0)
        self.assertFalse(self.state.claim(inbound_key(raw)))

    async def test_status_shows_shared_desktop_connection(self):
        self.state.put("selected_thread", "task-a")
        self.codex.read_thread = AsyncMock(return_value={"name": "Task A", "status": {"type": "idle"}})
        self.bridge.shared_status = AsyncMock(return_value="共享服务：正常；Desktop：已连接")
        await self.bridge.on_message("C2C_MESSAGE_CREATE", incoming("status", "/status"))
        self.assertIn("Desktop：已连接", self.api.sent[-1][2])
        self.assertIn("状态：idle", self.api.sent[-1][2])

    async def test_owned_turn_streams_and_suppresses_duplicate_stop_notice(self):
        self.state.put("selected_thread", "task-a")
        await self.bridge.on_message("C2C_MESSAGE_CREATE", incoming("q-stream", "Hello"))
        await self.bridge.on_codex_event("item/started", {
            "threadId": "task-a", "turnId": "turn-1",
            "item": {"id": "item-1", "type": "agentMessage", "phase": "commentary"},
        })
        await self.bridge.on_codex_event("item/agentMessage/delta", {
            "threadId": "task-a", "turnId": "turn-1", "itemId": "item-1", "delta": "先查资料。",
        })
        await self.bridge.on_codex_event("item/completed", {
            "threadId": "task-a", "turnId": "turn-1",
            "item": {"id": "search-1", "type": "webSearch", "query": "OpenAI 新闻"},
        })
        await self.bridge.on_codex_event("item/started", {
            "threadId": "task-a", "turnId": "turn-1",
            "item": {"id": "final-1", "type": "agentMessage", "phase": "final_answer"},
        })
        await self.bridge.on_codex_event("item/agentMessage/delta", {
            "threadId": "task-a", "turnId": "turn-1", "itemId": "final-1", "delta": "最终结论。",
        })
        await self.bridge.on_codex_event("item/completed", {
            "threadId": "task-a", "turnId": "turn-1",
            "item": {"id": "final-1", "type": "agentMessage", "phase": "final_answer", "text": "最终结论。"},
        })
        await self.bridge.on_codex_event("turn/completed", {
            "threadId": "task-a", "turn": {"id": "turn-1", "status": "completed"},
        })
        frames = [entry[3]["body"] for entry in self.api.sent if entry[1].endswith("/stream_messages")]
        self.assertEqual([frame["input_state"] for frame in frames], [1, 10])
        self.assertEqual([frame["index"] for frame in frames], [0, 1])
        self.assertEqual(frames[0]["msg_id"], "q-stream")
        self.assertEqual(frames[0]["msg_seq"], frames[1]["msg_seq"])
        self.assertEqual(frames[1]["content_raw"], "先查资料。\n\n🌐 已搜索网页：OpenAI 新闻")
        self.assertIn("stream_msg_id", frames[1])
        self.assertEqual(self.api.sent[-1][2], "最终结论。")
        self.assertFalse(self.state.claim_turn("task-a", "turn-1"))

    async def test_stream_failure_leaves_stop_hook_available(self):
        self.state.put("selected_thread", "task-a")
        await self.bridge.on_message("C2C_MESSAGE_CREATE", incoming("q-fail", "Hello"))
        async def reject_stream(method, path, body):
            raise RuntimeError("stream capability unavailable")
        self.api.request = reject_stream
        await self.bridge.on_codex_event("item/started", {
            "threadId": "task-a", "turnId": "turn-1",
            "item": {"id": "comment-1", "type": "agentMessage", "phase": "commentary"},
        })
        await self.bridge.on_codex_event("item/agentMessage/delta", {
            "threadId": "task-a", "turnId": "turn-1", "itemId": "comment-1", "delta": "Hello",
        })
        await self.bridge.on_codex_event("turn/completed", {
            "threadId": "task-a", "turn": {"id": "turn-1", "status": "completed"},
        })
        self.assertTrue(self.state.claim_turn("task-a", "turn-1"))

    async def test_desktop_turn_sends_only_final_without_hook_duplicate(self):
        self.codex.shared = True
        await self.bridge.on_codex_event("item/started", {
            "threadId": "desktop-task", "turnId": "desktop-turn",
            "item": {"id": "comment", "type": "agentMessage", "phase": "commentary"},
        })
        key = {"threadId": "desktop-task", "turnId": "desktop-turn", "itemId": "comment", "delta": "Desktop 正在处理"}
        await self.bridge.on_codex_event("item/agentMessage/delta", key)
        self.assertEqual(self.api.sent, [])
        await self.bridge.on_codex_event("item/completed", {
            "threadId": "desktop-task", "turnId": "desktop-turn",
            "item": {"id": "final", "type": "agentMessage", "phase": "final_answer", "text": "最终答复。"},
        })
        await self.bridge.on_codex_event("turn/completed", {
            "threadId": "desktop-task", "turn": {"id": "desktop-turn", "status": "completed"},
        })
        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(self.api.sent[0][2], "最终答复。")
        self.assertFalse(self.state.claim_turn("desktop-task", "desktop-turn"))

    async def test_active_writer_forks_and_routes_followup_to_copy(self):
        self.state.put("selected_thread", "desktop-task")
        self.state.set_effort("desktop-task", "high")
        calls = []
        forks = []
        async def send(thread_id, content, *, allow_steer, effort=None):
            calls.append((thread_id, content, effort))
            if thread_id == "desktop-task":
                raise CodexError("{'code': -32600, 'message': 'thread desktop-task already has an active writer'}")
            return "fork-turn", False
        async def fork_thread(thread_id):
            forks.append(thread_id)
            self.assertEqual(thread_id, "desktop-task")
            return {"id": "qq-copy"}
        self.codex.send = send
        self.codex.fork_thread = fork_thread
        await self.bridge.on_message("C2C_MESSAGE_CREATE", incoming("q-copy", "继续"))
        self.assertEqual(calls, [("desktop-task", "继续", "high"), ("qq-copy", "继续", "high")])
        self.assertEqual(self.state.get("selected_thread"), "qq-copy")
        self.assertEqual(self.state.effort_for_thread("qq-copy"), "high")
        self.assertIn("已复制到新任务", self.api.sent[-1][2])
        await self.bridge.on_message("C2C_MESSAGE_CREATE", incoming("q-followup", "再继续"))
        self.assertEqual(forks, ["desktop-task"])
        self.assertEqual(calls[-1], ("qq-copy", "再继续", "high"))

    async def test_quote_overrides_selected_task(self):
        self.state.put("selected_thread", "task-a")
        self.state.bind_outbound({"id": "bot-id", "ext_info": {"ref_idx": "target-ref"}}, "task-b")
        raw = incoming("q2", "Continue here", ref="target-ref")
        self.assertEqual(referenced_index(raw), "target-ref")
        await self.bridge.on_message("C2C_MESSAGE_CREATE", raw)
        self.assertEqual(self.codex.calls[0][0], "task-b")

    async def test_empty_new_thread_is_replaced_and_old_quote_routes_to_it(self):
        root = Path(self.tmp.name)
        project_path = root / "project"
        project_path.mkdir()
        snapshot = ProjectSnapshot([Project("project-1", "My Project", project_path)], {}, {})
        self.state.put("selected_thread", "empty-thread")
        self.state.bind_project("empty-thread", "project-1")
        self.state.bind_outbound(
            {"id": "old-ack", "ext_info": {"ref_idx": "old-ref"}}, "empty-thread"
        )

        async def send(thread_id, content, *, allow_steer):
            self.codex.calls.append((thread_id, content, allow_steer))
            if thread_id == "empty-thread":
                raise CodexError("{'code': -32600, 'message': 'no rollout found for thread id empty-thread'}")
            return "turn-1", False

        async def start_thread(cwd):
            self.assertEqual(cwd, str(project_path))
            return {"id": "replacement-thread"}

        self.codex.send = send
        self.codex.start_thread = start_thread
        self.codex.has_persisted_thread = lambda _thread_id: False
        with patch("qq_bridge.load_projects", return_value=snapshot):
            await self.bridge.on_message("C2C_MESSAGE_CREATE", incoming("q-new", "你好"))
            await self.bridge.on_message("C2C_MESSAGE_CREATE", incoming("q-quote", "继续", ref="old-ref"))

        self.assertEqual(self.state.get("selected_thread"), "replacement-thread")
        self.assertEqual(self.state.thread_for_ref("old-ref"), "replacement-thread")
        self.assertEqual([call[0] for call in self.codex.calls],
                         ["empty-thread", "replacement-thread", "replacement-thread"])
        self.assertIn("已自动换成新任务", self.api.sent[0][2])

    async def test_unselected_task_does_not_run(self):
        await self.bridge.on_message("C2C_MESSAGE_CREATE", incoming("q3", "Hello"))
        self.assertEqual(self.codex.calls, [])
        self.assertIn("/list", self.api.sent[0][2])

    async def test_project_names_and_grouped_list(self):
        root = Path(self.tmp.name)
        snapshot = ProjectSnapshot(
            [Project("p-a", "UI Alpha", root / "alpha"), Project("p-b", "UI Beta", root / "beta")],
            {"task-b": "p-b"}, {},
        )
        self.codex.threads_data = [
            {"id": "task-a", "name": "Older", "cwd": str(root / "alpha"), "recencyAt": 100},
            {"id": "task-b", "name": "Newest", "cwd": str(root / "alpha"), "recencyAt": 200},
        ]
        with patch("qq_bridge.load_projects", return_value=snapshot):
            await self.bridge.on_message("C2C_MESSAGE_CREATE", incoming("q8", "/projects"))
            self.assertIn("UI Alpha", self.api.sent[-1][2])
            self.assertNotIn(str(root), self.api.sent[-1][2])
            await self.bridge.on_message("C2C_MESSAGE_CREATE", incoming("q9", "/list"))
            await self.bridge.on_message("C2C_MESSAGE_CREATE", incoming("q10", "projects"))
        self.assertEqual([t["id"] for t in self.bridge.listed_threads], ["task-b", "task-a"])
        self.assertLess(self.api.sent[-2][2].index("UI Beta"), self.api.sent[-2][2].index("UI Alpha"))
        self.assertIn("UI Alpha", self.api.sent[-1][2])

    async def test_new_keyboard_selects_project_model_and_effort(self):
        project_path = Path(self.tmp.name) / "project"
        project_path.mkdir()
        snapshot = ProjectSnapshot([Project("project-1", "My Project", project_path)], {}, {})
        with patch("qq_bridge.load_projects", return_value=snapshot):
            await self.bridge.on_message("C2C_MESSAGE_CREATE", incoming("new-1", "/new"))
            initial = self.api.sent[-1][3]["body"]
            self.assertEqual(initial["msg_type"], 2)
            self.assertIn("keyboard", initial)
            self.assertIn("/pick 编号", initial["markdown"]["content"])
            nonce = self.bridge.new_flow["nonce"]
            await self.bridge.on_interaction("INTERACTION_CREATE", interaction("i0", f"new:{nonce}:project:0", "stranger"))
            self.assertEqual(self.bridge.new_flow["step"], "project")
            await self.bridge.on_interaction("INTERACTION_CREATE", interaction("i1", f"new:{nonce}:project:0"))
            self.assertEqual(self.bridge.new_flow["step"], "model")
            self.assertEqual(self.api.sent[-1][3]["body"]["msg_id"], "new-1")
            self.assertNotIn("event_id", self.api.sent[-1][3]["body"])
            await self.bridge.on_interaction("INTERACTION_CREATE", interaction("i2", f"new:{nonce}:model:0"))
            self.assertEqual(self.bridge.new_flow["step"], "effort")
            await self.bridge.on_interaction("INTERACTION_CREATE", interaction("i3", f"new:{nonce}:effort:1"))
        self.assertEqual(self.codex.calls[-1], ("start", str(project_path), "model-a"))
        self.assertEqual(self.state.effort_for_thread("created-thread"), "high")
        self.assertEqual(self.state.get("selected_thread"), "created-thread")
        self.assertIsNone(self.bridge.new_flow)

    async def test_new_text_fallback_can_select_project(self):
        project_path = Path(self.tmp.name) / "project"
        project_path.mkdir()
        snapshot = ProjectSnapshot([Project("project-1", "My Project", project_path)], {}, {})
        with patch("qq_bridge.load_projects", return_value=snapshot):
            await self.bridge.on_message("C2C_MESSAGE_CREATE", incoming("new-text", "/new"))
            await self.bridge.on_message("C2C_MESSAGE_CREATE", incoming("pick-project", "/pick 1"))
        self.assertEqual(self.bridge.new_flow["step"], "model")

    async def test_approval_one_time_and_scoped(self):
        self.bridge.owned_turns.add(("task-a", "turn-1"))
        pending = asyncio.create_task(self.bridge.on_approval(
            "item/commandExecution/requestApproval",
            {"threadId": "task-a", "turnId": "turn-1", "command": "git status"},
        ))
        for _ in range(20):
            if self.bridge.pending_approvals:
                break
            await asyncio.sleep(.01)
        code = next(iter(self.bridge.pending_approvals))
        await self.bridge.on_message("C2C_MESSAGE_CREATE", incoming("q4", f"批准 {code}", user="stranger"))
        self.assertFalse(pending.done())
        await self.bridge.on_message("C2C_MESSAGE_CREATE", incoming("q5", f"批准 {code}"))
        self.assertEqual(await pending, {"decision": "accept"})
        await self.bridge.on_message("C2C_MESSAGE_CREATE", incoming("q6", f"批准 {code}"))
        self.assertIn("无效或已过期", self.api.sent[-1][2])

    async def test_resolved_approval_cannot_be_confirmed(self):
        self.bridge.owned_turns.add(("task-a", "turn-1"))
        pending = asyncio.create_task(self.bridge.on_approval(
            "item/fileChange/requestApproval",
            {"threadId": "task-a", "turnId": "turn-1", "_request_id": 12},
        ))
        for _ in range(20):
            if self.bridge.pending_approvals:
                break
            await asyncio.sleep(.01)
        code = next(iter(self.bridge.pending_approvals))
        await self.bridge.on_codex_event("serverRequest/resolved", {"requestId": 12})
        self.assertEqual(await pending, {"decision": "decline"})
        await self.bridge.on_message("C2C_MESSAGE_CREATE", incoming("q7", f"批准 {code}"))
        self.assertIn("无效或已过期", self.api.sent[-1][2])


if __name__ == "__main__":
    unittest.main()
