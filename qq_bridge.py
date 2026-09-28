"""QQ private-chat ingress for local Codex tasks. Run with: py -3 qq_bridge.py."""

import asyncio
from collections import defaultdict
import json
import logging
import os
import re
import secrets
import time

import httpx
from qqbot_agent_sdk import EventParser, QQApiClient, QQWebSocket, WSCallbacks, WSSessionStore
from qqbot_agent_sdk import parse_interaction_event
from qqbot_agent_sdk.dto import (InlineKeyboard, KeyboardContent, KeyboardRow,
    KeyboardButton, KeyboardButtonAction, KeyboardButtonRenderData,
    MessageToCreate, MarkdownContent, QQMessageType)
import qqbot_agent_sdk.websocket as qq_websocket_module

from codex_client import CodexClient, CodexError
from codex_listing import overview, project_page
from codex_projects import load_projects
from qq_state import DATA_DIR, State
from qq_stream import QQStream

LOG = logging.getLogger("codexqq")
APPROVAL_TIMEOUT = 300
HELP = "指令：/projects 查看桌面项目；/list 按项目查看最近任务；/list 项目编号 [页码] 查看更多；/use 任务编号 选择任务；/new 逐步选择项目、模型和思考强度；/status 当前任务；/quota 查看额度；/help 帮助。引用机器人任务消息可直接回复对应任务。审批回复：批准 编号 或 拒绝 编号。"


def config_value(name):
    value = os.environ.get(name)
    if value:
        return value
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                return winreg.QueryValueEx(key, name)[0]
        except FileNotFoundError:
            pass
    return None


def inbound_key(raw):
    ext = (raw.get("message_scene") or {}).get("ext") or []
    idx = next((x[8:] for x in ext if isinstance(x, str) and x.startswith("msg_idx=")), "")
    return f"{raw.get('id', '')}:{idx}"


def referenced_index(raw):
    ext = (raw.get("message_scene") or {}).get("ext") or []
    return next((x[12:] for x in ext if isinstance(x, str) and x.startswith("ref_msg_idx=")), None)


