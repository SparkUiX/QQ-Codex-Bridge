"""Read Codex desktop's local project labels and thread membership."""

import json
import os
from dataclasses import dataclass
from pathlib import Path


CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    path: Path


@dataclass
class ProjectSnapshot:
    projects: list[Project]
    thread_projects: dict[str, str]
    app_server_ids: dict[str, str]

    def by_id(self):
        return {p.id: p for p in self.projects}

    def project_for_thread(self, thread: dict, bridge_project: str | None = None) -> Project | None:
        by_id = self.by_id()
        project_id = (
            bridge_project
            or self.thread_projects.get(thread.get("id", ""))
            or self.app_server_ids.get(thread.get("projectId", ""))
            or thread.get("projectId")
        )
        if project_id in by_id:
            return by_id[project_id]

        # Older threads may have a workspace but no explicit desktop project
        # assignment. Resolve to the most specific registered root.
        cwd = thread.get("cwd")
        if not cwd:
            return None
        cwd_norm = os.path.normcase(os.path.normpath(str(cwd).removeprefix("\\\\?\\")))
        matches = []
        for project in self.projects:
            root = os.path.normcase(os.path.normpath(str(project.path)))
            if cwd_norm == root or cwd_norm.startswith(root + os.sep):
                matches.append(project)
        return max(matches, key=lambda p: len(str(p.path)), default=None)


def load_projects(home: Path = CODEX_HOME) -> ProjectSnapshot:
    path = home / ".codex-global-state.json"
    # The desktop app replaces this file while running. Retry a partial read.
    for attempt in range(2):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            break
        except (OSError, json.JSONDecodeError):
            if attempt:
                raise RuntimeError("无法读取 Codex 桌面项目列表") from None
    local = data.get("local-projects") or {}
    order = data.get("project-order") or list(local)
    projects = []
    for project_id in order:
        info = local.get(project_id) or {}
        roots = info.get("rootPaths") or []
        if roots and info.get("name"):
            projects.append(Project(project_id, info["name"], Path(roots[0])))
    assignments = {}
    for thread_id, value in (data.get("thread-project-assignments") or {}).items():
        if isinstance(value, dict) and value.get("projectKind") == "local":
            assignments[thread_id] = value.get("projectId")
    app_server_ids = {}
    for host_map in (data.get("app-server-project-id-by-legacy-project-id-by-host") or {}).values():
        for legacy_id, app_id in host_map.items():
            app_server_ids[app_id] = legacy_id
    return ProjectSnapshot(projects, assignments, app_server_ids)
