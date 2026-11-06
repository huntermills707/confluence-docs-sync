import hashlib
import pathlib
import sys
import tempfile
import unittest

TOOL_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_ROOT / "scripts"))

import sync


class CharacterizationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = pathlib.Path(self.temp_dir.name)
        docs = self.repo_root / "docs"
        (docs / "assets").mkdir(parents=True)
        (docs / "index.md").write_text(
            "---\ndoc_id: home\ntitle: Home\n---\n"
            "# Home\n\nSee [Guide](guide.md).\n",
            encoding="utf-8",
        )
        (docs / "guide.md").write_text(
            "---\ndoc_id: guide\ntitle: Guide\n---\n"
            "## Formatting\n\n**bold** and *em*.\n\n"
            "![Diagram](assets/example.png)\n",
            encoding="utf-8",
        )
        (docs / "assets" / "example.png").write_bytes(b"png-fixture")

    def tearDown(self):
        self.temp_dir.cleanup()

    def manifest(self):
        return sync.build_manifest(self.repo_root / "docs", self.repo_root, "docs")

    def test_manifest_preserves_relative_identities(self):
        manifest = self.manifest()
        self.assertEqual({"home", "guide"}, set(manifest.docs))
        self.assertEqual({}, manifest.folders)
        self.assertEqual("docs/index.md", manifest.docs["home"]["source_path"])
        self.assertEqual("docs/guide.md", manifest.docs["guide"]["source_path"])

    def test_conversion_golden_output_and_attachment_name(self):
        manifest = self.manifest()
        converter = sync.Converter(manifest, self.repo_root, "SPACE", {}, True)
        body, attachments = converter.convert(manifest.docs["guide"])
        digest = hashlib.sha1(b"png-fixture").hexdigest()[:8]
        name = f"ghsync-example-png-{digest}.png"
        self.assertEqual(
            "<h2>Formatting</h2><p><strong>bold</strong> and <em>em</em>.</p>"
            f'<p><ac:image ac:alt="Diagram"><ri:attachment '
            f'ri:filename="{name}"/></ac:image></p>',
            body,
        )
        self.assertEqual({name: (b"png-fixture", "image/png")}, attachments)
        self.assertEqual([], converter.errors)

    def test_internal_link_golden_output(self):
        manifest = self.manifest()
        converter = sync.Converter(manifest, self.repo_root, "SPACE", {}, True)
        body, attachments = converter.convert(manifest.docs["home"])
        self.assertEqual(
            "<h1>Home</h1><p>See <ac:link><ri:page "
            'ri:content-title="Guide" ri:space-key="SPACE"/>'
            "<ac:plain-text-link-body><![CDATA[Guide]]>"
            "</ac:plain-text-link-body></ac:link>.</p>",
            body,
        )
        self.assertEqual({}, attachments)
        self.assertEqual([], converter.errors)

    def test_creation_order_is_parents_first(self):
        child = {"id": "child", "source_path": "docs/parent/child.md"}
        parent = {"id": "parent", "source_path": "docs/parent/_index.md"}
        ordered = sync.creation_order([(child, "parent"), (parent, "ROOT")])
        self.assertEqual(["parent", "child"], [entry["id"] for entry, _ in ordered])

    def test_existing_property_version_is_incremented(self):
        class PropertyClient:
            def get_property(self, page_id):
                return {"version": {"number": 7}}

            def put(self, url, body):
                self.body = body
                return body

        client = PropertyClient()
        result = sync.Confluence.set_property(client, "123", {"test": True})
        self.assertEqual({"number": 8}, result["version"])
        self.assertEqual({"number": 8}, client.body["version"])


if __name__ == "__main__":
    unittest.main()
