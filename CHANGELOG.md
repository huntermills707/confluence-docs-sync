# Changelog

## 1.0.1 — 2026-11-06

- Corrected missing-dependency guidance to reference the tool checkout's
  `requirements.txt` instead of the consumer working directory.
- Added stateful create, update, move, delete, unchanged, failure, and rerun
  reconciliation characterization tests.
- Added the compatibility contract and repository governance checklist.
- Documented protected-release and pin-based upgrade/rollback requirements.

## 1.0.0 — 2026-11-06

- Moved the validated Alpha/Beta sync engine into one canonical tool repository.
- Preserved conversion, identity, ownership, reconciliation, and deletion behavior.
- Added required `--repo-root` selection to sync, Mermaid rendering, and validation.
- Added offline characterization and content-root regression tests.
- Added thin-consumer checkout and release/upgrade documentation.
