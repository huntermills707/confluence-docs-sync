# Releasing and upgrading consumers

## Release policy

- Use semantic versions and matching annotated tags (`v1.0.0`, `v1.0.1`, …).
- Treat release tags as immutable: never move, reuse, or delete a published tag.
  Roll back consumers by selecting an older release instead.
- A full commit SHA is also a valid consumer pin and is preferable when policy
  requires commit-level immutability.
- Keep engine fixes in this repository. Never copy scripts into Alpha, Beta, or
  another content repository.

## Publish a release

1. Make the change on a tool-repository branch and add regression coverage.
2. Update `VERSION` and `CHANGELOG.md` when preparing a new release.
3. Run the complete offline suite:

   ```bash
   python -m pip install -r requirements.txt
   python -m unittest discover -s tests -v
   ```

4. Merge the reviewed change to `main`, then create and push an annotated tag
   for the resulting commit:

   ```bash
   git tag -a v1.0.1 -m "confluence-docs-sync v1.0.1" <reviewed-commit-sha>
   git push origin v1.0.1
   ```

5. Record the tagged commit SHA in the GitHub release notes. Protect release
   tags against updates and deletion where repository policy supports it.

## Upgrade Alpha or Beta

1. In the content repository, change the `ref:` for the
   `huntermills707/confluence-docs-sync` checkout in both
   `.github/workflows/sync-confluence.yml` and `.github/workflows/validate.yml`
   to the same new tag or full commit SHA.
2. Open a pull request. The validator checks out that exact release, so PR
   validation exercises the proposed upgrade without Confluence credentials.
3. Before publication, run **Sync docs to Confluence** manually with `dry_run`
   enabled and review the plan.
4. Merge only after validation and the dry run succeed. Both workflows then use
   the new engine without any script copying.
5. Upgrade the other content repository to the same release and repeat its
   validation/dry-run checks.

To roll back, change both consumer pins to the previous release and run the
same validation and dry-run process. Do not edit or copy the engine in the
content repository.
