# confluence-docs-sync

Canonical, one-way GitHub → Confluence Cloud documentation publisher. This
repository owns the sync engine, Mermaid renderer, validator, Python
dependencies, and offline regression tests used by all content repositories.

The implementation was moved unchanged from the validated Alpha/Beta proof of
concept before the entry points were given an explicit content-root argument.
Page identity, ownership markers, conversion, reconciliation, and deletion
behavior are otherwise unchanged.

## Consumer contract

A content repository supplies:

- a `docs/` tree whose Markdown files have `doc_id` and `title` front matter;
- its Confluence space and dedicated root-page configuration;
- `GITHUB_REPOSITORY`, used as the ownership marker; and
- a pinned checkout of this tool.

The tool never infers the content root from its own location. Every entry point
requires `--repo-root`, so it can run from any working directory and any tool
checkout path. Relative document paths and `build/mermaid/` output remain
relative to the **content** repository.

Consumers should pin an immutable release tag or full commit SHA. The initial
consolidation release is **`v1.0.0`**. Do not track `main`.

Example workflow checkout:

```yaml
- uses: actions/checkout@v4
  with:
    repository: huntermills707/confluence-docs-sync
    ref: v1.0.0
    path: .confluence-docs-sync
    persist-credentials: false
```

If this repository is private, the checkout credential must have read-only
access to it. Keep that credential separate from all Confluence secrets.

## Local use

Install the pinned Python dependency in an isolated environment:

```bash
python -m pip install -r requirements.txt
```

Run the credential-free validator (Node.js/`npx` is required for Mermaid):

```bash
python scripts/validate.py --repo-root /path/to/content-repository
```

Render diagrams and perform a non-mutating reconciliation plan:

```bash
python scripts/render_mermaid.py --repo-root /path/to/content-repository
python scripts/sync.py --repo-root /path/to/content-repository --dry-run
```

Publication requires these environment variables:

| Variable | Purpose |
|---|---|
| `ATLASSIAN_BASE_URL` | Confluence Cloud site URL |
| `ATLASSIAN_USER_EMAIL` | Account email for Basic authentication |
| `ATLASSIAN_API_TOKEN` | Dedicated API token |
| `CONFLUENCE_SPACE_KEY` | Target space key |
| `CONFLUENCE_ROOT_PAGE_ID` | Existing, repository-specific root page ID |
| `GITHUB_REPOSITORY` | `owner/repository` ownership value |

`--dry-run`/`DRY_RUN=true`, `--force-delete`/`FORCE_DELETE=true`, and the local
only `--skip-mermaid` option retain their PoC meanings. `--docs-dir` defaults
to `docs` and remains relative to `--repo-root`.

## Tests

A clean checkout runs the complete offline suite with one command:

```bash
python -m unittest discover -s tests -v
```

The suite includes golden conversion checks and characterization coverage for
parent-first creation and property versions. Entry-point tests prove that sync,
validation, and rendering use the explicit content root rather than the tool
location or current working directory. No Confluence credentials or network
access are required.

## Repository layout

```text
scripts/
├── sync.py              canonical reconciliation engine
├── render_mermaid.py    canonical Mermaid renderer
└── validate.py          canonical credential-free validator
tests/                   offline characterization and entry-point tests
requirements.txt         pinned Python runtime dependency
VERSION                  release version without the leading "v"
CHANGELOG.md             release history
RELEASING.md             release and consumer upgrade procedure
.github/workflows/       tool PR/push regression tests
```

See [RELEASING.md](RELEASING.md) to publish a fix or upgrade/roll back consumer
pins. Content repositories must not carry copies of anything under `scripts/`.
