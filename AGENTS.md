# Codex QQBot project

- `qq_bridge.py` is the long-running QQ ingress. It uses `qqbot_agent_sdk` for the gateway and `codex_client.py` for App Server requests. `shared_runtime.py` lets Desktop and the bridge share one WebSocket App Server; `qq_notify.py` remains the global `Stop` Hook fallback for final replies.
- `codex_projects.py` reads desktop project labels and thread assignments from `.codex-global-state.json`; `codex_listing.py` groups tasks by that membership and sorts by `recencyAt`. The app's state format is internal, so verify it against the installed Codex version when changing this integration.
- Shared state lives in `%LOCALAPPDATA%\CodexQQ\bridge.sqlite3`. Keep the QQ message reference to Codex task mapping and the inbound/turn deduplication durable across process restarts.
- Only the `QQBOT_USER_OPENID` C2C sender may submit commands or approve actions. Preserve Codex's existing task permissions; never silently resume an externally active desktop turn through a separate App Server.
- Run `py -3 -m unittest -v test_codex_client test_qq_bridge test_qq_notify test_codex_listing test_qq_quota` and `py -3 -m py_compile qq_bridge.py qq_notify.py qq_state.py codex_client.py codex_projects.py codex_listing.py qq_menu.py qq_stream.py qq_quota.py install_startup.py shared_runtime.py start_shared.py` after changes. A short live startup check should reach QQ `READY` without sending a prompt.
- `qq_menu.py --apply-panel` creates or updates the owner's C2C command panel. `qq_menu.py --apply` overwrites the global QQ menu. Do not run either as part of tests or startup.
