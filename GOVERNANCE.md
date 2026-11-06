# Repository governance

The canonical engine can mutate every consumer's Confluence subtree, so release
integrity is part of the publishing safety boundary.

## Required controls

- Changes to `main` go through a pull request with at least one approving review.
- The `offline-tests` status check is required and must pass against an up-to-date
  branch before merge.
- Force-pushes and deletion of `main` are prohibited.
- `v*` release tags cannot be updated or deleted. A release fix uses a new tag;
  consumers roll back by selecting an earlier tag or commit SHA.
- Only the repository owner/release manager may create release tags and GitHub
  releases. Do not grant write access to automation that only needs to read the
  tool.
- Consumers use full commit SHAs when commit-level immutability is required.
  Validation and publication in the same consumer must always use the same ref.
- Private consumers receive distinct read-only deploy keys. Keys and checkout
  credentials are never shared with Confluence publication steps.

GitHub branch/tag rulesets enforce the applicable controls. Repository settings
and the rulesets themselves are restricted to repository administrators.

## Release review checklist

Before approving a tool change, reviewers confirm:

1. The change includes appropriate regression coverage and `offline-tests` passes.
2. `VERSION` and `CHANGELOG.md` match the intended release.
3. `COMPATIBILITY.md` has been reviewed and breaking changes use a major version.
4. No credentials, generated Mermaid output, or consumer-specific roots are
   included in the tool repository.
5. The release will target the reviewed merge commit, and consumer upgrades will
  change only workflow pins.

See `RELEASING.md` for the release, upgrade, and rollback procedure.