class Bridge:
    def __init__(self, state, api, codex, owner):
        self.state = state
        self.api = api
        self.codex = codex
        self.owner = owner
        self.pending_approvals = {}
        self.approval_context = {}
        self.listed_threads = []
        self.listed_projects = []
        self.owned_turns = set()
        self.turn_streams = {}
        self.stream_origins = {}
        self.pending_stream_origin = {}
        self.stream_locks = defaultdict(asyncio.Lock)
        self.turn_items = defaultdict(dict)
        self.item_deltas = defaultdict(str)
        self.final_answers = {}
        self.progress_items = defaultdict(set)
        self.send_lock = asyncio.Lock()
        self.task_locks = defaultdict(asyncio.Lock)
        self.new_flow = None

    async def reply(self, text, *, reply_to=None, thread_id=None):
        async with self.send_lock:
            result = await self.api.send_text(
                "c2c", self.owner, text[:1700], reply_to=reply_to,
                markdown=True, retries=2,
            )
            if thread_id:
                self.state.bind_outbound(result, thread_id)
            return result

    async def reply_long(self, text, *, reply_to=None, thread_id=None):
        chunk = ""
        for line in text.splitlines():
            if len(line) > 1500:
                if chunk:
                    await self.reply(chunk, reply_to=reply_to, thread_id=thread_id)
                    reply_to, chunk = None, ""
                for offset in range(0, len(line), 1500):
                    part = line[offset:offset + 1500]
                    if offset + 1500 < len(line):
                        await self.reply(part, reply_to=reply_to, thread_id=thread_id)
                        reply_to = None
                    else:
                        chunk = part
                continue
            next_chunk = chunk + ("\n" if chunk else "") + line
            if len(next_chunk) > 1500 and chunk:
                await self.reply(chunk, reply_to=reply_to, thread_id=thread_id)
                reply_to = None
                chunk = line
            else:
                chunk = next_chunk
        if chunk:
            await self.reply(chunk, reply_to=reply_to, thread_id=thread_id)

    async def shared_status(self):
        if not os.environ.get("CODEXQQ_APP_SERVER_URL"):
            return ""
        if not getattr(self.codex, "connected", True):
            return "共享服务：已断开"
        try:
            async with httpx.AsyncClient(timeout=2, trust_env=False) as http:
                response = await http.get("http://127.0.0.1:17653/healthz")
                response.raise_for_status()
                health = response.json()
        except (httpx.HTTPError, ValueError):
            return "共享服务：健康检查失败"
        clients = health.get("clients") or []
        desktop = "已连接" if "Codex Desktop" in clients else "未连接（请从共享启动器打开）"
        return f"共享服务：{'正常' if health.get('upstream') else '上游断开'}；Desktop：{desktop}"

    async def choices(self, title, choices, *, reply_to=None):
        """Send QQ's C2C inline keyboard; callback data is scoped to one flow."""
        lines = []
        for label, data in choices:
            action, index = data.rsplit(":", 2)[-2:]
            if action != "page":
                lines.append(f"{int(index)+1}. {label}")
        fallback = title + "\n" + "\n".join(lines) + "\n按钮未显示时发送 /pick 编号；翻页发送 /page 页码。"
        rows = []
        for start in range(0, len(choices), 3):
            buttons = []
            for label, data in choices[start:start + 3]:
                buttons.append(KeyboardButton(
                    id=data[:64], render_data=KeyboardButtonRenderData(label=label[:12], visited_label=label[:12]),
                    action=KeyboardButtonAction(type=1, data=data),
                ))
            rows.append(KeyboardRow(buttons=buttons))
        msg = MessageToCreate(msg_type=QQMessageType.MARKDOWN,
                              markdown=MarkdownContent(content=fallback),
                              msg_id=reply_to or "", msg_seq=self.api.next_msg_seq())
        keyboard = InlineKeyboard(content=KeyboardContent(rows=rows)).to_dict()
        for row in keyboard["content"]["rows"]:
            for button in row["buttons"]:
                button["action"]["unsupport_tips"] = "当前 QQ 客户端不支持此按钮，请发送 /new 重试"
                button["action"].pop("click_limit", None)
        body = msg.to_dict()
        body["keyboard"] = keyboard
        async with self.send_lock:
            try:
                return await self.api.request("POST", f"/v2/users/{self.owner}/messages", body)
            except Exception:
                LOG.exception("QQ rejected interactive keyboard; using text choices")
        return await self.reply(fallback, reply_to=reply_to)

    async def show_new_step(self, *, reply_to=None):
        flow = self.new_flow
        nonce = flow["nonce"]
        if flow["step"] == "project":
            projects = flow["projects"]
            page = flow["page"]
            start = page * 12
            options = [(p.name, f"new:{nonce}:project:{i}") for i, p in enumerate(projects[start:start + 12], start)]
            if page:
                options.append(("上一页", f"new:{nonce}:page:{page-1}"))
            if start + 12 < len(projects):
                options.append(("下一页", f"new:{nonce}:page:{page+1}"))
            await self.choices(f"选择项目（第 {page+1} 页）：", options, reply_to=reply_to)
        elif flow["step"] == "model":
            models = flow["models"]
            page = flow["page"]
            start = page * 12
            options = [(m.get("displayName") or m.get("model") or m["id"], f"new:{nonce}:model:{i}")
                       for i, m in enumerate(models[start:start + 12], start)]
            if page:
                options.append(("上一页", f"new:{nonce}:page:{page-1}"))
            if start + 12 < len(models):
                options.append(("下一页", f"new:{nonce}:page:{page+1}"))
            await self.choices(f"项目：{flow['project'].name}\n选择模型（第 {page+1} 页）：", options, reply_to=reply_to)
        else:
            model = flow["model"]
            efforts = model.get("supportedReasoningEfforts") or []
            options = [(e["reasoningEffort"], f"new:{nonce}:effort:{i}") for i, e in enumerate(efforts)]
            await self.choices(f"模型：{model.get('displayName') or model['model']}\n选择思考强度：", options, reply_to=reply_to)

    async def on_interaction(self, event_type, raw):
        if event_type != "INTERACTION_CREATE":
            return
        event = parse_interaction_event(raw)
        if event.user_openid != self.owner or not event.is_c2c:
            return
        LOG.info("QQ C2C button interaction received")
        try:
            await self.api.acknowledge_interaction(event.id)
            LOG.info("QQ C2C button interaction acknowledged")
        except Exception:
            LOG.exception("QQ C2C button interaction acknowledgement failed")
            return
        data = event.data.resolved.button_data
        parts = data.split(":")
        flow = self.new_flow
        if len(parts) != 4 or parts[0] != "new" or not flow or parts[1] != flow["nonce"]:
            return
        _, _, action, raw_index = parts
        if not raw_index.isdecimal():
            return
        try:
            await self.select_new_option(action, int(raw_index), reply_to=flow["origin_msg_id"])
        except Exception:
            LOG.exception("QQ C2C button action failed")
            try:
                await self.reply("按钮操作失败，请使用消息中的 /pick 编号继续选择。", reply_to=flow["origin_msg_id"])
            except Exception:
                LOG.exception("Could not report QQ button action failure")

    async def select_new_option(self, action, index, *, reply_to=None):
        flow = self.new_flow
        if not flow:
            return
        if action == "page":
            flow["page"] = index
            await self.show_new_step(reply_to=reply_to)
        elif action == "project" and flow["step"] == "project" and index < len(flow["projects"]):
            flow["project"] = flow["projects"][index]
            flow["models"] = await self.codex.models()
            if not flow["models"]:
                await self.reply("当前账号没有可选模型。", reply_to=reply_to)
                return
            flow["step"], flow["page"] = "model", 0
            await self.show_new_step(reply_to=reply_to)
        elif action == "model" and flow["step"] == "model" and index < len(flow["models"]):
            flow["model"] = flow["models"][index]
            flow["step"] = "effort"
            await self.show_new_step(reply_to=reply_to)
        elif action == "effort" and flow["step"] == "effort":
            efforts = flow["model"].get("supportedReasoningEfforts") or []
            if index >= len(efforts):
                return
            flow["step"] = "creating"
            project = load_projects().by_id().get(flow["project"].id)
            if not project or not project.path.is_dir():
                self.new_flow = None
                await self.reply("项目已不可用，请重新发送 /new。", reply_to=reply_to)
                return
            effort = efforts[index]["reasoningEffort"]
            try:
                thread = await self.codex.start_thread(str(project.path), flow["model"].get("model") or flow["model"]["id"])
            except Exception:
                flow["step"] = "effort"
                raise
            self.state.bind_project(thread["id"], project.id)
            self.state.set_effort(thread["id"], effort)
            self.state.put("selected_thread", thread["id"])
            self.new_flow = None
            await self.reply(f"已创建任务：{thread['id']}\n{project.name} · {flow['model'].get('displayName') or flow['model']['id']} · {effort}\n现在可直接发送消息。", reply_to=reply_to, thread_id=thread["id"])

    async def on_message(self, event_type, raw):
        if event_type != "C2C_MESSAGE_CREATE":
            return
        event = EventParser.parse(event_type, raw)
        if not event or event.user_id != self.owner or (raw.get("author") or {}).get("bot"):
            return
        if event.message_type not in (0, 103) or not event.content:
            return
        key = inbound_key(raw)
        if not event.message_id or not self.state.claim(key):
            return
        try:
            await self.process(event.content.strip(), raw, event.message_id)
            self.state.mark_inbound(key, "done")
        except Exception as exc:
            LOG.exception("Inbound message failed: %s", key)
            self.state.mark_inbound(key, "failed")
            try:
                await self.reply(f"处理失败：{str(exc)[:300]}。请重新发送消息。", reply_to=event.message_id)
            except Exception:
                LOG.exception("Could not report inbound failure")

    async def process(self, content, raw, message_id):
        approval = re.fullmatch(r"(批准|拒绝)\s+([A-Z0-9]{6})", content)
        if approval:
            code = approval.group(2)
            pending = self.pending_approvals.pop(code, None)
            if not pending or pending.done():
                await self.reply("审批编号无效或已过期。", reply_to=message_id)
            else:
                pending.set_result(approval.group(1) == "批准")
                await self.reply("已提交审批决定。", reply_to=message_id)
            return

        # QQ normalizes panel item names such as "/list" to "list" in its
        # stored panel. Accept either wire shape when the owner submits one.
        if content in {"list", "projects", "status", "quota", "help", "use", "new"} or re.fullmatch(r"(?:list|use|new)\s+\d+(?:\s+\d+)?", content):
            content = "/" + content
        if content == "/use":
            await self.reply("请补上编号。" + HELP, reply_to=message_id)
            return

        if content == "/new":
            projects = load_projects().projects
            if not projects:
                await self.reply("没有可用的本地项目。", reply_to=message_id)
                return
            self.new_flow = {"nonce": secrets.token_hex(4), "step": "project", "page": 0,
                             "projects": projects, "origin_msg_id": message_id}
            await self.show_new_step(reply_to=message_id)
            return
        pick = re.fullmatch(r"/pick\s+(\d+)", content)
        if pick:
            flow = self.new_flow
            if not flow or flow["step"] not in ("project", "model", "effort"):
                await self.reply("没有待选择的新建任务步骤，请发送 /new。", reply_to=message_id)
                return
            index = int(pick.group(1)) - 1
            start = flow["page"] * 12 if flow["step"] != "effort" else 0
            count = 12 if flow["step"] != "effort" else len(flow["model"].get("supportedReasoningEfforts") or [])
            if not start <= index < start + count:
                await self.reply("编号不在当前页，请按消息中的编号选择。", reply_to=message_id)
                return
            flow["origin_msg_id"] = message_id
            await self.select_new_option(flow["step"], index, reply_to=message_id)
            return
        page = re.fullmatch(r"/page\s+(\d+)", content)
        if page:
            flow = self.new_flow
            if not flow or flow["step"] not in ("project", "model"):
                await self.reply("当前步骤无法翻页，请发送 /new。", reply_to=message_id)
                return
            count = len(flow["projects"] if flow["step"] == "project" else flow["models"])
            index = int(page.group(1)) - 1
            if not 0 <= index < (count + 11) // 12:
                await self.reply("页码无效。", reply_to=message_id)
                return
            flow["origin_msg_id"] = message_id
            await self.select_new_option("page", index, reply_to=message_id)
            return

        if content == "/help":
            await self.reply(HELP, reply_to=message_id)
            return
        list_match = re.fullmatch(r"/list(?:\s+(\d+)(?:\s+(\d+))?)?", content)
        if list_match:
            snapshot = load_projects()
            threads = await self.codex.threads()
            if not threads:
                await self.reply("没有可用任务。先用 /projects 选项目，再用 /new 编号 新建。", reply_to=message_id)
                return
            project_for_thread = lambda thread: snapshot.project_for_thread(
                thread, self.state.project_for_thread(thread["id"])
            )
            if list_match.group(1) is None:
                message, self.listed_threads = overview(snapshot, threads, project_for_thread)
            else:
                message, self.listed_threads = project_page(
                    snapshot, threads, project_for_thread,
                    int(list_match.group(1)), int(list_match.group(2) or 1),
                )
            await self.reply_long(message, reply_to=message_id)
            return
        if content.startswith("/use "):
            index = self._number(content, "/use ", self.listed_threads)
            thread = self.listed_threads[index]
            self.state.put("selected_thread", thread["id"])
            await self.reply(f"已选择：{(thread.get('name') or thread.get('preview') or thread['id'])[:80]}", reply_to=message_id, thread_id=thread["id"])
            return
        if content == "/projects":
            self.listed_projects = load_projects().projects
            lines = [f"{i}. {p.name}" for i, p in enumerate(self.listed_projects, 1)]
            await self.reply("Codex 项目：\n" + "\n".join(lines or ["无本地项目"]) + "\n/new 编号 新建任务；/list 编号 查看项目任务。", reply_to=message_id)
            return
        if content == "/quota":
            from qq_quota import format_quota
            try:
                quota = format_quota(await self.codex.rate_limits())
            except CodexError:
                LOG.exception("Could not fetch Codex rate limits")
                quota = "暂时无法读取 Codex 额度。请稍后重试 /quota；QQ 指令本身已正常接收。"
            await self.reply(quota, reply_to=message_id)
            return
        if content.startswith("/new "):
            if not self.listed_projects:
                self.listed_projects = load_projects().projects
            index = self._number(content, "/new ", self.listed_projects)
            project = self.listed_projects[index]
            latest = {p.id: p for p in load_projects().projects}.get(project.id)
            if not latest or not latest.path.is_dir():
                raise ValueError("项目已不存在，请重新执行 /projects")
            models = await self.codex.models()
            if not models:
                await self.reply("当前账号没有可选模型。", reply_to=message_id)
                return
            self.new_flow = {"nonce": secrets.token_hex(4), "step": "model", "page": 0,
                             "project": latest, "models": models, "origin_msg_id": message_id}
            await self.show_new_step(reply_to=message_id)
            return
        if content == "/status":
            transport = await self.shared_status()
            if transport == "共享服务：已断开":
                await self.reply(transport, reply_to=message_id)
                return
            selected = self.state.get("selected_thread")
            if not selected:
                await self.reply("尚未选择任务。使用 /list。" + (f"\n{transport}" if transport else ""), reply_to=message_id)
            else:
                selected = self.state.resolve_thread(selected)
                thread = await self.codex.read_thread(selected)
                status = (thread.get("status") or {}).get("type", "未知")
                detail = "（当前服务尚未加载；其他进程仍可能持有写入权）" if status == "notLoaded" else ""
                await self.reply(f"当前任务：{thread.get('name') or thread.get('preview') or selected}\n状态：{status}{detail}" + (f"\n{transport}" if transport else ""), reply_to=message_id, thread_id=selected)
            return
        if content.startswith("/"):
            await self.reply("未知指令。" + HELP, reply_to=message_id)
            return

        ref = referenced_index(raw)
        thread_id = self.state.thread_for_ref(ref) if ref else None
        thread_id = thread_id or self.state.get("selected_thread")
        if not thread_id:
            await self.reply("还没有选定任务。使用 /list 查看，再发送 /use 编号。", reply_to=message_id)
            return
        thread_id = self.state.resolve_thread(thread_id)
        replaced = None
        forked = False
        original_thread_id = thread_id
        async with self.task_locks[thread_id]:
            # An earlier QQ message may have redirected this task while we waited.
            thread_id = self.state.resolve_thread(thread_id)
            self.pending_stream_origin[thread_id] = message_id
            try:
                effort = self.state.effort_for_thread(thread_id)
                turn_id, steered = await self.codex.send(thread_id, content, allow_steer=True, effort=effort) if effort else await self.codex.send(thread_id, content, allow_steer=True)
            except CodexError as exc:
                if "already has an active writer" in str(exc):
                    source_id = thread_id
                    fork = await self.codex.fork_thread(source_id)
                    thread_id = fork["id"]
                    self.state.replace_empty_thread(source_id, thread_id,
                                                    self.state.project_for_thread(source_id))
                    self.pending_stream_origin[thread_id] = message_id
                    effort = self.state.effort_for_thread(thread_id)
                    turn_id, steered = await self.codex.send(
                        thread_id, content, allow_steer=True, effort=effort,
                    )
                    replaced, forked = source_id, True
                else:
                    project_id = self.state.project_for_thread(thread_id)
                    if ("no rollout found for thread id" not in str(exc)
                            or not project_id
                            or self.codex.has_persisted_thread(thread_id) is not False):
                        raise
                    project = load_projects().by_id().get(project_id)
                    if not project or not project.path.is_dir():
                        raise ValueError("原任务尚未初始化，关联项目已不可用。请重新执行 /projects 和 /new。") from exc
                    replacement = await self.codex.start_thread(str(project.path))
                    new_id = replacement["id"]
                    LOG.info("Replacing empty thread %s with %s", thread_id, new_id)
                    self.state.replace_empty_thread(thread_id, new_id, project_id)
                    replaced, thread_id = thread_id, new_id
                    self.pending_stream_origin[thread_id] = message_id
                    effort = self.state.effort_for_thread(thread_id)
                    turn_id, steered = await self.codex.send(thread_id, content, allow_steer=True, effort=effort) if effort else await self.codex.send(thread_id, content, allow_steer=True)
            finally:
                self.pending_stream_origin.pop(original_thread_id, None)
                self.pending_stream_origin.pop(thread_id, None)
            self.owned_turns.add((thread_id, turn_id))
            self.stream_origins[(thread_id, turn_id)] = message_id
            self.state.mark_streaming(thread_id, turn_id)
        confirmation = ""
        if replaced:
            if forked:
                confirmation = f"原任务由另一 Codex 进程占用写入，已复制到新任务：{thread_id}\n原任务仍保留；后续 QQ 消息使用新任务。"
            else:
                confirmation = f"原任务尚未初始化，已自动换成新任务：{thread_id}"
        if confirmation:
            await self.reply(confirmation, reply_to=message_id, thread_id=thread_id)

    @staticmethod
    def _number(content, prefix, options):
        value = content[len(prefix):].strip()
        if not value.isdecimal() or not (1 <= int(value) <= len(options)):
            raise ValueError("编号无效，请先查看对应列表并使用其中的编号")
        return int(value) - 1

    async def on_approval(self, method, params):
        supported = {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
            "tool/requestUserInput",
        }
        if method not in supported:
            LOG.warning("Unsupported App Server request: %s", method)
            return {"action": "decline", "content": None}
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        if (thread_id, turn_id) not in self.owned_turns:
            if method == "tool/requestUserInput":
                return {"answers": {}}
            return {"permissions": {}} if "permissions" in method else {"decision": "decline"}
        question = None
        if method == "tool/requestUserInput":
            questions = params.get("questions") or []
            if len(questions) != 1 or questions[0].get("isSecret"):
                return {"answers": {}}
            question = questions[0]
        code = secrets.token_hex(3).upper()
        future = asyncio.get_running_loop().create_future()
        self.pending_approvals[code] = future
        self.approval_context[code] = (thread_id, turn_id, params.get("_request_id"))
        summary = (question or {}).get("question") or params.get("reason") or params.get("command") or params.get("grantRoot") or json.dumps(params.get("permissions") or {}, ensure_ascii=False)
        if question and question.get("options"):
            summary = str(summary) + "\n选项：" + "、".join(str(x.get("label", "")) for x in question["options"])
        try:
            await self.reply(f"Codex 请求审批 [{code}]\n任务：{thread_id[:12]}\n类型：{method}\n内容：{str(summary)[:700]}\n5 分钟内回复“批准 {code}”或“拒绝 {code}”。", thread_id=thread_id)
            accepted = await asyncio.wait_for(future, APPROVAL_TIMEOUT)
        except asyncio.TimeoutError:
            accepted = False
            await self.reply(f"审批 {code} 已过期，按拒绝处理。", thread_id=thread_id)
        finally:
            self.pending_approvals.pop(code, None)
            self.approval_context.pop(code, None)
        if method == "item/permissions/requestApproval":
            return {"permissions": params.get("permissions", {}) if accepted else {}, "scope": "turn"}
        if method == "tool/requestUserInput":
            options = question.get("options") or []
            desired = ("accept", "approve", "yes", "允许", "确认") if accepted else ("decline", "reject", "no", "拒绝", "取消")
            option = next((o.get("label", "") for o in options if any(word in o.get("label", "").lower() for word in desired)), None)
            return {"answers": {question["id"]: {"answers": [option]}}} if option else {"answers": {}}
        return {"decision": "accept" if accepted else "decline"}

    async def _progress(self, key, delta, item_id=None):
        if key not in self.owned_turns or key not in self.stream_origins or not delta:
            return
        async with self.stream_locks[key]:
            stream = self.turn_streams.get(key)
            if stream is None:
                stream = QQStream(self.api, self.owner, self.stream_origins[key])
                self.turn_streams[key] = stream
            if item_id and item_id not in self.progress_items[key]:
                self.progress_items[key].add(item_id)
                delta = ("\n\n" if stream.text else "") + delta
            try:
                async with self.send_lock:
                    await stream.update(delta)
            except Exception:
                LOG.exception("QQ progress stream failed; final answer will still be sent")

    @staticmethod
    def _tool_label(item):
        kind = item.get("type")
        if kind == "webSearch":
            action = item.get("action") or {}
            query = item.get("query") or action.get("query") or ", ".join(action.get("queries") or [])
            return "🌐 已搜索网页" + (f"：{query[:160]}" if query else "")
        if kind == "commandExecution":
            command = str(item.get("command") or "").replace("\n", " ")[:160]
            return "已运行命令" + (f"：`{command}`" if command else "")
        if kind == "mcpToolCall":
            name = "/".join(str(item.get(k) or "") for k in ("server", "tool")).strip("/")
            return "已调用工具" + (f"：{name}" if name else "")
        if kind == "dynamicToolCall":
            return "已调用工具" + (f"：{item['tool']}" if item.get("tool") else "")
        if kind == "fileChange":
            return "已修改文件"
        if kind == "imageView":
            return "已查看图片"
        return None

    async def on_codex_event(self, method, params):
        key = (params.get("threadId"), params.get("turnId"))
        if method.startswith("item/") and all(key):
            if key not in self.stream_origins and key[0] in self.pending_stream_origin:
                self.stream_origins[key] = self.pending_stream_origin[key[0]]
                self.owned_turns.add(key)
                self.state.mark_streaming(*key)
            item = params.get("item") or {}
            item_id = params.get("itemId") or item.get("id")
            if method == "item/started":
                if item_id:
                    self.turn_items[key][item_id] = item
                if item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
                    self.state.mark_streaming(*key)
            elif method == "item/agentMessage/delta":
                delta = params.get("delta") or ""
                phase = (self.turn_items[key].get(item_id) or {}).get("phase")
                if item_id:
                    self.item_deltas[key, item_id] += delta
                if phase == "commentary":
                    await self._progress(key, delta, item_id)
                elif phase == "final_answer":
                    self.state.mark_streaming(*key)
            elif method == "item/reasoning/summaryTextDelta":
                await self._progress(key, params.get("delta") or "", item_id)
            elif method == "item/completed":
                kind = item.get("type")
                if kind == "agentMessage":
                    phase = item.get("phase") or (self.turn_items[key].get(item_id) or {}).get("phase")
                    content = item.get("text") or self.item_deltas.get((key, item_id), "")
                    if phase == "final_answer" or phase is None:
                        if content:
                            self.final_answers[key] = content
                            self.state.mark_streaming(*key)
                    elif phase == "commentary" and item_id not in self.progress_items[key]:
                        await self._progress(key, content, item_id)
                elif key in self.owned_turns:
                    label = self._tool_label(item)
                    if label:
                        await self._progress(key, label, item_id)
                self.item_deltas.pop((key, item_id), None)
            return
        if method == "serverRequest/resolved":
            request_id = params.get("requestId")
            for code, (_, _, pending_id) in list(self.approval_context.items()):
                if request_id == pending_id:
                    future = self.pending_approvals.pop(code, None)
                    self.approval_context.pop(code, None)
                    if future and not future.done():
                        future.set_result(False)
        if method == "turn/completed":
            thread_id = params.get("threadId")
            turn_id = (params.get("turn") or {}).get("id")
            for code, (pending_thread, pending_turn, _) in list(self.approval_context.items()):
                if (pending_thread, pending_turn) == (thread_id, turn_id):
                    future = self.pending_approvals.pop(code, None)
                    self.approval_context.pop(code, None)
                    if future and not future.done():
                        future.set_result(False)
            key = (thread_id, turn_id)
            async with self.stream_locks[key]:
                stream = self.turn_streams.pop(key, None)
                if stream and not stream.failed:
                    try:
                        async with self.send_lock:
                            response = await stream.complete()
                        if response:
                            self.state.bind_outbound(response, thread_id)
                    except Exception:
                        LOG.exception("QQ progress stream completion failed")
                status = (params.get("turn") or {}).get("status")
                final = self.final_answers.pop(key, None)
                try:
                    if final and not self.state.has_sent_turn(*key):
                        await self.reply_long(final, thread_id=thread_id)
                        self.state.claim_turn(*key)
                    elif key in self.owned_turns and status not in ("completed", None):
                        await self.reply(f"Codex 任务结束，状态：{status}", thread_id=thread_id)
                except Exception:
                    LOG.exception("QQ final answer failed; Stop hook will retry")
                finally:
                    self.state.clear_streaming(*key)
                    self.stream_origins.pop(key, None)
                    self.turn_items.pop(key, None)
                    self.progress_items.pop(key, None)
                    self.owned_turns.discard(key)


