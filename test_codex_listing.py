import json
import tempfile
import unittest
from pathlib import Path

from codex_listing import overview, project_page
from codex_projects import load_projects
from qq_menu import apply_panel


class ListingTests(unittest.TestCase):
    def test_desktop_names_membership_and_recency(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            alpha = home / "alpha-root"
            beta = home / "beta-root"
            alpha.mkdir()
            beta.mkdir()
            (home / ".codex-global-state.json").write_text(json.dumps({
                "local-projects": {
                    "p-a": {"name": "My UI Name", "rootPaths": [str(alpha)]},
                    "p-b": {"name": "Second UI Name", "rootPaths": [str(beta)]},
                },
                "project-order": ["p-a", "p-b"],
                "thread-project-assignments": {
                    "moved": {"projectKind": "local", "projectId": "p-b"},
                },
            }), encoding="utf-8")
            snapshot = load_projects(home)
            self.assertEqual([p.name for p in snapshot.projects], ["My UI Name", "Second UI Name"])
            threads = [
                {"id": "old", "name": "Old", "cwd": str(alpha), "recencyAt": 100},
                {"id": "new", "name": "New", "cwd": str(alpha), "recencyAt": 300},
                {"id": "moved", "name": "Moved", "cwd": str(alpha), "recencyAt": 200},
            ]
            assigned = snapshot.project_for_thread
            text, shown = overview(snapshot, threads, assigned)
            self.assertEqual([t["id"] for t in shown], ["new", "old", "moved"])
            self.assertLess(text.index("My UI Name"), text.index("Second UI Name"))
            page, selected = project_page(snapshot, threads, assigned, 2, 1)
            self.assertEqual([t["id"] for t in selected], ["moved"])
            self.assertIn("Second UI Name", page)


class MenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_panel_is_specific_to_owner(self):
        class FakeApi:
            def __init__(self):
                self.calls = []

            async def request(self, method, path, body=None):
                self.calls.append((method, path, body))
                return {"records": []} if method == "GET" else {"panel_id": "p-1"}

        api = FakeApi()
        await apply_panel(api, "owner-openid")
        method, path, body = api.calls[-1]
        self.assertEqual((method, path), ("POST", "/v2/panels"))
        self.assertEqual(body["target_type"], "specific")
        self.assertEqual(body["user_openids"], ["owner-openid"])
        self.assertIn("/list", [x["name"] for x in body["panel"]["items"]])


if __name__ == "__main__":
    unittest.main()
