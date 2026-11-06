#!/usr/bin/env python3
"""Pull-request validation: check docs without touching Confluence.

Checks, for every .md file under docs/:
  * valid front matter with a doc_id and title,
  * doc_id and title uniqueness across the repository,
  * every relative .md link resolves to a managed document,
  * every local image reference exists on disk,
  * Markdown converts to Confluence storage format without errors,
  * mermaid blocks render (runs mmdc; the diagrams are not published).

No credentials are required or read by this script.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import sync  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Validate a content repository")
    ap.add_argument("--repo-root", required=True,
                    help="path to the content repository containing docs/")
    args = ap.parse_args()
    repo_root = pathlib.Path(args.repo_root).expanduser().resolve()
    docs_dir = repo_root / "docs"
    errors = []
    try:
        manifest = sync.build_manifest(docs_dir, repo_root,
                                       sync.norm_path("docs"))
    except sync.SyncError as e:
        print("ERROR " + str(e))
        raise SystemExit(1)

    import subprocess
    subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve().parent /
                     "render_mermaid.py"), "--repo-root", str(repo_root)], check=False)
    mermaid_map = sync.load_mermaid_map(repo_root)
    converter = sync.Converter(manifest, repo_root, os.environ.get(
        "CONFLUENCE_SPACE_KEY", "UNKNOWN_SPACE"), mermaid_map,
        skip_mermaid=False)
    for entry in manifest.entries.values():
        converter.convert(entry)
    errors.extend(converter.errors)

    if errors:
        for e in errors:
            print("ERROR " + e)
        raise SystemExit(1)
    n_docs = len(manifest.docs)
    n_folders = len(manifest.folders)
    print(f"validate: OK ({n_docs} documents, {n_folders} folder pages, "
          f"all links and diagrams check out)")


if __name__ == "__main__":
    main()
