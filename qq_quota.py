"""Compact QQ rendering of Codex account rate-limit windows."""

from datetime import datetime


def format_quota(result):
    limits = result.get("rateLimits") or {}
    buckets = result.get("rateLimitsByLimitId") or {limits.get("limitId", "codex"): limits}
    lines = ["Codex 额度"]
    for bucket in buckets.values():
        name = bucket.get("limitName") or bucket.get("limitId") or "Codex"
        windows = []
        for key in ("primary", "secondary"):
            window = bucket.get(key)
            if not window:
                continue
            minutes = window.get("windowDurationMins")
            used = window.get("usedPercent")
            if minutes is None or used is None:
                continue
            remaining = max(0, min(100, round(100 - used)))
            filled = round(remaining / 5)
            label = (f"{minutes // 1440} 天" if minutes % 1440 == 0 else
                     f"{minutes // 60} 小时" if minutes % 60 == 0 else f"{minutes} 分钟")
            reset = window.get("resetsAt")
            reset_text = datetime.fromtimestamp(reset).strftime("%m月%d日 %H:%M") if reset else "未知"
            windows.append(f"{label}：{'█' * filled}{'░' * (20 - filled)} 剩余 {remaining}%（重置：{reset_text}）")
        if windows:
            if len(buckets) > 1:
                lines.append(str(name))
            lines.extend(windows)
    if len(lines) == 1:
        return "当前账号未返回可显示的 Codex 额度窗口。"
    return "\n".join(lines)
