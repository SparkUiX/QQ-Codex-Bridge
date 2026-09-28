"""Project grouped, recency ordered QQ task lists."""

from collections import defaultdict
from datetime import datetime


PAGE_SIZE = 15


def recency(thread: dict) -> int:
    return int(thread.get("recencyAt") or thread.get("updatedAt") or thread.get("createdAt") or 0)


def title(thread: dict) -> str:
    return " ".join(str(thread.get("name") or thread.get("preview") or "未命名任务").split())[:36]


def task_line(number: int, thread: dict) -> str:
    stamp = datetime.fromtimestamp(recency(thread)).strftime("%m-%d %H:%M") if recency(thread) else "时间未知"
    return f"{number}. {title(thread)}  ({stamp})"


def groups(snapshot, threads: list[dict], project_for_thread):
    grouped = defaultdict(list)
    for thread in threads:
        project = project_for_thread(thread)
        grouped[project.id if project else None].append(thread)
    for values in grouped.values():
        values.sort(key=recency, reverse=True)
    return sorted(grouped.items(), key=lambda item: recency(item[1][0]), reverse=True)


def overview(snapshot, threads: list[dict], project_for_thread):
    numbers = {project.id: i for i, project in enumerate(snapshot.projects, 1)}
    names = {project.id: project.name for project in snapshot.projects}
    displayed = []
    lines = ["最近对话（按项目分组）："]
    for project_id, values in groups(snapshot, threads, project_for_thread):
        project_number = numbers.get(project_id, 0)
        lines.append(f"【{project_number} {names.get(project_id, '未归入项目')}】共 {len(values)} 条")
        for thread in values[:2]:
            displayed.append(thread)
            lines.append(task_line(len(displayed), thread))
    lines.append("/use 任务编号；/list 项目编号 [页码] 查看更多。项目编号见 /projects。")
    return "\n".join(lines), displayed


def project_page(snapshot, threads: list[dict], project_for_thread, project_number: int, page: int):
    if project_number < 0 or project_number > len(snapshot.projects) or page < 1:
        raise ValueError("项目编号或页码无效，请使用 /projects 查看项目")
    project = snapshot.projects[project_number - 1] if project_number else None
    project_id = project.id if project else None
    matched = [thread for thread in threads if (assigned.id if (assigned := project_for_thread(thread)) else None) == project_id]
    matched.sort(key=recency, reverse=True)
    start = (page - 1) * PAGE_SIZE
    selected = matched[start:start + PAGE_SIZE]
    if not selected:
        raise ValueError("该项目此页没有任务")
    name = project.name if project else "未归入项目"
    lines = [f"【{name}】第 {page} 页，共 {len(matched)} 条（最近对话优先）"]
    lines.extend(task_line(i, thread) for i, thread in enumerate(selected, 1))
    if start + PAGE_SIZE < len(matched):
        lines.append(f"下一页：/list {project_number} {page + 1}")
    lines.append("选择任务：/use 编号")
    return "\n".join(lines), selected
