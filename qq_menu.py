"""Preview or install QQ C2C command panels and optional global menu."""

import argparse
import asyncio
import json

import httpx
from qqbot_agent_sdk import QQApiClient

from qq_bridge import config_value


MENU = {"menu": {"items": [
    {"type": "send_message", "name": "任务", "send_message": "/list"},
    {"type": "send_message", "name": "项目", "send_message": "/projects"},
    {"type": "send_message", "name": "帮助", "send_message": "/help"},
]}}
PANEL_REMARK = "CodexQQ commands"
PANEL_ITEMS = [
    {"type": "command", "name": "/list", "desc": "按项目查看最近任务"},
    {"type": "command", "name": "/projects", "desc": "查看 Codex 项目"},
    {"type": "command", "name": "/status", "desc": "查看当前任务"},
    {"type": "command", "name": "/quota", "desc": "查看 Codex 额度"},
    {"type": "command", "name": "/help", "desc": "查看使用方法"},
    {"type": "command", "name": "/use", "desc": "选择列表中的任务编号"},
    {"type": "command", "name": "/new", "desc": "在所选项目新建任务"},
]


async def apply_panel(api, owner):
    listing = await api.request("GET", "/v2/panels?scope=c2c&limit=50")
    existing = next((record for record in listing.get("records", [])
                     if (record.get("panel") or {}).get("remark") == PANEL_REMARK), None)
    panel = {"items": PANEL_ITEMS, "remark": PANEL_REMARK}
    if existing:
        if existing.get("target_type") != "specific":
            raise RuntimeError("同名指令面板不是指定用户面板，拒绝覆盖")
        detail = await api.request("GET", f"/v2/panels/{existing['panel_id']}")
        if owner not in (detail.get("user_openids") or []):
            raise RuntimeError("同名指令面板未关联当前用户，拒绝覆盖")
        result = await api.request("PUT", f"/v2/panels/{existing['panel_id']}", {"panel": panel})
        return {"panel_id": existing["panel_id"], "updated": True, **result}
    return await api.request("POST", "/v2/panels", {
        "scope": "c2c", "target_type": "specific",
        "user_openids": [owner], "panel": panel,
    })


async def main():
    parser = argparse.ArgumentParser(description="QQ 指令面板与全局单聊菜单")
    parser.add_argument("--apply", action="store_true", help="覆盖 QQ 机器人当前的全局单聊菜单")
    parser.add_argument("--apply-panel", action="store_true", help="仅为指定的 QQ 用户创建或更新 / 指令面板")
    args = parser.parse_args()
    if not args.apply and not args.apply_panel:
        print(json.dumps({"command_panel": PANEL_ITEMS, "global_menu": MENU}, ensure_ascii=False, indent=2))
        return
    app_id = config_value("QQBOT_APP_ID")
    secret = config_value("QQBOT_APP_SECRET")
    if not app_id or not secret:
        raise SystemExit("缺少 QQBOT_APP_ID 或 QQBOT_APP_SECRET")
    owner = config_value("QQBOT_USER_OPENID")
    if args.apply_panel and not owner:
        raise SystemExit("缺少 QQBOT_USER_OPENID")
    async with httpx.AsyncClient(timeout=30) as http:
        api = QQApiClient(app_id=app_id, client_secret=secret, log_tag="CodexQQMenu")
        api.setup(http)
        if args.apply_panel:
            result = await apply_panel(api, owner)
            print("指令面板：", json.dumps(result, ensure_ascii=False))
        if args.apply:
            current = await api.request("GET", "/v2/menu")
            print("当前菜单：", json.dumps(current, ensure_ascii=False))
            result = await api.request("PUT", "/v2/menu", MENU)
            print("全局菜单更新结果：", json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
