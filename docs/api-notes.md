# OpenAlex API notes

Ground truth that `oacurate` builds on. Sections 1–3 are copied from `PLAN.md` §2 (verified
13/10/2026); section 4 records what was learnt while implementing; section 5 tracks the
Milestone 0 unknowns. Any change of fact is recorded here, with its provenance.

## 1. Curation API (`user.openalex.org`)

| Fact                                                                                                                                                                     | Source                                                                  |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------- |
| `POST https://user.openalex.org/curations`; body is one JSON object or an array                                                                                          | docs (api/author-curation)                                              |
| Auth is `Authorization: Bearer <api_key>`. No auth → `401 {"code":"unauthorized","message":"Authentication required."}` for both GET and POST                            | docs + probe                                                            |
| An array POST returns `207 Multi-Status` with one outcome per item; one rejected item does not stop the others                                                           | docs (access/fixing-errors/ai-curation-guide)                           |
| Success returns `201` with a record: `id` (`cur-…`), `user_id`, `user_name`, `entity`, `entity_id`, `property`, `value`, `action`, `created`, `is_applied`, `applied_at` | docs                                                                    |
| Re-submitting an identical curation (same user, entity, entity_id, value, action) returns the existing row; no duplicate is created                                      | docs                                                                    |
| A user who neither owns the claimed profile nor is a curator gets `403`                                                                                                  | docs (data/curations)                                                   |
| A profile owner's adds are capped at 1,000 per rolling 24 h; removals are uncapped                                                                                       | docs (access/fixing-errors/authors)                                     |
| Curations are applied by a daily pipeline; changes are typically live within ~24–48 h                                                                                    | docs                                                                    |
| An ORCID with a bad check digit is rejected with `400`                                                                                                                   | docs                                                                    |
| `GET user.openalex.org/curations?entity=…&status=pending` exists (documented with `entity=ras`)                                                                          | docs (access/fixing-errors/affiliations); **response shape unverified** |

**Exact payloads.** These are built in Python and encoded with `json.dumps`; never escape by hand.

```jsonc
// claim a work: attach the authorship whose byline is <raw> to author A
{"entity":"works","entity_id":"https://openalex.org/W…",
 "property":"authorships[raw_author_name=\"<raw>\"].author.id",
 "action":"replace","value":"https://openalex.org/A…"}

// remove a work: detach author A from work W (sticky: re-applied every cycle)
{"entity":"works","entity_id":"https://openalex.org/W…",
 "property":"authorships.author.id","action":"remove","value":"https://openalex.org/A…"}

// display name
{"entity":"authors","entity_id":"https://openalex.org/A…",
 "property":"display_name","action":"replace","value":"<name>"}

// match name: should be one of the author's existing raw author names
{"entity":"authors","entity_id":"https://openalex.org/A…",
 "property":"full_name","action":"replace","value":"<name>"}

// ORCID set / detach (value is the ORCID in both cases; property is flat "orcid")
{"entity":"authors","entity_id":"https://openalex.org/A…",
 "property":"orcid","action":"replace","value":"https://orcid.org/0000-…"}
{"entity":"authors","entity_id":"https://openalex.org/A…",
 "property":"orcid","action":"remove","value":"https://orcid.org/0000-…"}
```

- `<raw>` is the work's `raw_author_name`, byte for byte. Inner `"` and `'` are **not**
  escaped inside the property string (the JSON encoder escapes `"` at the JSON level only).
  The server extracts it with the regex `^authorships\[raw_author_name="(.+)"\]\.author\.id$`.
- A claim overwrites `author_id` at the key `(work_id, raw_author_name)`. **Inference:** if
  two authorships on one work share an identical raw string, both would be overwritten, so
  the tool treats that case as `blocked`.
- A remove sets `author_id = NULL` at `(work_id, author_id)` and is re-applied every cycle.
- Setting an ORCID records it and makes it the match key for _future_ works. It does not
  move existing works and does not merge profiles.
- What happens when a claim and a remove both exist for the same work and author is
  **undocumented** (see U7).

## 2. Read API (`api.openalex.org`)

