import contextlib
import copy
import io
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

TOOL_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_ROOT / "scripts"))

import sync


class FakeConfluence:
    def __init__(self):
        self.pages = {
            "root": {"id": "root", "spaceId": "space", "status": "current",
                     "parentId": None, "title": "Root"},
        }
        self.properties = {}
        self.mutations = []
        self.next_id = 1000

    def space_by_key(self, key):
        return {"id": "space", "key": key}

    def get_page(self, page_id):
        return copy.deepcopy(self.pages[page_id])

    def pages_in_space(self, space_id):
        return copy.deepcopy(list(self.pages.values()))

    def get_property(self, page_id):
        return copy.deepcopy(self.properties.get(page_id))

    def create_page(self, space_id, title, parent_id, body):
        page_id = str(self.next_id)
        self.next_id += 1
        self.pages[page_id] = {
            "id": page_id, "spaceId": space_id, "status": "current",
            "parentId": str(parent_id), "title": title, "body": body,
        }
        self.mutations.append(("create", page_id, title, str(parent_id)))
        return copy.deepcopy(self.pages[page_id])

    def update_page(self, page_id, title, body):
        self.pages[page_id]["title"] = title
        self.pages[page_id]["body"] = body
        self.mutations.append(("update", page_id, title))
        return copy.deepcopy(self.pages[page_id])

    def move_page(self, page_id, new_parent_id):
        self.pages[page_id]["parentId"] = str(new_parent_id)
        self.mutations.append(("move", page_id, str(new_parent_id)))
        return copy.deepcopy(self.pages[page_id])

    def delete_page(self, page_id):
        self.pages[page_id]["status"] = "trashed"
        self.mutations.append(("delete", page_id))
        return {}

    def set_property(self, page_id, value):
        current = self.properties.get(page_id)
        version = 1 if current is None else current["version"]["number"] + 1
        self.properties[page_id] = {"value": copy.deepcopy(value),
                                    "version": {"number": version}}
        self.mutations.append(("property", page_id, value["source_path"]))
        return copy.deepcopy(self.properties[page_id])

    def list_attachments(self, page_id):
        return []

    def calls(self, operation):
        return [call for call in self.mutations if call[0] == operation]


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = pathlib.Path(self.temp_dir.name)
        self.docs = self.repo_root / "docs"
        self.docs.mkdir()
        self.confluence = FakeConfluence()
        self.write_document("index.md", "home", "Home", "# Home\n")
        for number in range(1, 5):
            self.write_document(f"stable-{number}.md", f"stable-{number}",
                                f"Stable {number}", f"# Stable {number}\n")
        self.write_document("folder/stable.md", "folder-stable",
                            "Folder Stable", "# Folder Stable\n")

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_document(self, relative_path, doc_id, title, body):
        path = self.docs / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\ndoc_id: {doc_id}\ntitle: {title}\n---\n{body}",
            encoding="utf-8",
        )

    def run_sync(self, expect_error=False):
        env = {
            "ATLASSIAN_BASE_URL": "https://example.atlassian.net",
            "ATLASSIAN_USER_EMAIL": "docs@example.com",
            "ATLASSIAN_API_TOKEN": "not-a-real-token",
            "CONFLUENCE_SPACE_KEY": "SPACE",
            "CONFLUENCE_ROOT_PAGE_ID": "root",
            "GITHUB_REPOSITORY": "example/content",
            "SKIP_MERMAID": "true",
        }
        argv = [str(TOOL_ROOT / "scripts" / "sync.py"),
                "--repo-root", str(self.repo_root)]
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sys, "argv", argv), \
                mock.patch.object(sync, "Confluence", return_value=self.confluence), \
                contextlib.redirect_stdout(stdout):
            if expect_error:
                with self.assertRaises(sync.SyncError):
                    sync.main()
            else:
                sync.main()
        return stdout.getvalue()

    def page_id(self, doc_id):
        matches = [page_id for page_id, prop in self.confluence.properties.items()
                   if prop["value"]["doc_id"] == doc_id]
        self.assertEqual(1, len(matches))
        return matches[0]

    def test_create_update_move_delete_unchanged_and_rerun(self):
        output = self.run_sync()
        self.assertIn("created=7", output)
        self.assertEqual(7, len(self.confluence.calls("create")))
        home_id = self.page_id("home")
        folder_id = self.confluence.pages[self.page_id("folder-stable")]["parentId"]

        self.confluence.mutations.clear()
        output = self.run_sync()
        self.assertIn("unchanged=7", output)
        self.assertEqual([], self.confluence.mutations)

        self.write_document("index.md", "home", "Home Renamed",
                            "# Home Renamed\n\nUpdated body.\n")
        output = self.run_sync()
        self.assertIn("updated=1", output)
        self.assertEqual([("update", home_id, "Home Renamed")],
                         self.confluence.calls("update"))
        self.assertEqual(home_id, self.page_id("home"))

        self.confluence.mutations.clear()
        (self.docs / "index.md").rename(self.docs / "folder" / "index.md")
        output = self.run_sync()
        self.assertIn("moved=1", output)
        self.assertIn("unchanged=7", output)
        self.assertEqual([("move", home_id, folder_id)], self.confluence.calls("move"))
        self.assertEqual([], self.confluence.calls("update"))
        self.assertEqual("docs/folder/index.md",
                         self.confluence.properties[home_id]["value"]["source_path"])

        self.confluence.mutations.clear()
        (self.docs / "folder" / "index.md").unlink()
        output = self.run_sync()
        self.assertIn("deleted=1", output)
        self.assertIn("unchanged=6", output)
        self.assertEqual([("delete", home_id)], self.confluence.calls("delete"))
        self.assertEqual("trashed", self.confluence.pages[home_id]["status"])

        self.confluence.mutations.clear()
        output = self.run_sync()
        self.assertIn("unchanged=6", output)
        self.assertEqual([], self.confluence.mutations)

    def test_conversion_failure_blocks_mutation_and_fixed_rerun_reconciles(self):
        self.run_sync()
        deleted_id = self.page_id("stable-1")
        original_pages = copy.deepcopy(self.confluence.pages)
        original_properties = copy.deepcopy(self.confluence.properties)
        self.confluence.mutations.clear()

        self.write_document("index.md", "home", "Home",
                            "# Home\n\n[Missing](missing.md)\n")
        (self.docs / "stable-1.md").unlink()
        self.run_sync(expect_error=True)
        self.assertEqual([], self.confluence.mutations)
        self.assertEqual(original_pages, self.confluence.pages)
        self.assertEqual(original_properties, self.confluence.properties)

        self.write_document("index.md", "home", "Home", "# Home\n")
        output = self.run_sync()
        self.assertIn("deleted=1", output)
        self.assertEqual([("delete", deleted_id)], self.confluence.calls("delete"))
        current_ids = [page_id for page_id, page in self.confluence.pages.items()
                       if page["status"] == "current"]
        self.assertEqual(len(current_ids), len(set(current_ids)))
        self.assertEqual(7, len(current_ids))  # root + six remaining managed pages


if __name__ == "__main__":
    unittest.main()