async def main():
    values = {name: config_value(name) for name in ("QQBOT_APP_ID", "QQBOT_APP_SECRET", "QQBOT_USER_OPENID")}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise SystemExit("缺少配置：" + ", ".join(missing))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, filename=DATA_DIR / "qq_bridge.log", encoding="utf-8", format="%(asctime)s %(levelname)s %(message)s")
    lock_file = (DATA_DIR / "bridge.lock").open("a+b")
    if os.name == "nt":
        import msvcrt
        if lock_file.seek(0, 2) == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            lock_file.close()
            raise SystemExit("QQ 桥接进程已在运行") from exc
    state = State()
    shared_url = config_value("CODEXQQ_APP_SERVER_URL")
    if shared_url:
        os.environ["CODEXQQ_APP_SERVER_URL"] = shared_url
    codex = CodexClient()
    try:
        try:
            codex.shared = True
            await codex.connect()
            await codex.threads()
            LOG.info("Connected to shared App Server")
        except Exception:
            await codex.close()
            if shared_url:
                raise RuntimeError("共享 App Server 不可用；拒绝退回私有进程以免产生写入冲突")
            codex = await CodexClient().connect()
            LOG.warning("Shared App Server unavailable; using local App Server for idle tasks")
        async with httpx.AsyncClient(timeout=30) as http:
            api = QQApiClient(app_id=values["QQBOT_APP_ID"], client_secret=values["QQBOT_APP_SECRET"], log_tag="CodexQQBridge")
            api.setup(http)
            await api.ensure_token()
            bridge = Bridge(state, api, codex, values["QQBOT_USER_OPENID"])
            codex.approval_handler = bridge.on_approval
            codex.event_handler = bridge.on_codex_event
            session_store = WSSessionStore(str(DATA_DIR))
            saved = session_store.get(values["QQBOT_APP_ID"])
            desired_intents = (1 << 25) | (1 << 26)
            same_intents = saved.intents == desired_intents
            if saved.session_id and not same_intents:
                LOG.info("QQ gateway intents changed from %s to %s; requesting fresh Identify", saved.intents, desired_intents)
            session = {"id": saved.session_id if same_intents else None,
                       "seq": saved.seq if same_intents else None}
            interval = {"seconds": 45.0}

            def set_session(session_id, seq):
                session.update(id=session_id, seq=seq)
                session_store.save(values["QQBOT_APP_ID"], session_id or "", seq,
                                   intents=desired_intents)

            callbacks = WSCallbacks(
                on_message_event=bridge.on_message,
                on_interaction_event=bridge.on_interaction,
                on_connected=lambda: LOG.info("QQ READY (WebSocket connected or resumed)"),
                on_ready=lambda _event: LOG.info("QQ READY"),
                on_disconnected=lambda: LOG.warning("QQ WebSocket disconnected"),
                on_fatal_error=lambda code, msg: LOG.error("QQ WebSocket fatal %s: %s", code, msg),
                get_token=api.ensure_token_sync,
                get_session=lambda: (session["id"], session["seq"]),
                set_session=set_session,
                set_heartbeat_interval=lambda seconds: interval.update(seconds=seconds),
                clear_token=api.clear_token,
                fail_pending=lambda error: LOG.error("QQ pending calls failed: %s", error),
                get_gateway_url=lambda: gateway_url,
            )
            # C2C messages and inline-keyboard interaction callbacks.
            qq_websocket_module.DEFAULT_INTENTS = desired_intents
            websocket = QQWebSocket(callbacks, log_tag="CodexQQBridge")
            # The SDK only keys its short-lived cache by msg_id, whereas QQ
            # documents msg_id + msg_idx. Our SQLite claim is the authority.
            websocket._is_duplicate = lambda _msg_id: False
            gateway_url = await api.get_gateway_url()
            websocket.start(gateway_url, asyncio.get_running_loop())
            shared_outage = False
            try:
                while True:
                    await asyncio.sleep(30)
                    if websocket._ws_thread and not websocket._ws_thread.is_alive():
                        raise RuntimeError("QQ WebSocket 已停止；请查看日志并重启桥接进程")
                    if shared_url and not codex.connected:
                        if not shared_outage:
                            shared_outage = True
                            LOG.error("Shared App Server disconnected; waiting to reconnect")
                            try:
                                await bridge.reply("共享 Codex 服务已断开，QQ 暂时无法提交任务；服务恢复后会自动重连。")
                            except Exception:
                                LOG.exception("Could not notify owner of shared runtime outage")
                        candidate = CodexClient(shared=True,
                            approval_handler=bridge.on_approval,
                            event_handler=bridge.on_codex_event)
                        try:
                            await candidate.connect()
                            await candidate.threads()
                        except Exception:
                            await candidate.close()
                            LOG.warning("Shared App Server still unavailable")
                        else:
                            for key in bridge.owned_turns | set(bridge.final_answers):
                                state.clear_streaming(*key)
                            bridge.owned_turns.clear()
                            bridge.turn_streams.clear()
                            bridge.stream_origins.clear()
                            bridge.pending_stream_origin.clear()
                            bridge.turn_items.clear()
                            bridge.item_deltas.clear()
                            bridge.final_answers.clear()
                            bridge.progress_items.clear()
                            for future in bridge.pending_approvals.values():
                                if not future.done():
                                    future.set_result(False)
                            bridge.pending_approvals.clear()
                            bridge.approval_context.clear()
                            old = codex
                            codex = candidate
                            bridge.codex = candidate
                            await old.close()
                            shared_outage = False
                            LOG.info("Reconnected to shared App Server")
                            try:
                                await bridge.reply("共享 Codex 服务已恢复连接。")
                            except Exception:
                                LOG.exception("Could not notify owner of shared runtime recovery")
                    if (not shared_url and not codex.shared and not codex.active and not bridge.owned_turns
                            and CodexClient.shared_socket().exists()):
                        candidate = CodexClient(shared=True,
                            approval_handler=bridge.on_approval,
                            event_handler=bridge.on_codex_event)
                        try:
                            await candidate.connect()
                            await candidate.threads()
                        except Exception:
                            await candidate.close()
                        else:
                            old = codex
                            codex = candidate
                            bridge.codex = candidate
                            await old.close()
                            LOG.info("Switched to shared App Server")
            finally:
                await websocket.async_stop()
    finally:
        await codex.close()
        state.close()
        if os.name == "nt":
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        lock_file.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(filename=DATA_DIR / "qq_bridge.log", level=logging.INFO, encoding="utf-8")
        LOG.exception("QQ bridge stopped unexpectedly")
        raise
