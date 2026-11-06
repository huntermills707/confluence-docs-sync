import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

TOOL_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_ROOT / "scripts"))

import render_mermaid
import sync
import validate


class RepoRootTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.temp_dir.name)
        self.repo_root = base / "content"
        self.docs = self.repo_root / "docs"
        self.docs.mkdir(parents=True)
        self.write_plain_document()
        self.old_cwd = pathlib.Path.cwd()
        elsewhere = base / "unrelated-working-directory"
        elsewhere.mkdir()
        os.chdir(elsewhere)

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()

    def write_plain_document(self):
        (self.docs / "index.md").write_text(
            "---\ndoc_id: home\ntitle: Fixture Home\n---\n# Fixture Home\n",
            encoding="utf-8",
        )

    def run_main(self, module, script_name):
        argv = [str(TOOL_ROOT / "scripts" / script_name),
                "--repo-root", str(self.repo_root)]
        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout):
            module.main()
        return stdout.getvalue()

    def test_repo_root_is_required_by_every_entry_point(self):
        cases = ((sync, "sync.py"), (render_mermaid, "render_mermaid.py"),
                 (validate, "validate.py"))
        for module, script_name in cases:
            with self.subTest(script=script_name):
                argv = [str(TOOL_ROOT / "scripts" / script_name)]
                with mock.patch.object(sys, "argv", argv), \
                        contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        module.main()
                self.assertEqual(2, raised.exception.code)

    def test_sync_dry_run_uses_content_root_from_any_working_directory(self):
        class FakeConfluence:
            def space_by_key(self, key):
                return {"id": "space-id", "key": key}

            def get_page(self, page_id):
                return {"id": page_id, "spaceId": "space-id", "title": "Root"}

            def pages_in_space(self, space_id):
                return [{"id": "root", "spaceId": space_id, "status": "current",
                         "parentId": None, "title": "Root"}]

            def get_property(self, page_id):
                return None

        env = {
            "ATLASSIAN_BASE_URL": "https://example.atlassian.net",
            "ATLASSIAN_USER_EMAIL": "docs@example.com",
            "ATLASSIAN_API_TOKEN": "not-a-real-token",
            "CONFLUENCE_SPACE_KEY": "SPACE",
            "CONFLUENCE_ROOT_PAGE_ID": "root",
            "GITHUB_REPOSITORY": "example/content",
            "DRY_RUN": "true",
        }
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(sync, "Confluence", return_value=FakeConfluence()):
            output = self.run_main(sync, "sync.py")
        self.assertIn("docs/index.md", output)
        self.assertIn("DRY RUN: no changes were made", output)

    def test_renderer_writes_only_below_content_root(self):
        source = "graph TD;\n  A-->B;\n"
        (self.docs / "index.md").write_text(
            "---\ndoc_id: home\ntitle: Fixture Home\n---\n"
            f"```mermaid\n{source}```\n",
            encoding="utf-8",
        )

        def fake_run(cmd, **kwargs):
            svg = pathlib.Path(cmd[cmd.index("-o") + 1])
            svg.write_text("<svg xmlns=\"http://www.w3.org/2000/svg\"/>",
                           encoding="utf-8")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(render_mermaid.subprocess, "run", side_effect=fake_run) as run:
            self.run_main(render_mermaid, "render_mermaid.py")
        manifest_path = self.repo_root / "build" / "mermaid" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(1, len(manifest))
        rendered = pathlib.Path(next(iter(manifest.values()))["svg"])
        self.assertTrue(rendered.is_file())
        self.assertTrue(rendered.is_relative_to(self.repo_root))
        command = run.call_args.args[0]
        self.assertTrue(pathlib.Path(command[command.index("-i") + 1])
                        .is_relative_to(self.repo_root))
        self.assertFalse((TOOL_ROOT / "build").exists())

    def test_validate_delegates_to_tool_renderer_with_content_root(self):
        completed = mock.Mock(returncode=0)
        with mock.patch("subprocess.run", return_value=completed) as run:
            output = self.run_main(validate, "validate.py")
        self.assertIn("validate: OK (1 documents, 0 folder pages", output)
        command = run.call_args.args[0]
        self.assertEqual(sys.executable, command[0])
        self.assertEqual(str(TOOL_ROOT / "scripts" / "render_mermaid.py"), command[1])
        self.assertEqual("--repo-root", command[2])
        self.assertEqual(str(self.repo_root.resolve()), command[3])
        self.assertFalse(run.call_args.kwargs["check"])


if __name__ == "__main__":
    unittest.main()
