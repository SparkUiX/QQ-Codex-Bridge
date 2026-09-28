"""One local App Server for Codex Desktop and the QQ bridge.

The proxy multiplexes JSON-RPC ids and broadcasts turn notifications. Approval
requests go only to the client that started the turn; a missing owner is an
error, never an implicit approval.
"""

import argparse
import asyncio
import json
import logging
import os
import shutil
import socket
import struct
import subprocess
import sys
from pathlib import Path

import aiohttp
from aiohttp import web

LOG = logging.getLogger("codexqq.shared")
HOST = "127.0.0.1"
PUBLIC_PORT = 17653
UPSTREAM_PORT = 17654
SOCKS_PORT = 1080


class SharedProxy:
    # Architecture inspiration: https://github.com/xiaoiecc/qq-codex-bridge
    # This Python proxy was implemented independently; no source was copied.
    def __init__(self, public_port=PUBLIC_PORT, upstream_port=UPSTREAM_PORT, socks_port=SOCKS_PORT):
        self.public_port = public_port
        self.upstream_port = upstream_port
        self.socks_port = socks_port
        self.clients = set()
        self.info = {}
        self.pending = {}
        self.server_requests = {}
        self.thread_owners = {}
        self.turn_owners = {}
        self.pending_turns = {}
        self.known_threads = {}
        self.active_turns = {}
        self.next_id = 1
        self.upstream = None
        self.session = None
        self.proc = None
        self.init_result = None
        self.ready = asyncio.Event()
        self.app = web.Application()
        self.app.router.add_get("/", self.accept)
        self.app.router.add_get("/healthz", self.health)

    async def start(self):
        executable = shutil.which("codex")
        if not executable:
            raise RuntimeError("找不到 Codex CLI")
        url = f"ws://{HOST}:{self.upstream_port}"
        self.proc = await asyncio.create_subprocess_exec(
            executable, "app-server", "--listen", url,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        asyncio.create_task(self._stderr())
        self.session = aiohttp.ClientSession()
        for _ in range(100):
            if self.proc.returncode is not None:
                raise RuntimeError("共享 App Server 启动失败")
            try:
                self.upstream = await self.session.ws_connect(url, max_msg_size=8 * 1024 * 1024)
                break
            except (OSError, aiohttp.ClientError):
                await asyncio.sleep(.1)
        if not self.upstream:
            raise RuntimeError("共享 App Server 未就绪")
        await self.upstream.send_json({"id": 0, "method": "initialize", "params": {
            "clientInfo": {"name": "codex_qq_shared_proxy", "title": "Codex QQ Shared Proxy", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True},
        }})
        frame = await asyncio.wait_for(self.upstream.receive(), 15)
        response = json.loads(frame.data)
        if "error" in response:
            raise RuntimeError(f"App Server initialize failed: {response['error']}")
        self.init_result = response.get("result", {})
        await self.upstream.send_json({"method": "initialized", "params": {}})
        self.reader = asyncio.create_task(self._read_upstream())
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, HOST, self.public_port)
        await self.site.start()
        self.socks = await asyncio.start_server(self._socks_client, HOST, self.socks_port)
        self.ready.set()
        LOG.info("Shared App Server ready: ws://%s:%s", HOST, self.public_port)

    async def _stderr(self):
        while line := await self.proc.stderr.readline():
            LOG.warning("App Server: %s", line.decode("utf-8", "replace").strip()[:500])

    async def health(self, _request):
        return web.json_response({"upstream": self.upstream is not None and not self.upstream.closed,
                                  "clients": [self.info.get(c, {}).get("clientInfo", {}).get("title", "") for c in self.clients]})

    async def accept(self, request):
        ws = web.WebSocketResponse(max_msg_size=8 * 1024 * 1024, heartbeat=30)
        await ws.prepare(request)
        self.clients.add(ws)
        try:
            async for frame in ws:
                if frame.type != aiohttp.WSMsgType.TEXT:
                    break
                try:
                    await self._from_client(ws, json.loads(frame.data))
                except (ValueError, TypeError):
                    LOG.warning("Ignoring invalid downstream message")
        finally:
            self.clients.discard(ws)
            self.info.pop(ws, None)
            self.thread_owners = {k: v for k, v in self.thread_owners.items() if v is not ws}
            self.turn_owners = {k: v for k, v in self.turn_owners.items() if v is not ws}
            for ident, (target, _) in list(self.server_requests.items()):
                if target is ws:
                    self.server_requests.pop(ident)
                    await self._upsend({"id": ident, "error": {"code": -32000, "message": "approval client disconnected"}})
        return ws

    async def _from_client(self, ws, msg):
        if not isinstance(msg, dict):
            return
        method = msg.get("method")
        ident = msg.get("id")
        if method == "initialize" and ident is not None:
            self.info[ws] = msg.get("params") or {}
            await ws.send_json({"id": ident, "result": self.init_result})
            for thread in self.known_threads.values():
                await ws.send_json({"method": "thread/started", "params": {"thread": thread}})
            for thread_id, turn in self.active_turns.items():
                await ws.send_json({"method": "turn/started", "params": {"threadId": thread_id, "turn": turn}})
            return
        if method == "initialized":
            return
        if ident is not None and not method:
            key = next((key for key, value in self.server_requests.items() if value == (ws, ident)), None)
            if key is not None:
                self.server_requests.pop(key)
                await self._upsend({**msg, "id": key})
            return
        if ident is None or not method:
            if method:
                await self._upsend(msg)
            return
        if method == "thread/unsubscribe":
            await ws.send_json({"id": ident, "result": {}})
            return
        params = msg.get("params") or {}
        thread_id = params.get("threadId")
        if method == "turn/start" and thread_id:
            self.pending_turns[thread_id] = ws
        if method == "turn/steer" and thread_id:
            self.thread_owners[thread_id] = ws
        upstream_id = self.next_id
        self.next_id += 1
        self.pending[upstream_id] = (ws, ident, method, params)
        await self._upsend({**msg, "id": upstream_id})

    async def _upsend(self, msg):
        if not self.upstream or self.upstream.closed:
            raise RuntimeError("共享 App Server 连接断开")
        await self.upstream.send_str(json.dumps(msg, ensure_ascii=False))

    async def _read_upstream(self):
        async for frame in self.upstream:
            if frame.type != aiohttp.WSMsgType.TEXT:
                break
            try:
                msg = json.loads(frame.data)
                await self._from_upstream(msg)
            except Exception:
                LOG.exception("Failed routing App Server message")
        LOG.error("Shared App Server disconnected")

    async def _from_upstream(self, msg):
        ident = msg.get("id")
        method = msg.get("method")
        if ident in self.pending and not method:
            ws, original, request_method, params = self.pending.pop(ident)
            result = msg.get("result") or {}
            thread_id = (result.get("thread") or {}).get("id") or result.get("threadId") or params.get("threadId")
            if "error" not in msg and request_method in ("thread/start", "thread/resume", "thread/fork") and thread_id:
                self.thread_owners[thread_id] = ws
                if result.get("thread"):
                    self.known_threads[thread_id] = result["thread"]
                    await self._broadcast({"method": "thread/started", "params": {"thread": result["thread"]}}, exclude=ws)
            if request_method == "turn/start":
                self.pending_turns.pop(params.get("threadId"), None)
                if "error" not in msg and thread_id:
                    turn = result.get("turn") or {}
                    self.thread_owners[thread_id] = ws
                    if turn.get("id"):
                        self.turn_owners[turn["id"]] = ws
                        self.active_turns[thread_id] = turn
                        await self._broadcast({"method": "turn/started", "params": {"threadId": thread_id, "turn": turn}}, exclude=ws)
            if not ws.closed:
                await ws.send_json({**msg, "id": original})
            return
        if method and ident is not None:
            params = msg.get("params") or {}
            turn_id = params.get("turnId")
            thread_id = params.get("threadId")
            target = self.turn_owners.get(turn_id) or self.pending_turns.get(thread_id) or self.thread_owners.get(thread_id)
            if (not target or target.closed) and method in ("account/chatgptAuthTokens/refresh", "attestation/generate"):
                target = next((ws for ws in self.clients if "desktop" in str(self.info.get(ws, {}).get("clientInfo", {})).lower() and not ws.closed), None)
            if not target:
                await self._upsend({"id": ident, "error": {"code": -32000, "message": "No client available for server request"}})
                return
            downstream_id = f"server:{ident}"
            self.server_requests[ident] = (target, downstream_id)
            await target.send_json({**msg, "id": downstream_id})
            return
        if method:
            params = msg.get("params") or {}
            if method == "turn/started":
                turn_id = (params.get("turn") or {}).get("id")
                if params.get("threadId") and turn_id:
                    self.active_turns[params["threadId"]] = params["turn"]
                owner = self.pending_turns.get(params.get("threadId")) or self.thread_owners.get(params.get("threadId"))
                if owner and turn_id:
                    self.turn_owners[turn_id] = owner
            elif method == "turn/completed":
                turn_id = (params.get("turn") or {}).get("id")
                self.turn_owners.pop(turn_id, None)
                self.active_turns.pop(params.get("threadId"), None)
            elif method == "thread/started":
                thread = params.get("thread") or {}
                if thread.get("id"):
                    self.known_threads[thread["id"]] = thread
            await self._broadcast(msg)

    async def _broadcast(self, msg, exclude=None):
        for ws in list(self.clients):
            if ws is exclude or ws.closed:
                continue
            excluded = (self.info.get(ws, {}).get("capabilities") or {}).get("optOutNotificationMethods") or []
            if msg.get("method") not in excluded:
                await ws.send_json(msg)

    async def _socks_client(self, reader, writer):
        remote = None
        try:
            version, count = await reader.readexactly(2)
            methods = await reader.readexactly(count)
            if version != 5 or 0 not in methods:
                return
            writer.write(b"\x05\x00")
            await writer.drain()
            version, command, _, address_type = await reader.readexactly(4)
            if version != 5 or command != 1:
                return
            if address_type == 1:
                host = socket.inet_ntop(socket.AF_INET, await reader.readexactly(4))
            elif address_type == 4:
                host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
            elif address_type == 3:
                host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
            else:
                return
            port = struct.unpack("!H", await reader.readexactly(2))[0]
            remote_reader, remote = await asyncio.open_connection(host, port)
            writer.write(b"\x05\x00\x00\x01" + b"\x00" * 6)
            await writer.drain()
            async def pipe(source, target):
                try:
                    while data := await source.read(65536):
                        target.write(data)
                        await target.drain()
                finally:
                    target.close()
            await asyncio.gather(pipe(reader, remote), pipe(remote_reader, writer))
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            if remote:
                remote.close()

    async def close(self):
        if hasattr(self, "socks"):
            self.socks.close()
            await self.socks.wait_closed()
        if hasattr(self, "runner"):
            await self.runner.cleanup()
        if self.upstream:
            await self.upstream.close()
        if self.session:
            await self.session.close()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=PUBLIC_PORT)
    parser.add_argument("--upstream-port", type=int, default=UPSTREAM_PORT)
    parser.add_argument("--socks-port", type=int, default=SOCKS_PORT)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    proxy = SharedProxy(args.port, args.upstream_port, args.socks_port)
    try:
        await proxy.start()
        await proxy.reader
    finally:
        await proxy.close()


if __name__ == "__main__":
    asyncio.run(main())
