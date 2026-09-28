import asyncio
import io
import json
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

import qq_notify
from qq_state import State


class StopHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_hook_waits_for_in_progress_bridge_final(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            state = State(path)
            state.mark_streaming("thread-a", "turn-a")
            state.close()
            payload = json.dumps({
                "hook_event_name": "Stop", "session_id": "thread-a",
                "turn_id": "turn-a", "last_assistant_message": "Done",
            }).encode()
            sender = AsyncMock()

            async def bridge_finishes():
                await asyncio.sleep(.1)
                bridge_state = State(path)
                bridge_state.claim_turn("thread-a", "turn-a")
                bridge_state.clear_streaming("thread-a", "turn-a")
                bridge_state.close()

            pending = asyncio.create_task(bridge_finishes())
            with patch.object(qq_notify, "State", lambda: State(path)), \
                 patch.object(qq_notify, "send_qq_message", sender), \
                 patch.object(qq_notify, "read_task_metadata", return_value=("Task", None)), \
                 patch.object(qq_notify.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(payload))), \
                 redirect_stdout(io.StringIO()):
                await qq_notify.main()
            await pending
            sender.assert_not_awaited()

    async def test_one_notification_per_turn_and_quote_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            payload = json.dumps({
                "hook_event_name": "Stop", "session_id": "thread-a",
                "turn_id": "turn-a", "last_assistant_message": "Done",
            }).encode()
            sender = AsyncMock(return_value={"id": "qq-a", "ext_info": {"ref_idx": "ref-a"}})
            output = io.StringIO()
            with patch.object(qq_notify, "State", lambda: State(path)), \
                 patch.object(qq_notify, "send_qq_message", sender), \
                 patch.object(qq_notify, "read_task_metadata", return_value=("Task", None)), \
                 redirect_stdout(output):
                for _ in range(2):
                    with patch.object(qq_notify.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(payload))):
                        await qq_notify.main()
            self.assertEqual(sender.await_count, 1)
            self.assertEqual(output.getvalue().splitlines(), ["{}", "{}"])
            state = State(path)
            try:
                self.assertEqual(state.thread_for_ref("ref-a"), "thread-a")
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
