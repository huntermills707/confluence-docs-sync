# Releasing and upgrading consumers

## Release policy

- Use semantic versions and matching annotated tags (`v1.0.0`, `v1.0.1`, …).
- Review `COMPATIBILITY.md` before choosing the version. Breaking consumer or
  publishing contracts require a major release.
- Treat release tags as immutable: never move, reuse, or delete a published tag.
  GitHub tag rulesets must enforce this restriction. Roll back consumers by
  selecting an older release instead.
- A full commit SHA is the preferred consumer pin because its identity does not
  depend on tag mutability. Never track `main`.
- Keep engine fixes in this repository. Never copy scripts into Alpha, Beta, or
  another content repository.
- Follow `GOVERNANCE.md`: reviewed pull requests, required CI, protected `main`
  and release tags, and least-privilege release/consumer access are required.

## Publish a release

1. Make the change on a tool-repository branch and add regression coverage.
2. Review `COMPATIBILITY.md`; update `VERSION` and `CHANGELOG.md` with the
   intended semantic version and any compatibility effects.
3. Run the complete offline suite:

   ```bash
   python -m pip install -r requirements.txt
   python -m unittest discover -s tests -v
   ```

4. Open a pull request and obtain the required review and `offline-tests` result.
   Merge only after both succeed.
5. Record the merged commit, then create and push an annotated tag for exactly
   that commit:

   ```bash
   git tag -a v1.0.1 -m "confluence-docs-sync v1.0.1" <reviewed-commit-sha>
   git push origin v1.0.1
   git rev-parse 'v1.0.1^{}'
   ```

6. Record the tagged commit SHA in the GitHub release notes. Verify that the
   release-tag ruleset rejects updates and deletion; use a new tag for any fix.

## Upgrade Alpha or Beta

1. In the content repository, change the `ref:` for the
   `huntermills707/confluence-docs-sync` checkout in both
   `.github/workflows/sync-confluence.yml` and `.github/workflows/validate.yml`
   to the same new full commit SHA (or protected release tag).
2. Confirm that the two pins match and that no engine source or dependency file
   is added to the consumer.
3. Open a pull request. The validator checks out that exact release, so PR
   validation exercises the proposed upgrade without Confluence credentials.
4. Before publication, run **Sync docs to Confluence** manually with `dry_run`
   enabled and review the plan.
5. Merge only after validation and the dry run succeed. Both workflows then use
   the new engine without any script copying.
6. Upgrade the other content repository to the same release and repeat its
   validation/dry-run checks.

## Roll back

1. Change both consumer workflow pins to the same previous release SHA or
   protected tag; do not edit or copy the engine.
2. Run PR validation and a reviewed dry-run before merging the rollback.
3. Publish Alpha and Beta, verify their expected no-op or intended reconciliation
   summaries, and record the rollback evidence.

## Required patch-release drill

For each consolidation acceptance cycle, perform the complete sequence above
with a real patch release: publish the tool fix, adopt it in both consumers,
validate, dry-run, publish, and then roll both consumers back to `v1.0.0` through
pin-only pull requests. Record all release, validation, dry-run, publication,
and rollback run IDs in the umbrella `TEST-REPORT.md` before closing Section 2.
After the rollback is verified, consumers may be moved forward again through a
new pin-only change; that forward restoration is also live publication work.
