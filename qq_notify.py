import asyncio
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from qq_state import State


LOG_DIR = Path(
    os.environ.get("LOCALAPPDATA", str(Path.home()))
) / "CodexQQ"

LOG_FILE = LOG_DIR / "qq_notify.log"
INVALID_UNICODE = re.compile("[\ud800-\udfff]")
CODEX_DIR = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


def get_config_value(name: str) -> str | None:
    value = os.environ.get(name)
    if value:
        return value

    if os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value, _ = winreg.QueryValueEx(key, name)
                return value or None
        except FileNotFoundError:
            pass

    return None


def write_log(message: str):
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    with LOG_FILE.open("a", encoding="utf-8") as f:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"[{timestamp}] {message}\n")


def describe_error(exc: Exception) -> str:
    secrets = [get_config_value(name) for name in (
        "QQBOT_APP_ID", "QQBOT_APP_SECRET", "QQBOT_USER_OPENID"
    )]
    parts = []
    seen = set()
    current = exc

    while current is not None and id(current) not in seen:
        seen.add(id(current))
        detail = str(current)
        for secret in secrets:
            if secret:
                detail = detail.replace(secret, "[REDACTED]")
        parts.append(f"{type(current).__name__}: {detail}")
        current = current.__cause__ or current.__context__

    return " <- ".join(parts)


def read_task_metadata(session_id: str, turn_id: str) -> tuple[str, int | None]:
    title = "未命名任务"
    started_at = None

    try:
        with (CODEX_DIR / "session_index.jsonl").open(encoding="utf-8") as index:
            for line in index:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("id") == session_id and record.get("thread_name"):
                    title = record["thread_name"]
    except OSError as exc:
        write_log(f"Task title lookup failed: {type(exc).__name__}: {exc}")

    try:
        with sqlite3.connect(
            f"{(CODEX_DIR / 'thread_history_1.sqlite').as_uri()}?mode=ro",
            uri=True,
            timeout=2,
        ) as db:
            row = db.execute(
                "SELECT started_at FROM thread_turns "
                "WHERE thread_id = ? AND turn_id = ?",
                (session_id, turn_id),
            ).fetchone()
            if row:
                started_at = row[0]
    except (OSError, sqlite3.Error) as exc:
        write_log(f"Turn timing lookup failed: {type(exc).__name__}: {exc}")

    title = INVALID_UNICODE.sub("\ufffd", " ".join(title.split()))
    title = title[:100] + ("…" if len(title) > 100 else "")
    title = re.sub(r"([\\`*_{}\[\]()#+.!>|~-])", r"\\\1", title)
    return title, started_at


def format_duration(seconds: int) -> str:
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分 {seconds} 秒"
    if minutes:
        return f"{minutes} 分 {seconds} 秒"
    return f"{seconds} 秒"


async def send_qq_message(message: str):
    import httpx
    from qqbot_agent_sdk import QQApiClient

    app_id = get_config_value("QQBOT_APP_ID")
    app_secret = get_config_value("QQBOT_APP_SECRET")
    user_openid = get_config_value("QQBOT_USER_OPENID")

    missing = []

    if not app_id:
        missing.append("QQBOT_APP_ID")

    if not app_secret:
        missing.append("QQBOT_APP_SECRET")

    if not user_openid:
        missing.append("QQBOT_USER_OPENID")

    if missing:
        raise RuntimeError(
            "缺少环境变量: " + ", ".join(missing)
        )

    async with httpx.AsyncClient(timeout=30.0) as http:
        api = QQApiClient(
            app_id=app_id,
            client_secret=app_secret,
            log_tag="CodexQQHook",
        )

        api.setup(http)

        try:
            await api.ensure_token()
        except Exception as exc:
            exc.add_note("stage=ensure_token")
            raise

        try:
            return await api.send_text(
                "c2c",
                user_openid,
                message,
                markdown=True,
                retries=1,
            )
        except Exception as exc:
            exc.add_note("stage=send_text")
            raise


