# Compatibility contract

`confluence-docs-sync` uses semantic versions. Consumers pin an exact release tag
or commit SHA; no consumer should track a moving branch.

## Supported interfaces

The following are compatibility surfaces:

### Command line

- Every entry point requires `--repo-root`, which selects the content workspace.
- `sync.py` supports `--docs-dir` (default `docs`), `--dry-run`,
  `--force-delete`, and `--skip-mermaid`.
- `validate.py` and `render_mermaid.py` operate on `docs/` below the selected
  content root.
- Tool-relative imports and renderer discovery must continue to work from any
  current working directory.

### Environment

- Publication requires `ATLASSIAN_BASE_URL`, `ATLASSIAN_USER_EMAIL`,
  `ATLASSIAN_API_TOKEN`, `CONFLUENCE_SPACE_KEY`, `CONFLUENCE_ROOT_PAGE_ID`, and
  `GITHUB_REPOSITORY`.
- `DRY_RUN`, `FORCE_DELETE`, and `SKIP_MERMAID` accept the documented boolean
  values. `MERMAID_CLI` overrides the pinned Mermaid CLI package.
- `GITHUB_STEP_SUMMARY`, when set, receives the run summary.

### Files and generated output

- Relative `source_path` values remain rooted at the content repository.
- Mermaid output is written below `<repo-root>/build/mermaid/`, including
  `manifest.json`, and never below the tool checkout.
- Local images and rendered diagrams become content-addressed attachments.

### Remote identity and ownership

- The ownership property key remains `ghdocsync` and includes `repo`, `kind`,
  `doc_id`, `source_path`, `content_hash`, `synced_at`, and `tool`.
- Matching continues to use repository plus stable `doc_id`, not title, path, or
  Git rename detection.
- Managed attachment names retain the `ghsync-` prefix. Only that prefix is
  eligible for automatic attachment cleanup.
- The configured root and unmanaged or other-repository pages remain outside
  mutation scope.

### Runtimes

- Python 3.13 is the supported CI runtime and runs the complete offline suite.
- Node.js with `npx` on a GitHub-hosted runner is supported for Mermaid. Node 24
  is the validated runner runtime; `@mermaid-js/mermaid-cli` remains pinned in
  `render_mermaid.py`.
- Other local Python/Node versions are best-effort unless CI and release notes
  explicitly add support.

## Versioning rules

A **patch** release preserves all interfaces and behavior while fixing defects,
improving tests, or clarifying documentation.

A **minor** release may add backward-compatible options, environment variables,
metadata fields, or supported runtimes. Existing consumers must not need to
change their documents, roots, ownership markers, or workflow contracts.

A **major** release is required for removing or changing existing arguments,
environment variables, output locations, identity/ownership semantics,
attachment cleanup scope, or supported-runtime guarantees. Intentional storage
format changes that alter published bodies must also be called out explicitly
in the release notes even when the consumer interface is unchanged.

Every release must review these surfaces, run the complete offline suite, and
record compatibility-impacting changes in `CHANGELOG.md` and the GitHub release.
