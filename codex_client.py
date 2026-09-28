"""JSONL client for the local Codex app-server protocol."""

import asyncio
import json
import logging
import os
import shutil
import sqlite3
from pathlib import Path

import aiohttp

LOG = logging.getLogger(__name__)


class CodexError(RuntimeError):
    pass


class CodexClient:
    def __init__(self, *, shared: bool = False, approval_handler=None, event_handler=None):
        self.shared = shared
        self.approval_handler = approval_handler
        self.event_handler = event_handler
        self.proc = None
        self.pending = {}
        self.next_id = 1
        self.reader_task = None
        self.stderr_task = None
        self.event_task = None
        self.event_queue = asyncio.Queue()
        self.active = {}
        self.loaded_threads = set()
        self.session = None
        self.ws = None

    @staticmethod
    def shared_socket():
        home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        return home / "app-server-control" / "app-server-control.sock"

    @property
    def connected(self):
        if self.ws is not None:
            return not self.ws.closed and self.reader_task is not None and not self.reader_task.done()
        return self.proc is not None and self.proc.returncode is None

    async def connect(self):
        url = os.environ.get("CODEXQQ_APP_SERVER_URL") if self.shared else None
        if url:
            if not url.startswith("ws://127.0.0.1:") and not url.startswith("ws://localhost:"):
                raise CodexError("共享 App Server 仅允许本机 WebSocket 地址")
            self.session = aiohttp.ClientSession()
            try:
                self.ws = await self.session.ws_connect(url, heartbeat=30, max_msg_size=8 * 1024 * 1024, timeout=15)
                self.reader_task = asyncio.create_task(self._read_ws())
                self.event_task = asyncio.create_task(self._events())
                await self.request("initialize", {"clientInfo": {"name": "codex_qq_bridge", "title": "Codex QQ Bridge", "version": "0.1.0"}}, timeout=15)
                await self._write({"method": "initialized", "params": {}})
                return self
            except Exception:
                await self.close()
                raise
        if self.shared and not self.shared_socket().exists():
            raise CodexError("桌面端未开放共享 App Server 控制套接字；当前桌面端可能使用私有 stdio 连接")
        executable = shutil.which("codex")
        if not executable and os.name == "nt":
            candidates = list((Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin").glob("*/codex.exe"))
            if candidates:
                executable = str(max(candidates, key=lambda path: path.stat().st_mtime))
        if not executable:
            raise CodexError("找不到 Codex CLI，可检查登录自启环境中的 Codex 安装路径")
        command = [executable, "app-server", "proxy" if self.shared else "--stdio"]
        self.proc = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=8 * 1024 * 1024,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        self.reader_task = asyncio.create_task(self._read())
        self.event_task = asyncio.create_task(self._events())
        self.stderr_task = asyncio.create_task(self._stderr())
        await self.request("initialize", {"clientInfo": {"name": "codex_qq_bridge", "title": "Codex QQ Bridge", "version": "0.1.0"}}, timeout=15)
        await self._write({"method": "initialized", "params": {}})
        return self

    async def _write(self, message):
        if self.ws:
            if self.ws.closed:
                raise CodexError("共享 App Server 连接断开")
            await self.ws.send_str(json.dumps(message, ensure_ascii=False))
            return
        if not self.proc or not self.proc.stdin or self.proc.stdin.is_closing():
            raise CodexError("Codex App Server 未连接")
        self.proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def request(self, method, params=None, *, timeout=30):
        ident = self.next_id
        self.next_id += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self._write({"id": ident, "method": method, "params": params or {}})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(ident, None)

    async def _read(self):
        try:
            while line := await self.proc.stdout.readline():
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    LOG.warning("Ignoring non-JSON App Server output")
                    continue
                self._handle_message(msg)
        finally:
            self._fail_pending()

    async def _read_ws(self):
        try:
            async for frame in self.ws:
                if frame.type != aiohttp.WSMsgType.TEXT:
                    break
                try:
                    self._handle_message(json.loads(frame.data))
                except json.JSONDecodeError:
                    LOG.warning("Ignoring non-JSON WebSocket App Server output")
        finally:
            self._fail_pending()

    def _fail_pending(self):
        for future in self.pending.values():
            if not future.done():
                future.set_exception(CodexError("Codex App Server 连接断开"))

    def _handle_message(self, msg):
        ident = msg.get("id")
        if ident in self.pending:
            future = self.pending[ident]
            if not future.done():
                if "error" in msg:
                    future.set_exception(CodexError(str(msg["error"])))
                else:
                    future.set_result(msg.get("result", {}))
        elif ident is not None and "method" in msg:
            asyncio.create_task(self._server_request(msg))
        elif "method" in msg:
            params = msg.get("params") or {}
            if msg["method"] == "turn/started":
                self.active[params.get("threadId")] = (params.get("turn") or {}).get("id")
            elif msg["method"] == "turn/completed":
                self.active.pop(params.get("threadId"), None)
            if self.event_handler:
                self.event_queue.put_nowait((msg["method"], params))

    async def _events(self):
        while True:
            method, params = await self.event_queue.get()
            try:
                if self.event_handler:
                    await self.event_handler(method, params)
            except Exception:
                LOG.exception("App Server event handler failed: %s", method)
            finally:
                self.event_queue.task_done()

    async def _server_request(self, msg):
        result = None
        if self.approval_handler:
            try:
                params = dict(msg.get("params") or {})
                params["_request_id"] = msg["id"]
                result = await self.approval_handler(msg["method"], params)
            except Exception:
                LOG.exception("Approval handler failed")
        if result is None:
            method = msg["method"]
            if method == "item/permissions/requestApproval":
                result = {"permissions": {}}
            elif method == "tool/requestUserInput":
                result = {"answers": {}}
            elif method == "mcpServer/elicitation/request":
                result = {"action": "decline", "content": None}
            else:
                result = {"decision": "decline"}
        await self._write({"id": msg["id"], "result": result})

    async def _stderr(self):
        while line := await self.proc.stderr.readline():
            LOG.warning("App Server: %s", line.decode("utf-8", "replace").rstrip()[:500])

    async def threads(self):
        threads = []
        cursor = None
        for _ in range(100):
            result = await self.request("thread/list", {
                "cursor": cursor, "limit": 50,
                "sortKey": "recency_at", "sortDirection": "desc",
                "sourceKinds": ["cli", "vscode", "exec", "appServer", "unknown"],
            })
            threads.extend(result.get("data", []))
            next_cursor = result.get("nextCursor")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return threads

    async def read_thread(self, thread_id):
        return (await self.request("thread/read", {"threadId": thread_id}))["thread"]

    async def models(self):
        models, cursor = [], None
        while True:
            result = await self.request("model/list", {"limit": 100, "cursor": cursor, "includeHidden": False})
            models.extend(result.get("data") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                return models

    async def rate_limits(self):
        try:
            return await self.request("account/rateLimits/read")
        except CodexError:
            # The long-lived app-server can retain a stale Windows proxy route.
            # A fresh read-only connection uses the current proxy configuration.
            fresh = CodexClient(shared=self.shared)
            try:
                await fresh.connect()
                return await fresh.request("account/rateLimits/read")
            finally:
                await fresh.close()

    async def start_thread(self, cwd, model=None):
        params = {"cwd": cwd, "serviceName": "codex_qq_bridge"}
        if model:
            params["model"] = model
        thread = (await self.request("thread/start", params))["thread"]
        self.loaded_threads.add(thread["id"])
        return thread

    async def fork_thread(self, thread_id):
        thread = (await self.request("thread/fork", {"threadId": thread_id}, timeout=60))["thread"]
        self.loaded_threads.add(thread["id"])
        return thread

    async def send(self, thread_id, text, *, allow_steer=False, effort=None):
        if allow_steer and self.active.get(thread_id):
            turn_id = self.active[thread_id]
            await self.request("turn/steer", {
                "threadId": thread_id, "expectedTurnId": turn_id,
                "input": [{"type": "text", "text": text}],
            })
            return turn_id, True
        if self._other_process_has_active_turn(thread_id):
            raise CodexError("桌面端任务仍在运行；独立 App Server 无法安全接管，请等待任务结束")
        # A new thread has no rollout to resume until its first turn starts.
        if thread_id not in self.loaded_threads:
            await self.request("thread/resume", {"threadId": thread_id})
            self.loaded_threads.add(thread_id)
        thread = await self.read_thread(thread_id)
        status = thread.get("status") or {}
        if status.get("type") == "active":
            raise CodexError("任务正在运行；当前连接无法安全接管，请稍后重试")
        params = {
            "threadId": thread_id, "input": [{"type": "text", "text": text}],
        }
        if effort:
            params["effort"] = effort
        result = await self.request("turn/start", params)
        turn_id = result["turn"]["id"]
        self.active[thread_id] = turn_id
        return turn_id, False

    @staticmethod
    def _other_process_has_active_turn(thread_id):
        home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        db_path = home / "thread_history_1.sqlite"
        try:
            with sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=2) as db:
                row = db.execute(
                    "SELECT status FROM thread_turns WHERE thread_id=? ORDER BY started_at DESC LIMIT 1",
                    (thread_id,),
                ).fetchone()
            return bool(row and row[0] == "inProgress")
        except sqlite3.Error:
            # If the runtime state cannot be checked, fail closed.
            return True

    @staticmethod
    def has_persisted_thread(thread_id):
        """Fail closed when the local index cannot establish that a thread is empty."""
        home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        db_path = home / "state_5.sqlite"
        try:
            with sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=2) as db:
                row = db.execute("SELECT 1 FROM threads WHERE id=?", (thread_id,)).fetchone()
            return bool(row)
        except sqlite3.Error:
            return None

    async def close(self):
        if self.ws:
            await self.ws.close()
            self.ws = None
        if self.session:
            await self.session.close()
            self.session = None
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        for task in (self.reader_task, self.stderr_task, self.event_task):
            if task:
                task.cancel()
