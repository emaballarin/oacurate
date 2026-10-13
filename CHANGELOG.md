# Changelog

## 0.2.0 — 13/10/2026

- A curation the server returns with a row created more than 2 minutes before the submission is
  journalled as `duplicate`, even without a matching journal entry (the server answers `201` with
  the existing row for a re-submission).
- `plan add`, `plan remove` and `audit` deselect rows already submitted successfully per the
  journal (reason `already-submitted:<curation id>`; `--accept` overrides).
- `pending` reads every page of `GET /curations` and shows the server's check error or
  superseding curation.
- Requires Python 3.14: `simple-parsing` 0.1.9 does not import on Python 3.15.
- Live API findings (Milestone 0 and the first real submissions) recorded in `docs/api-notes.md`,
  with redacted fixtures and tests pinned to the real replies.

## 0.1.0 — 13/10/2026

- First implementation of `PLAN.md`: `show`, `orcid`, `name`, `plan add/remove/show/pick`, `audit`,
  `apply`, `pending`, `revert`.
