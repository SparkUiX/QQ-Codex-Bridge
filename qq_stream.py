"""One QQ C2C replace-mode stream anchored to an inbound owner message."""

import time


class QQStream:
    def __init__(self, api, owner, msg_id):
        self.api = api
        self.owner = owner
        self.msg_id = msg_id
        self.msg_seq = api.next_msg_seq()
        self.index = 0
        self.stream_msg_id = None
        self.text = ""
        self.last_sent = ""
        self.last_at = 0.0
        self.failed = False

    async def update(self, delta):
        if self.failed or not delta:
            return
        self.text += delta
        if len(self.text) > 1500:
            self.text = self.text[:1460] + "\n\n……（后续内容请在 Codex 中查看）"
        if time.monotonic() - self.last_at >= 0.5 and self.text != self.last_sent:
            await self._send(1)

    async def complete(self):
        if self.failed or not self.text:
            return None
        return await self._send(10)

    async def _send(self, state):
        body = {
            "input_mode": "replace", "input_state": state,
            "content_type": "markdown", "content_raw": self.text,
            "event_id": self.msg_id, "msg_id": self.msg_id,
            "msg_seq": self.msg_seq, "index": self.index,
        }
        if self.stream_msg_id:
            body["stream_msg_id"] = self.stream_msg_id
        try:
            result = await self.api.request("POST", f"/v2/users/{self.owner}/stream_messages", body)
        except Exception:
            self.failed = True
            raise
        self.index += 1
        self.stream_msg_id = self.stream_msg_id or result.get("id")
        self.last_sent = self.text
        self.last_at = time.monotonic()
        return result