| Fact                                                                                                                                                                                                                                                                | Source                              |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------- |
| Single-entity GETs cost 0 credits: `/works/W…`, `/works/doi:<doi>`, `/authors/A…`, `/authors/orcid:<orcid>` (header `X-RateLimit-Credits-Used: 0`; they kept working with the daily budget at zero)                                                                 | probe                               |
| **The same lookups written with a URL in the path are billed (1 credit):** `/works/https://doi.org/…`, `/authors/https://orcid.org/…`. Always use the `doi:` and `orcid:` prefixed forms                                                                            | probe                               |
| `doi:` lookups are case-insensitive and accept either a bare DOI or a `https://doi.org/…` value after the `doi:` prefix                                                                                                                                             | probe                               |
| DOIs containing `()<>;:` resolve when percent-encoded with `urllib.parse.quote(doi, safe="/:")` (tested on `10.1002/(SICI)1097-0258(19980430)17:8<857::AID-SIM777>3.0.CO;2-E` → `W2146272590`)                                                                      | probe                               |
| An unknown DOI returns `404`. An old author ID (one sample) also returned `404`, with no redirect                                                                                                                                                                   | probe                               |
| **A DOI lookup can return a work whose `doi` field is a different DOI**, because the match was on a secondary location. Example: `doi:10.48550/arXiv.1706.03762` → `W2626778328`, whose `doi` is `10.65215/2q58a426`; the arXiv DOI appears under `locations[].doi` | probe                               |
| **`publication_year` can be far off** (`W2626778328` reports 2025 for a 2017 paper). Year is weak evidence only and never gates a decision                                                                                                                          | probe                               |
| arXiv works resolve via the DOI `10.48550/arXiv.<id>` (OpenAlex stores it lower-cased)                                                                                                                                                                              | probe                               |
| A single-work response carries the full authorship list; list responses cut it to 100 authorships per work                                                                                                                                                          | docs (ai-curation-guide)            |
| List queries default to the core corpus; `corpus=all` adds the expansion corpus (~22% of byline-match candidates). Never mix `corpus=` with the deprecated `include_xpac` (that is an error). Single lookups accept `corpus=all` (200)                              | docs (data/works/corpus) + probe    |
| `per_page` maximum is 100 (200 is deprecated legacy; the "Audit a profile" recipe still uses it — don't copy that). Basic paging stops at 10,000 results; use `cursor=*` beyond                                                                                     | docs (api/paging)                   |
| OR within one filter uses `\|`, up to 100 values; OR across different filters is an error                                                                                                                                                                           | docs (api/filtering)                |
| Request URLs are limited to ~4 KB; split long OR queries and union the results client-side                                                                                                                                                                          | docs (api/searching)                |
| Cost: single lookup free; list + filter 1 credit; search 10 credits (`search=`, `search.*`, and `*.search` filters). A free key gives $1/day = 10,000 credits; keyless gives $0.10/day, shared per IP. A `title.search` request reported `creditsRequired: 10`      | docs (access/example-costs) + probe |
| `429` means either the daily budget is exhausted (JSON body with `retryAfter`, `dailyRemainingUsd`, `creditsRequired`, …) or more than 100 req/s                                                                                                                    | docs + probe                        |
| Rate headers: `X-RateLimit-Limit`, `-Remaining`, `-Credits-Used`, `-Credits-Required`, `-Reset` (seconds to midnight UTC), `-Cost-USD`                                                                                                                              | probe                               |
| The key can also go in an `api_key` query parameter. **Never do this**: it leaks into URLs and logs                                                                                                                                                                 | docs (api/authentication)           |
| The API key is also the openalex.org sign-in credential                                                                                                                                                                                                             | docs (api/authentication)           |
| `raw_author_name.search`: a quoted phrase matches within a single byline; `~N` allows N intervening tokens; combine with `"a" OR "b"` inside one filter value (separate clauses AND together)                                                                       | docs (api/searching)                |
| Title search: the `search.title=…` parameter or the `filter=title.search:…` filter                                                                                                                                                                                  | docs (api/searching)                |
| Author fields: `display_name`, `display_name_alternatives`, `full_name`, `raw_author_names`, `orcid`, `observed_orcids`, `works_count`, `last_known_institutions`, …                                                                                                | probe                               |
| Authorship fields: `author_position`, `author{id,display_name,orcid,observed_orcids}`, `raw_author_name`, `raw_orcid`, `is_corresponding`, `institutions`, `raw_affiliation_strings`, `affiliations`, `countries`                                                   | probe                               |
| Work fields used: `id`, `doi`, `ids`, `title`, `display_name`, `publication_year`, `type`, `is_paratext`, `is_retracted`, `is_xpac`, `primary_location`, `locations`, `authorships`                                                                                 | probe                               |
| `/authors?filter=orcid:<id>` matches the primary ORCID or any `observed_orcids` entry (a list query, 1 credit)                                                                                                                                                      | docs (api/filtering)                |
| Status codes: `400` bad syntax, `403` forbidden, `404` not found, `429` rate or budget, `500` transient (retry with backoff)                                                                                                                                        | docs (api/errors)                   |

## 3. Libraries and tooling

| Fact                                                                                                                                                                                                          | Source                          |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------- |
| `simple-parsing` 0.1.9: nested subcommands via `command: A \| B = subparsers({"a": A, "b": B})` work at two levels                                                                                            | probe                           |
| **Options from a dataclass added separately (`parser.add_arguments(Common, …)`) must come before the subcommand**; placing them after it gives "unrecognized arguments"                                       | probe                           |
| `simple_parsing.helpers.serialization.Serializable` round-trips nested dataclasses containing `Literal` and `X \| None` fields                                                                                | probe                           |
| `bibtexparser` 2.1.0 (stable): `bibtexparser.parse_file`/`parse_string(…, append_middleware=[LatexDecodingMiddleware()])`; entries are in `library.entries`, with fields in `entry.fields` (`.key`, `.value`) | probe                           |
| bibtexparser puts unparseable entries in `library.failed_blocks` and only logs a warning; it does **not** raise                                                                                               | probe                           |
| `LatexDecodingMiddleware` decodes `{\"u}` → `ü` **and** `--` → `–` (en dash)                                                                                                                                  | probe                           |
| Scholar BibTeX truncates author lists with `and others`, and carries no DOIs                                                                                                                                  | probe + external library guides |
| `httpx` 0.28.1 is the stable release (1.0 is dev-only); `httpx.MockTransport` works for offline tests                                                                                                         | probe + PyPI                    |
| Current releases: `uv_build` 0.13.0, `pytest` 9.1.1, `ruff` 0.17.0                                                                                                                                            | PyPI                            |

## 4. Findings during implementation (13/10/2026)

Provenance: **probe** = a local check against the installed library or a live free lookup.

| Fact                                                                                                                                                                                                                                                                    | Source | Consequence in the code                                                                                                         |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------- |
| Keyless single lookups report `X-RateLimit-Credits-Used: 0` (all fixtures recorded by `scripts/record_fixtures.py`)                                                                                                                                                     | probe  | `show` and DOI/W plans work without a key                                                                                       |
| A key the server does not know gets `401 {"message": "API key not found"}` **even on free single lookups**; without a key the same lookup is `200`                                                                                                                      | probe  | The `401` message names `OPENALEX_API_KEY` / `--api_key_file`                                                                   |
| `A5023888391.raw_author_names` contains `"Jason, Priem"` (a stray-comma byline)                                                                                                                                                                                         | probe  | Surname vetting drops it from the seeds (the README lists the weak spot)                                                        |
| simple-parsing 0.1.9: **repeating a list option keeps only the last occurrence** (`--doi a --doi b` → `["b"]`); `--doi a b` works                                                                                                                                       | probe  | `cli.check_repeated_options` refuses any repeated option (`-o` and `--output` count as one) instead of dropping values silently |
| simple-parsing 0.1.9: `add_option_string_dash_variants=DashVariant.DASH` works on a flat parser but **not inside subparsers**                                                                                                                                           | probe  | Option names keep their underscores (`--no_search`); documented in the README                                                   |
| simple-parsing 0.1.9: `allow_abbrev=False` is **not propagated to subparsers**, so `--api_key x` was accepted as an abbreviation of `--api_key_file`. Subparsers are built with `parser_class=type(parser)`                                                             | probe  | `cli.Parser` forces `allow_abbrev=False` in `__init__`; a test pins that `--api_key` is rejected                                |
| simple-parsing 0.1.9 `Serializable` does not decode PEP 695 `type X = Literal[…]` aliases (warns and skips the membership check); plain `X = Literal[…]` aliases are checked                                                                                            | probe  | `model.py` uses plain aliases; `Plan.validate()` checks membership explicitly anyway                                            |
| simple-parsing 0.1.9 **fails to import on Python 3.15** (`from dataclasses import _MISSING_TYPE`; 3.15 replaced that class with `MISSING = sentinel("MISSING")`), and 0.1.9 is the latest release (27/07/2026). Reported 13/10/2026 after `uv tool install` picked 3.15 | probe  | `requires-python = ">=3.14,<3.15"` until a fixed release exists                                                                 |
| simple-parsing 0.1.9 on Python 3.14 triggers argparse's `DeprecationWarning` about `prefix_chars` in `add_argument_group`                                                                                                                                               | probe  | Filtered in the pytest configuration; harmless at run time                                                                      |
| bibtexparser 2.1.0: `failed_blocks[*].start_line` is 0-based; the parse error text is in `block.error.abort_reason`; a duplicate key yields a `DuplicateBlockKeyBlock` in `failed_blocks`; field keys keep their case (`archivePrefix`)                                 | probe  | Reported lines are `start_line + 1`; field lookup is case-insensitive                                                           |
| bibtexparser 2.1.0: `LatexDecodingMiddleware` also rewrites identifier fields (`https://doi.org/10.1000/a--b` → `…/a–b`)                                                                                                                                                | probe  | Identifiers (`doi`, `url`, `eprint`, …) come from a second, undecoded parse; text fields from the decoded one                   |

## 5. Milestone 0 status

Read-only probe run on 13/10/2026 (`scripts/probe_api.py` without `--with-write-sample`; 22
credits); redacted responses in `tests/fixtures/live/probe_*.json`. The account had no curations
yet, so row shapes are still unseen. Anything the parsers do not positively recognise stays
`unknown`, never success.

| ID  | Status                        | Finding (13/10/2026)                                                                                                                                                                                                                                                                                                                                                             | Handling in the code                                                                                                                                                                                                                                                                                                                                                                                                      |
| --- | ----------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| U1  | envelope settled; rows unseen | `GET /curations` → `200 {"meta": {"count", "page", "per_page", "total_count", "total_count_capped", "total_pages"}, "results": [...]}`; default `per_page` 25; `page` / `per_page` honoured; `entity`, `status`, `entity=works&status=pending` all accepted (`200`). An account with no curations gets `results: []` with no parameters, so the list is scoped to the key's user | `pending` reads `results` page by page up to `meta.total_pages`. Row fields (`id`, `is_applied`, `applied_at`, `created`, …) are still the documented ones, unverified. Whether `entity` / `status` actually filter cannot be seen on an empty list                                                                                                                                                                       |
| U2  | open                          | Needs `--with-write-sample --author A…` on a claimed profile. Observed 13/10/2026: a single-object claim POST for A5122991001 from an account not (or not yet) owning it → `403 {"message": "Claim-owners may only add/remove works for their own claimed author."}`; the canary aborted the run and nothing was created                                                         | Single-object POST: `201` + a row echoing every payload field → `created`, `200` + row → `duplicate` (assumed), `400` → `rejected`. `207`: one entry per item (top-level list or under `results` / `items` / `data` / `curations` / `responses`) with an integer `status` and the row inline or under `data` / `body` / `curation` / `result` / `record`. A curation ID already in the journal also counts as `duplicate` |
| U3  | not probed                    | —                                                                                                                                                                                                                                                                                                                                                                                | `--chunk 25`                                                                                                                                                                                                                                                                                                                                                                                                              |
| U4  | settled                       | `raw_author_name.search` filter: 10 credits; `author.id` filter: 1; `search.title`: 10 (each `per_page=1`, `corpus=all`). A single lookup with `select=` costs 0. Rate headers: `X-RateLimit-Limit: 10000` (credits per day for this key), `X-RateLimit-Cost-USD: 0.0001` per credit                                                                                             | Matches the estimates already in the code (10 for any search, else 1); owner lookups with `select=` are free                                                                                                                                                                                                                                                                                                              |
| U5  | inconclusive                  | `OPTIONS /curations/<id>` → `200`, empty `text/html` body, **no `Allow` header**                                                                                                                                                                                                                                                                                                 | No cancel; `revert` always builds an inverse plan. Ask OpenAlex together with U7; never probe with `DELETE`                                                                                                                                                                                                                                                                                                               |
| U6  | settled                       | An expansion-corpus work (`is_xpac: true`, `W6893254818`, DOI `10.5281/zenodo.15521699`) resolves by `/works/doi:…` and by `/works/W…`, both `200` at 0 credits, with no `corpus=` parameter                                                                                                                                                                                     | Single lookups keep omitting `corpus=`; a `lookup-failed` input is therefore unlikely to be an expansion work                                                                                                                                                                                                                                                                                                             |
| U7  | ask OpenAlex                  | —                                                                                                                                                                                                                                                                                                                                                                                | Every claim/remove inverse carries the caveat warning                                                                                                                                                                                                                                                                                                                                                                     |

## 6. Implementation decisions beyond the letter of PLAN.md (13/10/2026)

Recorded so that none of them is mistaken for inertia; each can be reverted on its own.

- **Option parsing.** Abbreviations are disabled at every subparser level, and any repeated option
  is refused, because simple-parsing would keep only its last value (§4).
- **Name tokens.** Besides the plan's hyphen/dot/apostrophe rule: `casefold()` instead of
  `lower()`; `ø ł đ ð æ œ þ ı` transliterated instead of dropped; typographic apostrophes are
  separators like `'`; generational suffixes (`jr`, `sr`, `ii`, `iii`, `iv`) are ignored.
- **arXiv IDs.** An `eprint` value written as `arXiv:…` is accepted without `archivePrefix`.
- **Missing inputs.** Inputs with no work are carried in `Plan.unresolved` (reason `missing`, or
  `lookup-failed` when a work was found but its single lookup fails), not as `Action` rows with
  status `missing`, which would have no work ID to act on. The `missing` status stays in the
  schema, unused.
- **Extra statuses and reasons.** `review(owner-lookup-failed)` when the owner of a byline cannot
  be fetched; `blocked(unanchorable-byline)` when the server regex cannot recover the byline from
  the claim property (for example a newline). Author-level actions use status `claim` for
  `replace` and `remove` for `remove`.
- **Removal checks** look at every authorship the target holds on the work: one carrying your
  ORCID makes the row `protected`, and audit auto-removal needs a foreign `raw_orcid` on all of them.
  With no known ORCID of your own (profile and `--orcid` both empty) no `raw_orcid` can be judged
  foreign, so audit removal candidates stay `review` (PLAN §6.7 read literally would remove them).
- **Title search.** The query is the quoted phrase of §6.6. Near-title hits next to a single
  exact hit become `review(title-near)` rows (another version of the work); every hit, matched or
  not, is listed in the rows' `search_hits` evidence.
- **Audit.** Addition candidates also include works where your ORCID appears (add-table row 3),
  even when the byline fails the gate. Works whose single lookup fails are listed as warnings.
  The cost estimate counts every probe after URL-length splitting.
- **Journal entries** also store `kind`, `previous` and `raw_author_name`: `revert` needs them.
- **Plan order.** Author-level rows come first (ORCID removal before ORCID set, so an ORCID
  revert removes before it sets), then work rows by status and evidence strength.
- **Retries.** "1, 2, 4, 8, 16 s" is read as five retries after the first attempt (six attempts
  at most). A `429` whose `Retry-After` exceeds 60 s raises at once instead of sleeping, which
  mirrors the plan's `retryAfter > 60` budget rule for the body. Without a `Retry-After` header,
  a body `retryAfter` of up to 60 s is honoured.
- **Exit codes.** `2` also covers claims left over by the daily cap and items left unsent when a
  run stops at HTTP level; a run stopped by authentication exits `3`, by rate or budget `4`, and
  both still list the unsent items. A preflight `GET /curations` answering anything but `401` with
  a `4xx` (including `403`) is a note, not an abort: the canary checks authorisation.
- **`--format json` for `apply`** prints one JSON document: counts, results, stale, left-over and
  unsent items, notes, credits and the exit code.
- **Re-validation** also skips a claim whose work now redirects to another ID (a merge).
- **`plan pick`** warns when the chosen byline matches no seed or belongs to a profile with an
  ORCID or more than `--splinter_max` works.
- **DOI lists.** `#` starts a comment only at the start of a line or after whitespace, since
  DOIs may contain `#`.
- **Licence.** `pyproject.toml` declares MIT, matching the committed `LICENSE`; PLAN.md §5 and
  §12 still say the licence is undecided.
