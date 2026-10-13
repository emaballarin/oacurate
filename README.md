# oacurate

Curate your own [OpenAlex](https://openalex.org) author profile through the official curation
API: set or detach its ORCID, set its display and match names, attach works from DOIs, OpenAlex
work IDs or BibTeX (Google Scholar exports included), detach works that are not yours, audit the
profile, and track or revert what you submitted.

**Status: 0.1.0.** All commands are implemented and covered by an offline test suite against
mocked HTTP. The read-only Milestone 0 probes have been run against the live API (U1, U4–U6 in
[`docs/api-notes.md`](docs/api-notes.md)). Not yet done: submitting a curation live, a dry run on
a real Scholar export, and a live audit. The shape of a curation row and of a `207` batch reply
are still unverified (U2); until they are, any reply the tool cannot positively recognise is
recorded as `unknown`, never as success.

## Install

Requires Python ≥ 3.14.

```fish
uv tool install git+https://github.com/emaballarin/oacurate   # or, from a checkout: uv tool install .
```

Runtime dependencies: `httpx` (≥ 0.28, < 1), `bibtexparser` (≥ 2.1, < 3), `simple-parsing`
(≥ 0.1.9). Development: `pytest`, `ruff`.

## Before you start

1. **Claim your profile** on openalex.org (author page → **Claim**). That step is web-only; a
   verified institutional e-mail on the account gives automatic approval. Curations from an
   account that does not own the profile are refused (`403`).
2. **Get an API key** (free) from your openalex.org account. The key is also your sign-in
   credential, so treat it like a password. Give it to the tool through `OPENALEX_API_KEY` or a
   file passed with `--api_key_file` (`chmod 600` it; the tool warns otherwise). There is no
   option that takes the key itself.
3. **Name your profile** with `--author A…` or `OACURATE_AUTHOR`.

Reading single works and authors is free and works without a key; list and search queries are
billed (see [Costs](#costs)).

## Quick start

```fish
set -x OACURATE_AUTHOR A5023888391
set -x OPENALEX_API_KEY (cat ~/.config/openalex/key)

oacurate show                                       # profile + journal summary, 0 credits
oacurate orcid set 0000-0001-6187-6610              # dry run: prints the exact payload
oacurate orcid set 0000-0001-6187-6610 --yes        # submits it

oacurate plan add --bib scholar.bib --dois_file dois.txt -o add.json
oacurate plan show add.json --status review         # read every row before submitting
oacurate plan pick add.json 3f9c1e2a7b --position 4 # anchor an ambiguous row by hand
oacurate apply add.json                             # dry run
oacurate apply add.json --accept W2741809807 --yes  # submit, plus one hand-checked review row

oacurate pending                                    # journal vs server
oacurate audit -o audit.json                        # missing / wrongly attached works
oacurate revert --since 13/10/2026 -o undo.json     # inverse plan, applied like any other
```

## Safety model

- **Two phases.** `plan add`, `plan remove`, `audit` and `revert` only write a plan file (JSON,
  stable order, so diffs between runs are meaningful). Only `apply` submits.
- **Dry run by default.** Every submitting command (`apply`, `orcid`, `name`) prints the exact
  JSON it would send and exits unless `--yes` is given.
- **Conservative defaults.** Only `claim` and `remove` rows are submitted by default. Ambiguous
  bylines, initial-only matches, works owned by larger profiles or by a profile with another
  ORCID, near-title matches and multiple versions are `review` rows: accept them one by one with
  `--accept`, after checking.
- **Re-validation.** Just before submitting, `apply` re-reads each work (free) and skips rows
  that went stale: byline gone or no longer unique, owner changed, already attached, no longer
  on the profile.
- **Canary.** The first item is sent alone; a `401`/`403` aborts everything.
- **Daily cap.** Claims are capped at 1,000 per rolling 24 h (OpenAlex's limit for profile
  owners); the journal counts what was already sent and the rest is left for later.
- **Journal.** Every submitted item is appended to
  `$XDG_DATA_HOME/oacurate/<author>.jsonl` (default `~/.local/share/oacurate/`), flushed and
  fsynced as soon as its reply arrives. `pending` reconciles it with the server; `revert` builds
  inverse plans from it.

Curations are applied by a daily OpenAlex pipeline, typically within 24–48 h. A removal is
sticky: it is re-applied every cycle.

## Commands

| Command                                | What it does                                                                                                                                              | Credits                             |
| -------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------- |
| `show`                                 | Profile summary and journal status                                                                                                                        | 0                                   |
| `orcid set ORCID [--force]`            | Checks the checksum locally and whether the ORCID already resolves to another (duplicate) profile; refuses to replace an existing ORCID without `--force` | 0                                   |
| `orcid remove ORCID`                   | Detaches the ORCID                                                                                                                                        | 0                                   |
| `name display NAME`                    | Sets the display name                                                                                                                                     | 0                                   |
| `name match NAME [--force]`            | Sets the match name (`full_name`); must be one of the profile's raw author names unless `--force`                                                         | 0                                   |
| `plan add …`                           | Resolves `--doi`, `--dois_file`, `--work`, `--bib` inputs and classifies each work                                                                        | 0, plus 10 per title-only input     |
| `plan remove --doi … --work …`         | Classifies works for detachment; refuses works whose byline carries your ORCID unless `--force`                                                           | 0                                   |
| `plan show PLAN [--status S …]`        | Prints a plan; `--status missing` prints ticket-ready text for works absent from OpenAlex                                                                 | 0                                   |
| `plan pick PLAN ACTION --position N`   | Anchors a claim row on the byline at author position N (1-based, as displayed)                                                                            | 0                                   |
| `audit [--merge_duplicates]`           | Removal candidates, addition candidates (byline ladder search) and duplicate profiles                                                                     | estimated up front; default cap 200 |
| `apply PLAN [--accept …] [--reject …]` | Submits a plan (dry run unless `--yes`)                                                                                                                   | 0                                   |
| `pending`                              | Journal vs every page of `GET /curations`: applied, pending, journal-only, server-only, unknown-outcome                                                   | 0                                   |
| `revert --since DD/MM/YYYY \| --ids …` | Inverse plan of journalled curations                                                                                                                      | 0                                   |

Common options, accepted after any command: `--author`, `--api_key_file`, `--journal`,
`--format table|tsv|json`, `--max_credits` (default 200), `--verbose` (HTTP log, key redacted).

**Option syntax.** Option names use underscores (`--no_search`, `--max_credits`): the
dashed variants of the argument library do not reach subcommands. List options take all their
values after a single flag (`--doi 10.1/a 10.1/b`); repeating the flag is refused, because the
library would otherwise keep only the last occurrence.

### Plan statuses

| Status           | Meaning                                                                                                                                                                                          | Submitted by default |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------- |
| `claim`          | One byline matches you (or carries your ORCID), and it is unowned, owned by a splinter profile (≤ `--splinter_max` works) or by a duplicate carrying your ORCID                                  | yes                  |
| `remove`         | A work to detach                                                                                                                                                                                 | yes                  |
| `review`         | Needs a human: `ambiguous`, `no-byline`, `initial-only`, `conflicting-orcid`, `owned-by-larger-profile`, `paratext`, `multiple-versions`, `title-near`, `owner-lookup-failed`, `byline-mismatch` | no (`--accept`)      |
| `blocked`        | The byline string occurs twice on the work (a claim would overwrite both) or cannot be encoded in a claim                                                                                        | never                |
| `protected`      | Removal refused: the byline carries your ORCID (`plan remove --force` overrides)                                                                                                                 | never                |
| `already`        | Already on the profile                                                                                                                                                                           | —                    |
| `not_on_profile` | Removal of a work that is not on the profile                                                                                                                                                     | —                    |

Inputs that produce no row (unparseable BibTeX blocks, duplicate keys, malformed identifiers,
works absent from OpenAlex, works found but not fetchable by single lookup, title-only inputs
under `--no_search`) are listed as unresolved, never dropped.

## Costs

OpenAlex bills by credits: a free key has 10,000 per day, keyless use 1,000 per day shared per IP.

- Single lookups (`/works/W…`, `/works/doi:…`, `/authors/A…`, `/authors/orcid:…`) are free; the
  tool never uses the billed URL-in-path forms.
- A list query costs 1 credit per page; anything with a search costs 10 per page.
- `plan add` resolves DOIs, arXiv IDs and work IDs for free; only title-only inputs (typical of
  Scholar exports) are title-searched, at 10 credits each, after printing the estimate.
- `audit` prints an estimate before anything billed and refuses to start above `--max_credits`.
- Every billed request is checked against `--max_credits` before it is sent; the real total is
  reported at the end.

## Limitations

- **Name matching** compares surname plus first given name (or its initial) against the
  profile's display name, its vetted raw names and every `--name` you pass. It is weak on CJK
  names, transliteration variants, name changes, single-name authors, surname-first bylines
  without a comma (`Smith J`) and `First, Last` bylines with a stray comma. Pass extra forms with
  `--name`, and set your ORCID: an ORCID on a byline outranks any name match.
- `plan add` has no co-author data (that would cost credits), so initial-only bylines are always
  `review` there; `audit` uses co-author overlap with your profile.
- Publication years in OpenAlex can be far off; the year is shown as evidence and never used to
  decide.
- Works that are not in OpenAlex cannot be attached; `plan show --status missing` prints text for
  a ticket at openalex.org/contact.
- Not supported: claiming the profile, affiliation curation, Scholar CSV input, scraping Google
  Scholar, writing to ORCID.
- Until OpenAlex documents how a claim and a removal of the same work interact, every
  claim/remove revert carries a caveat.

## Security and privacy

- The key is read only from the environment or a file; it is never accepted as an option, put
  in a URL, logged (`--verbose` redacts the header), or written to plans or the journal.
- Plan files contain no secrets. The journal contains curation IDs and the server's raw replies,
  which echo your OpenAlex user name; it is created with mode `0600`.
- BibTeX is parsed, never executed. No shell-outs.

## Exit codes

`0` success · `1` usage or validation error · `2` partial failure on apply (a rejected or
unknown item, items left over by the daily cap, or items left unsent by a stopped run) · `3` authentication (`401` /
`403`) · `4` budget (`429`, or `--max_credits`).

## Development

```fish
uv sync
uv run ruff format --check; and uv run ruff check; and uv run pytest
uv run pytest -m live                                   # free single lookups against the live API
uv run python scripts/record_fixtures.py               # re-record fixtures (free lookups only)
uv run python scripts/probe_api.py                      # Milestone 0 probe: needs a key, < 50 credits
```

The default test run makes no network calls. API facts, findings and open questions live in
[`docs/api-notes.md`](docs/api-notes.md); the design is in [`PLAN.md`](PLAN.md).

## Licence

MIT (see [`LICENSE`](LICENSE)).