async def main():
    # Codex 通过 stdin 传入 UTF-8 JSON；Windows 的 Python 默认可能按 GBK 解码。
    raw = sys.stdin.buffer.read().decode("utf-8")

    event = json.loads(raw or "{}")

    # 防止误触发其他类型的 Hook
    if event.get("hook_event_name") != "Stop":
        print("{}")
        return

    excluded = {value.strip() for value in (get_config_value("CODEXQQ_EXCLUDED_SESSION_IDS") or "").split(",") if value.strip()}
    if event.get("session_id") in excluded:
        print("{}")
        return

    message = (
        event.get("last_assistant_message")
        or ""
    ).strip()

    if not message:
        write_log(
            "Stop event received, but last_assistant_message was empty"
        )

        print("{}")
        return

    invalid_char_count = len(INVALID_UNICODE.findall(message))
    if invalid_char_count:
        message = INVALID_UNICODE.sub("\ufffd", message)

    # QQ 通知没必要把特别长的 Codex 输出全部塞进去。
    # 第一版先截断，后面可以做智能摘要。
    max_length = 1700

    if len(message) > max_length:
        message = (
            message[:max_length]
            + "\n\n……（Codex 回复过长，QQ 通知已截断）"
        )

    turn_id = event.get("turn_id", "unknown")
    session_id = event.get("session_id", "unknown")
    completed_at = datetime.now().astimezone()
    title, turn_started_at = read_task_metadata(session_id, turn_id)
    duration = (
        format_duration(int(completed_at.timestamp() - turn_started_at))
        if turn_started_at is not None else "未知"
    )
    qq_message = (
        "# Codex 任务完成\n\n"
        f"**任务：** {title}\n\n"
        f"**完成时间：** {completed_at:%Y-%m-%d %H:%M:%S}\n\n"
        f"**处理时长：** {duration}\n\n"
        "---\n\n"
        + message
    )
    started = time.monotonic()

    state = State()
    if state.is_streaming(session_id, turn_id):
        # Let the bridge finish an already visible stream before the Stop hook
        # tries its fallback. A crashed bridge still gets a bounded retry.
        for _ in range(20):
            if state.has_sent_turn(session_id, turn_id):
                print("{}")
                state.close()
                return
            if not state.is_streaming(session_id, turn_id):
                break
            await asyncio.sleep(.5)
    if not state.claim_turn(session_id, turn_id):
        print("{}")
        state.close()
        return

    try:
        response = await send_qq_message(qq_message)
        state.bind_outbound(response, session_id)

        write_log(
            f"QQ notification sent successfully; session_id={session_id}; "
            f"turn_id={turn_id}; chars={len(qq_message)}; "
            f"replaced_invalid_unicode={invalid_char_count}; "
            f"elapsed_s={time.monotonic() - started:.2f}"
        )

        # Stop Hook 的 stdout 应保持合法 JSON
        print("{}")

    except Exception as exc:
        state.release_turn(session_id, turn_id)
        stage = next((note for note in getattr(exc, "__notes__", [])
                      if note.startswith("stage=")), "stage=setup")
        write_log(
            f"QQ notification failed; session_id={session_id}; "
            f"turn_id={turn_id}; chars={len(qq_message)}; "
            f"replaced_invalid_unicode={invalid_char_count}; "
            f"elapsed_s={time.monotonic() - started:.2f}; "
            f"{stage}; error_chain={describe_error(exc)}"
        )

        # 不让 QQ 通知失败阻止 Codex。
        # systemMessage 会让 Codex UI 有机会提示错误。
        print(
            json.dumps(
                {
                    "systemMessage":
                    "QQ 通知发送失败，请检查 CodexQQ 日志。"
                },
                ensure_ascii=False,
            )
        )
    finally:
        state.close()


if __name__ == "__main__":
    asyncio.run(main())
