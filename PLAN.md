# oacurate — implementation plan

Status: **plan only, no code yet.** Target repository: `github.com/emaballarin/oacurate`.
Written 13/10/2026, to be implemented by Claude Code. Section 2 is the ground truth the
implementation builds on; section 3 lists what is still unknown and how to settle it.

---

## 1. Purpose and scope

`oacurate` is a command-line tool that lets any author curate their own OpenAlex author
profile through the official curation API:

- set or detach the profile's ORCID;
- set the display name and the match name (`full_name`);
- attach works from DOIs, OpenAlex work IDs, or BibTeX (including Google Scholar exports,
  which carry no DOIs);
- detach works that are not the author's;
- audit the profile for missing and wrongly attached works, and for duplicate profiles;
- track submitted curations and build revert plans.

**Safety model.** Work-level changes are two-phase: a command builds a reviewable plan file,
and only `apply` submits it. Every submitting command is a dry run unless `--yes` is given.
Every submitted item is journalled.

### Non-goals (v1)

- Claiming the profile itself. That is web-only: openalex.org → author page → **Claim**.
  A verified institutional e-mail on the account gives automatic approval.
- Adding works that do not exist in OpenAlex. That needs a ticket at openalex.org/contact;
  the tool prints ticket-ready text for such inputs.
- Affiliation curation (it is scoped to member institutions).
- Scraping Google Scholar. The input is the author's own Scholar BibTeX export.
- Scholar CSV input: its column names are unverified, so it is deferred.
- Writing to ORCID.

---

## 2. Ground truth (verified 13/10/2026)

Provenance: **probe** = live request from a sandbox (free endpoints only);
**docs** = help.openalex.org page named in brackets. Do not deviate from these without
re-verifying, and record any change in `docs/api-notes.md`.

### 2.1 Curation API (`user.openalex.org`)

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

### 2.2 Read API (`api.openalex.org`)

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

### 2.3 Libraries and tooling

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

---

## 3. Unknowns — Milestone 0 (needs an API key; the maintainer runs it)

| ID  | Unknown                                                                                                   | How it gets settled                                                                                                                                                                                                                                                                           |
| --- | --------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| U1  | `GET /curations` response shape, accepted filters (`entity`, `status`), pagination                        | `scripts/probe_api.py`                                                                                                                                                                                                                                                                        |
| U2  | Shape of each item inside a `207`                                                                         | Opt-in `--with-write-sample`: POST an array of (a) a `display_name` replace with the profile's **current** display name (a no-op success sample; it does create a curation row) and (b) an ORCID set with a deliberately wrong check digit (a guaranteed `400` item). Needs a claimed profile |
| U3  | Maximum array size per POST                                                                               | Not probed. Default chunk size 25; revisit only if `413`/`400` appears                                                                                                                                                                                                                        |
| U4  | Credits charged for `raw_author_name.search` (1 or 10?), for `filter=author.id:…`, and for `search.title` | Read `X-RateLimit-Credits-Used` on one `per_page=1` request each                                                                                                                                                                                                                              |
| U5  | Whether pending curations can be cancelled via the API (the web UI offers cancel)                         | Send `OPTIONS /curations/<id>` only and read `Allow`. **Never send `DELETE` blindly**                                                                                                                                                                                                         |
| U6  | Whether `/works/doi:` resolves expansion-corpus works                                                     | `corpus=expansion&filter=has_doi:true&per_page=1` (1 credit), then a single lookup of that DOI                                                                                                                                                                                                |
| U7  | How a claim and a remove interact when both exist for the same (work, author)                             | Cannot be probed safely. Ask OpenAlex support and record the answer in `docs/api-notes.md`                                                                                                                                                                                                    |

The probe script saves redacted raw responses under `tests/fixtures/live/`, prints a summary,
and costs fewer than 50 credits. Until U1 and U2 are settled, response parsers store the raw
JSON and classify unrecognised shapes as `unknown`, **never as success**.

---

## 4. Conventions

- Python ≥ 3.14; a `uv` project with `src/` layout; build backend `uv_build`; installable
  with `uv tool install`.
- Runtime dependencies: `httpx>=0.28,<1`, `bibtexparser>=2.1,<3`, `simple-parsing>=0.1.9`.
  Development: `pytest`, `ruff`. Nothing else without discussion. Note every new dependency
  in the README.
- Ruff for formatting and linting: `target-version = "py314"`; rules
  `E, F, I, UP, B, SIM, RUF`, plus `S101` (no `assert`) on `src/` only.
- Modern type hints (`X | None`, `collections.abc`). A one-line `"""docstring"""` on every
  module, class and function. British English in docs, comments and messages.
- **The tool must behave identically under `python -O`.** No `assert` for runtime checks:
  raise explicit exceptions from the `OacurateError` hierarchy. `assert` is for tests only.
- Library modules never `print`; only `cli.py` and `render.py` produce output.
- Human-facing dates use DD/MM/YYYY HH:mm (24-hour, local time); files use ISO 8601 UTC.
- Plan files are written in a stable sort order so that diffs between runs are meaningful.

---

## 5. Repository layout

```
oacurate/
  pyproject.toml
  README.md
  LICENSE                       # TODO: licence not chosen yet — maintainer decides
  docs/api-notes.md             # copy of §2 + Milestone 0 findings; kept current
  scripts/probe_api.py          # Milestone 0, maintainer-run, needs a key
  scripts/record_fixtures.py    # free single lookups only
  src/oacurate/
    __init__.py                 # __version__
    __main__.py                 # python -m oacurate
    errors.py                   # exception hierarchy, exit-code mapping
    ids.py                      # DOI / ORCID / OpenAlex ID / arXiv normalisation
    names.py                    # byline tokenising, seeds, matching gate
    client.py                   # HTTP: read API + curation API, auth, retries, credit ledger
    model.py                    # Action, Plan, JournalEntry, light views of Work/Author
    bib.py                      # BibTeX / DOI list / W list → InputRecord
    resolve.py                  # InputRecord → candidate works
    classify.py                 # (work, author) → status + evidence
    audit.py                    # missing / misattributed works, duplicate profiles
    apply.py                    # preflight, re-validation, cap, canary, chunking, outcomes
    journal.py                  # append-only JSONL, pending reconciliation, revert plans
    render.py                   # tables (table|tsv|json), ticket text
    cli.py                      # simple_parsing commands → functions
  tests/
    fixtures/{works,authors,lists,curations,live}/
    test_ids.py  test_names.py  test_bib.py  test_resolve.py  test_classify.py
    test_audit.py  test_apply.py  test_journal.py  test_client.py  test_cli.py
```

---

## 6. Module specifications

### 6.1 `ids.py`

- `normalise_doi(s) -> str`
    1. Trim whitespace. Drop a leading `doi:` or `http(s)://(dx.)doi.org/` (case-insensitive).
    2. Strip trailing `.,;`, and closing brackets only when they are unbalanced.
    3. Lower-case. Require `^10\.\d{4,9}/\S+$`; otherwise raise `InvalidIdentifier`.
- `doi_path(doi) -> str`: `"doi:" + quote(doi, safe="/:")`.
- `normalise_orcid(s) -> str` returns the canonical `https://orcid.org/XXXX-XXXX-XXXX-XXXX`.
    - Accept bare, unhyphenated, `http`/`www`, and trailing-slash forms; upper-case `X`.
    - Verify the ISO 7064 MOD 11-2 check character locally, before any request.
    - Algorithm: `t = 0; for d in first 15 digits: t = (t + d) * 2; r = (12 - t % 11) % 11;
check = "X" if r == 10 else str(r)`.
    - Valid test vectors: `0000-0002-1825-0097`, `0000-0001-6187-6610`,
      `0000-0002-0889-9220`, `0000-0002-1694-233X`.
    - Invalid test vectors: `0000-0002-1825-0098`, and the transposition `0000-0002-1825-0079`.
- `normalise_openalex_id(s, kind: Literal["W", "A"]) -> tuple[str, str]` returns
  `(short, long)`. Accept `A123`, `a123`, `openalex.org/A123`, `https://openalex.org/A123`;
  reject the wrong kind.
- `arxiv_id_from(fields) -> str | None`.
    - Recognise new-style IDs (`\d{4}\.\d{4,5}`, optional `vN`, which is stripped) and
      old-style IDs (`[a-z-]+(\.[A-Z]{2})?/\d{7}`).
    - Look in `eprint` (with `archiveprefix = arXiv`), in `journal` (e.g.
      `arXiv preprint arXiv:2401.01234`), and in `url`/`howpublished` (`arxiv.org/abs|pdf/…`).
- `arxiv_doi(aid) -> str`: `"10.48550/arxiv." + aid.lower()`.

### 6.2 `names.py`

This re-implements the byline logic of OpenAlex's "Audit a profile" recipe; cite
`help.openalex.org/tutorials/audit-a-profile` in the module docstring.

- `name_tokens(name) -> list[str]`
    - NFD, drop combining marks, lower-case.
    - Map `-` `.` `'` and unicode dashes to spaces. This departs from the recipe, which
      deletes them and so fuses hyphenated surnames such as "García-Martínez".
    - Keep `[a-z, ]`. Treat `Last, First` as `First Last`. Split on whitespace.
- `Seeds` is the set of name forms a byline is compared against:
    - the profile's `display_name`;
    - `raw_author_names` and `display_name_alternatives` from the author lookup (free), each
      kept only if its surname token equals the `display_name` surname (these fields can carry
      other people's names);
    - every `--name` form given on the command line. These are always kept; this is how
      compound surnames, name changes and transliterations get handled.
- `match_tier(raw_name, seeds) -> Literal["full", "initial", "none"]`
    - Surname equal **and** first given token equal → `full`.
    - Surname equal **and** first given tokens initial-compatible → `initial`.
    - A seed with a single token can only ever reach `initial`.
    - Best tier over all seeds wins.
- `matching_authorships(work, seeds) -> list[tuple[authorship, tier]]`, tier `!= "none"` only.
- Weak spots, documented in the README: CJK names, transliteration variants, name changes,
  single-name authors, `First, Last` bylines with a stray comma. The mitigation is explicit
  `--name` seeds plus ORCID evidence.

### 6.3 `client.py`

- `OpenAlexClient(api_key: str | None, *, base="https://api.openalex.org",
user_base="https://user.openalex.org", max_rps=10, timeout=30.0)`.
- One `httpx.Client`. Send `Authorization: Bearer` when a key is present, and
  `User-Agent: oacurate/<version> (+https://github.com/emaballarin/oacurate)`.
- Read methods:
    - `get_work(ref, select)` and `get_author(ref, select)`: single lookups using only
      `/works/W…`, `/works/doi:…`, `/authors/A…`, `/authors/orcid:…`. They return `None` on
      `404`.
    - `count_works(filter, search=None)`: `per_page=1`, billed.
    - `iter_works(filter, *, search=None, select, corpus="all")`: cursor paging,
      `per_page=100`, billed per page.
    - `list_authors(filter, select)`: billed.
- Curation methods: `post_curations(items) -> list[ItemOutcome]`, `get_curations(**params)`,
  and `options_curation(cid)` (used by Milestone 0 only).
- **Invariant, enforced by a test:** no request path contains `://` after `/works/` or
  `/authors/`, and every `/works` list request carries `corpus=all`.
- Credit ledger.
    - Accumulate `X-RateLimit-Credits-Used`; expose `credits_used`, `remaining`, and
      `reset_at` (local time).
    - A `CreditBudget(max_credits)` check runs **before** every billed request, using the
      estimated cost (1 for a list, 10 for a search; refined by U4). Going over raises
      `BudgetExceeded` without sending the request.
- Retries.
    - Network errors and `5xx`: back off 1, 2, 4, 8, 16 s (at most 5 tries).
    - `429` with a body showing budget exhaustion (`dailyRemainingUsd == 0`, or
      `retryAfter > 60`): raise `BudgetExhausted(reset_at)` immediately, no retry.
    - Any other `429`: honour `Retry-After`, up to 3 times.
    - Other `4xx`: no retry; raise a typed error (`BadRequest(body)`, `Unauthorized`,
      `Forbidden`).
- Never log the key. With `--verbose`, the HTTP log redacts `Authorization`.
- Single lookups are memoised in memory for the length of a run.

### 6.4 `model.py`

All of these are dataclasses extending `Serializable`.

- **`Action`**
    - `id`: a stable short hash of `(kind, entity_id, value, raw_author_name)`.
    - `kind: Literal["claim_work", "remove_work", "set_orcid", "remove_orcid",
"set_display_name", "set_full_name"]`.
    - `entity_id`, `value`, `raw_author_name: str | None`.
    - `previous: str | None`: the prior display name, full name or ORCID, kept for revert.
    - `status: Literal["already", "claim", "remove", "review", "blocked", "protected",
"not_on_profile", "missing"]`.
    - `decision: Literal["apply", "skip"]` and `reasons: list[str]`.
    - `evidence: dict`: title, year, type, venue, byline, author position, tier, `raw_orcid`,
      current owner ID / works_count / ORCID, co-author overlap, match basis (`doi`, `arxiv`,
      `title-exact`, `title-near`, `work-id`, `audit`), and the input reference (e.g.
      `bib:ballarin2024koopman`).
    - `to_payload() -> dict` builds exactly the JSON in §2.1.
- **`Plan`**: `schema_version = 1`, `tool_version`, `created_utc`, `author_id` (long form),
  `author_display_name`, `seeds`, `actions`, `unresolved: list[UnresolvedInput]`,
  `warnings`, `credits_used`.
- `Plan.validate()` runs explicitly on every load:
    - `Literal` membership;
    - ID formats;
    - claim payloads match the server regex;
    - `author_id` matches the author passed to the command.
    - Do not rely on the serialiser for any of this.
- **`JournalEntry`**: `ts_utc`, `action_id`, `payload`,
  `http_status`, `outcome: Literal["created", "duplicate", "rejected", "unknown"]`,
  `curation_id | None`, `message`, `raw` (the response item as received).

### 6.5 `bib.py`

- **BibTeX**: parse with bibtexparser 2.x plus `LatexDecodingMiddleware`. Report every
  `failed_blocks` entry (line and reason) as an unresolved input; never drop one silently.
  Duplicate keys are reported too.
- **`InputRecord`** fields:
    - `key`, `entry_type`, and `title` (decoded, leftover braces stripped);
    - `year`, `doi` (normalised; also taken from a `url` pointing at doi.org), `arxiv_id`;
    - `venue` (journal or booktitle), `authors`, and `authors_truncated` (true when the list
      ends in `and others`).
- **Other inputs**: plain DOI lists (one per line, `#` comments allowed) and OpenAlex `W…`
  lists.
- **Title normalisation, `norm_title`** (shared with `resolve.py`): NFKD, drop combining
  marks, casefold, map unicode dashes and quotes to ASCII, delete `{}`, `$` and `\`, turn
  non-alphanumerics into spaces, collapse whitespace.

### 6.6 `resolve.py`

Resolution order per input record:

1. **DOI**: `get_work(doi)` (free). If found, the basis is `doi`.
    - A different returned `doi` is expected and fine (see §2.2).
    - If it returns `404`, go to step 2 if an arXiv ID exists, else to step 3.
2. **arXiv**: `get_work(arxiv_doi)` (free). If found, the basis is `arxiv`.
3. **Title search** (billed, 10 credits). Skipped entirely with `--no_search`.
    - Request: `search.title="<norm_title tokens>"`, `filter=type:!paratext`, `corpus=all`,
      `per_page=10`, a minimal `select`.
    - Before any search runs, print the estimated cost (title-only records × 10) and stop if
      it exceeds `--max_credits`.
    - Score each hit:
        - `norm_title(hit) == norm_title(input)` → `title-exact`;
        - otherwise a `difflib.SequenceMatcher` ratio ≥ 0.92 → `title-near`;
        - otherwise discard the hit.
    - Year is recorded as evidence, never used as a filter.
4. For each resolved work ID, run `get_work(W)` as a **full** single lookup (free; full
   authorship list), then classify it (§6.7).

How the outcomes map to statuses:

| Outcome                                                                                                                                  | Status                                                                           |
| ---------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| Single exact-title hit whose byline passes the gate                                                                                      | classified normally (§6.7)                                                       |
| More than one exact-title hit (e.g. a preprint and the proceedings version as separate works, or an unrelated paper with the same title) | every hit is classified, then capped at `review` with reason `multiple-versions` |
| Near-title hit only                                                                                                                      | `review`                                                                         |
| No hit                                                                                                                                   | `missing`, with ticket text                                                      |

### 6.7 `classify.py`

**Inputs**: the full work, the target author (ID, ORCID, works_count), the seeds, and
`my_orcid` (the profile's ORCID, or `--orcid`).

**Adding a work.** The first matching row wins:

| #   | Condition                                                                                                                   | Status                               | Default decision |
| --- | --------------------------------------------------------------------------------------------------------------------------- | ------------------------------------ | ---------------- |
| 1   | Some authorship's `author.id` is the target                                                                                 | `already`                            | skip             |
| 2   | `work.is_paratext`                                                                                                          | `review` (`paratext`)                | skip             |
| 3   | An authorship's `raw_orcid == my_orcid`, or its `author.orcid == my_orcid` on another profile (that profile is a duplicate) | `claim` (basis `orcid`)              | apply            |
| 4   | No authorship passes the gate                                                                                               | `review` (`no-byline`)               | skip             |
| 5   | More than one authorship passes the gate                                                                                    | `review` (`ambiguous`)               | skip             |
| 6   | Tier is `initial`, **and not** (co-author overlap ≥ `--coauthor_min` (3) with the owner absent or a splinter)               | `review` (`initial-only`)            | skip             |
| 7   | The authorship's owner is `None`                                                                                            | `claim`                              | apply            |
| 8   | The owner has an ORCID different from `my_orcid`                                                                            | `review` (`conflicting-orcid`)       | skip             |
| 9   | The owner's `works_count ≤ --splinter_max` (3)                                                                              | `claim`                              | apply            |
| 10  | Otherwise (the owner is a larger profile)                                                                                   | `review` (`owned-by-larger-profile`) | skip             |

- **Post-rule:** any `claim` whose `raw_author_name` occurs on more than one authorship of
  the work becomes `blocked` (`duplicate-byline-anchor`).
- A retracted work only gets a note in `reasons`; its status is unchanged.
- Fetching the owner author is a free lookup.
- The thresholds above are command-line flags.

**Removing a work:**

| Condition                                                                  | Status                                 | Default decision |
| -------------------------------------------------------------------------- | -------------------------------------- | ---------------- |
| The target is not on any authorship                                        | `not_on_profile`                       | skip             |
| The target authorship's `raw_orcid == my_orcid`                            | `protected` (refused unless `--force`) | skip             |
| Explicit input to `plan remove`                                            | `remove`                               | apply            |
| Found by the audit, and `raw_orcid` is present and differs from `my_orcid` | `remove`                               | apply            |
| Found by the audit, otherwise                                              | `review`                               | skip             |

### 6.8 `audit.py`

The algorithm follows the OpenAlex "Audit a profile" recipe, extended with scoring.

- **A. Profile works.** Fetch
  `filter=author.id:<A>,type:!paratext`, `corpus=all`, `per_page=100`, `cursor=*`, with a
  minimal `select` including `authorships`. Build the on-profile set and a co-author ID
  multiset (excluding the target).
- **B. Removal candidates.**
    - For each profile work, find the target authorship. If the list response returned 100
      authorships, re-fetch the work with a free single lookup first.
    - If its `raw_author_name` fails the gate against all seeds, it is a removal candidate.
    - Classify it with the removal table in §6.7.
- **C. Addition candidates.**
    - Phrases are the recipe's ladder forms built over the vetted seeds:
        - step 1: the typed form;
        - step 2: comma-reversed forms, plus forms with middle names dropped;
        - step 3: first-initial forms;
        - steps 4 and 5: the same phrases with `~1` and `~2`.
    - Each rung is one OR'd `raw_author_name.search` value, plus `type:!paratext` and
      `corpus=all`. If the URL would exceed 3.5 KB, split the OR list and union the results.
    - Probe each rung with `count_works` and stop at the first whose count is at least
      `--threshold` (100), or at `--max_step` (5).
    - Page through the chosen rung. Drop works already on the profile. Keep only works with an
      authorship that passes the gate.
    - Re-fetch each survivor with a full single lookup (free), then classify it with the add
      table in §6.7, passing the co-author overlap with the profile.
- **D. Duplicate profiles.** If `my_orcid` is known, query `/authors?filter=orcid:<orcid>`
  (1 credit). Any other profile found is reported. With `--merge_duplicates`, its works
  (`filter=author.id:<dup>`) become addition candidates and go through §6.7 like any other.
- **E. Cost.** Before anything billed runs, print an estimate:
  `ceil(works_count / 100)` pages + ladder probes × (1 or 10, from U4) + candidate pages.
  Enforce `--max_credits` (default 200), and report the real total at the end.
- **F. Output.** A plan containing both claim and remove actions, sorted by status and then
  by evidence strength, plus a summary table.

### 6.9 `apply.py`

1. **Load.** Load and validate the plan; refuse it if its author differs from the one
   configured.
2. **Select.** Take actions with `decision == "apply"`, add `--accept <action-id|W…>`, and
   drop `--reject <…>`. Print the table of what will be sent.
3. **Dry run.** Without `--yes`, print the exact JSON payloads and exit `0`, sending nothing.
4. **Preflight.** Require a key, then call `GET /curations` (an auth check, and seed data for
   reconciliation). On `401`, raise `Unauthorized` with a hint to check the key.
5. **Re-validate** every work action against a fresh single lookup (free). This guards
   against stale plans.
    - Claim: the `raw_author_name` is still present, the work is still not on the profile,
      and the owner is unchanged since planning. Otherwise skip it with a warning.
    - Remove: the work is still on the profile.
    - Author-level actions: re-read the author to refresh `previous`.
6. **Daily cap.** Count claims journalled in the last 24 h (outcome `created` or
   `duplicate`) plus this batch, and never exceed 1,000. Submit up to the cap and report how
   many are left over and when the window frees up.
7. **Canary.** Send the first action alone, as a single-object POST.
    - `401` or `403`: abort everything. Explain the likely causes: the profile isn't claimed,
      the claim is still pending approval, or the account is wrong.
    - `400`: journal it as `rejected` and continue.
8. **Chunks.** Send the rest in arrays of `--chunk` (25) and parse each item's outcome.
    - A chunk failing at HTTP level is retried once; if it fails again, its items are marked
      `unknown` and the run stops.
    - A `duplicate` counts as success but is listed separately.
9. **Journal.** Write each item to the journal (JSONL, flushed and fsynced) as soon as its
   response arrives.
10. **Summary.** Print counts by outcome and the credits used.
11. **Exit code.** `0` if everything was `created` or `duplicate`, `2` if anything was
    `rejected` or `unknown`, `3` for auth errors, `4` for budget errors.

### 6.10 `journal.py`

- Location: `$XDG_DATA_HOME/oacurate/<author-short-id>.jsonl` (default
  `~/.local/share/oacurate/`); override with `--journal`. Append-only.
- **`pending`**: reconcile the server's `GET /curations` response (filters from U1) with the
  journal by curation ID, and print a table:
    - applied (with `applied_at`);
    - pending (with age);
    - in the journal but missing on the server;
    - on the server but not in the journal.
- **`revert`**: builds an _inverse plan_, which still has to go through `apply`.

    | Original                 | Inverse                                                                  |
    | ------------------------ | ------------------------------------------------------------------------ |
    | claim                    | remove                                                                   |
    | remove                   | claim, using the stored `raw_author_name`                                |
    | ORCID set                | ORCID remove (same value), plus ORCID set to `previous` if there was one |
    | ORCID remove             | ORCID set                                                                |
    | display name / full name | the stored `previous` value                                              |

    The inverses between claim and remove carry a printed caveat until U7 is answered. If U5
    turns up a cancel endpoint, `revert` cancels still-pending curations instead of inverting
    them.

### 6.11 `render.py`

- Tables in `table` (fixed-width, titles truncated), `tsv` or `json` format, with no extra
  dependencies.
- Plan tables show, per action:
    - the status and the action ID;
    - the work ID, year and type;
    - the title (truncated);
    - the byline and its position, plus the tier;
    - the current owner (ID / works_count / ORCID);
    - the basis and the reasons.
- Ticket text for `missing` inputs: the record (DOI, title, venue, year), what is wrong (the
  work is missing from OpenAlex), and an evidence URL. This follows the OpenAlex guidance on
  writing a good ticket.

---

## 7. Command line

Built on `simple_parsing`. The top-level `Cli` dataclass has
`command: … = subparsers({...})`, nested two levels deep.

Because options from a separately added dataclass must precede the subcommand (§2.3),
**every leaf command dataclass inherits `CommonArgs`**, so its options work after the leaf
command. A CLI test must confirm this.

Also to verify with tests during M1:

- dashed option names via
  `ArgumentParser(add_option_string_dash_variants=DashVariant.DASH)` (both names exist in
  simple-parsing 0.1.9; the behaviour is not yet tested);
- repeatable list options (`list[str] = field(default_factory=list)`).

If either doesn't work as expected, keep underscores and document them in the README. Don't
invent a workaround.

`CommonArgs` fields:

| Field          | Default   | Notes                                                                                                                              |
| -------------- | --------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `author`       | `""`      | Falls back to env `OACURATE_AUTHOR`; required in the end                                                                           |
| `api_key_file` | `""`      | Otherwise env `OPENALEX_API_KEY`. **There is deliberately no `--api_key` option.** Warn if the file is readable by group or others |
| `journal`      | `""`      |                                                                                                                                    |
| `format`       | `"table"` | `Literal["table", "tsv", "json"]`                                                                                                  |
| `max_credits`  | `200`     |                                                                                                                                    |
| `verbose`      | `False`   |                                                                                                                                    |

```
oacurate show                                   # profile summary + journal status; 0 credits
oacurate orcid set <ORCID> [--force] [--yes]    # local checksum; checks /authors/orcid: (free)
oacurate orcid remove <ORCID> [--yes]
oacurate name display <NAME> [--yes]
oacurate name match <NAME> [--force] [--yes]    # must be in raw_author_names unless --force
oacurate plan add  [--doi D…] [--dois_file F] [--work W…] [--bib F…] [--name N…]
                   [--orcid O] [--no_search] [--splinter_max 3] [--coauthor_min 3] -o PLAN
oacurate plan remove [--doi D…] [--work W…] [--force] -o PLAN
oacurate plan show PLAN [--status S…]
oacurate plan pick PLAN <action-id> --position N   # choose the byline by author position
oacurate audit [--name N…] [--orcid O] [--threshold 100] [--max_step 5]
               [--merge_duplicates] -o PLAN
oacurate apply PLAN [--accept ID…] [--reject ID…] [--chunk 25] [--yes]
oacurate pending
oacurate revert [--since DD/MM/YYYY | --ids CUR…] -o PLAN
```

Notes on individual commands:

- **Single author-level commands** (`orcid`, `name`) build a one-action plan in memory and
  pass it through the same apply path: dry run by default, `--yes` to submit, journalled.
- **`orcid set`** checks the ORCID before submitting.
    - If `/authors/orcid:<id>` resolves to a _different_ profile, warn that it is probably a
      duplicate. Setting the ORCID won't merge them; suggest `audit --merge_duplicates`.
    - If the profile already has a different ORCID, refuse unless `--force`, and record
      `previous`.
- **`plan pick`** fixes `ambiguous` and `no-byline` rows by hand. It sets `raw_author_name`
  from the authorship at the given position (1-based, as displayed), re-runs the post-rule
  and sets the row's decision to apply.

**Exit codes:** `0` OK · `1` usage or validation · `2` partial failure on apply · `3` auth
(401/403) · `4` budget (429 or `--max_credits`).

---

## 8. Security

- The API key is the openalex.org sign-in credential (§2.2), so handle it like a password:
    - read it only from the environment or a file;
    - never take it as a command-line argument, never put it in a URL, never log it;
    - never write it into plan or journal files.
- Plan files contain no secrets. The journal contains curation IDs and the `user_name`
  echoed back by the server; mention that in the README.
- No shell-outs and no `eval`. BibTeX input is untrusted text: parse it, never execute it.

---

## 9. Testing

- The default `pytest` run makes **no network calls** (`httpx.MockTransport`).
    - An opt-in `-m live` marker runs smoke tests against free single lookups only.
    - Billed endpoints are never hit by tests.
- Fixtures recorded by `scripts/record_fixtures.py` (free single lookups):
    - `W2741809807`: clean bylines;
    - `W2626778328`: matched via a secondary DOI, odd year, 11 locations;
    - `W2146272590`: a DOI with special characters;
    - `A5023888391`: an author with an ORCID and `raw_author_names`;
    - `authors/orcid:0000-0001-6187-6610`;
    - one `404` DOI.
- Synthetic fixtures (marked synthetic):
    - two "J. Smith" bylines on one work;
    - an identical raw name on two authorships;
    - a list response with 100 authorships for a work that has more;
    - `207` mixed outcomes, in the U2 shape once known;
    - a `429` budget-exhaustion body;
    - a Scholar-style `.bib` with a broken entry, LaTeX escapes, `and others`, an arXiv
      `journal` field, and a title containing `--`.

Must-have tests (each one pins a pre-mortem failure, §11):

| Area     | Test                                                                                                                                                                                                              |
| -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| payload  | Claim property strings are byte-exact for `George "Vern" Yocum`, `O'Brien` and a diacritic name; survive a `json` round trip; match the server regex                                                              |
| classify | One test per row of each table in §6.7, plus the post-rule                                                                                                                                                        |
| ids      | DOI normalisation table; ORCID vectors; arXiv new-style, old-style and version stripping                                                                                                                          |
| bib      | Failed blocks are reported; LaTeX decoding works; `and others` is flagged; `norm_title` handles en dashes                                                                                                         |
| client   | Single-lookup paths never use the URL form; every `/works` list request has `corpus=all`; the key never appears in URLs or logs; a budget-exhaustion `429` raises with no retry; `5xx` backoff (with mocked time) |
| apply    | Dry run sends nothing; a canary `403` aborts; a mixed `207` gives exit 2; the 24 h cap holds; stale-plan rows are skipped                                                                                         |
| cli      | Options work after the leaf subcommand; there is no `--api_key` option                                                                                                                                            |

---

## 10. Milestones

Each milestone ends with `uv run ruff format --check`, `uv run ruff check` and `uv run pytest`
all green, and `docs/api-notes.md` updated if any fact changed.

| M   | Content                                                                                                        | Acceptance                                                                                          |
| --- | -------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| M0  | `scripts/probe_api.py` (maintainer-run); commit redacted fixtures; update §3                                   | Unknowns U1–U6 answered or marked unanswerable; U7 question sent to OpenAlex                        |
| M1  | `pyproject`, `errors`, `ids`, read side of `client`, `model`, `cli` skeleton, `show`                           | `oacurate show --author A5023888391` prints the profile with 0 credits used; CLI parsing tests pass |
| M2  | `names`, `resolve` (DOI and W inputs), `classify`, `plan add`, `plan show`, `plan pick`                        | A plan built from a DOI list uses 0 credits; every classify row is tested                           |
| M3  | `bib`, arXiv path, title search, budget guard                                                                  | A dry run on a real Scholar export prints status counts and credits used                            |
| M4  | `apply`, `journal`, `orcid`, `name` (the `207` parser is finalised after M0)                                   | Dry runs print exact payloads; the maintainer sets the ORCID on his own profile live                |
| M5  | `plan remove`, `pending`                                                                                       | `pending` reconciles the journal with the server                                                    |
| M6  | `audit` (A–F)                                                                                                  | Audit of the maintainer's profile within `--max_credits`; candidates reviewed by hand               |
| M7  | `revert` (and cancel, if U5 finds an endpoint)                                                                 | Inverse plan for a test claim shown in a dry run                                                    |
| M8  | README (install, quick start, safety model, costs, limitations), GitHub Actions CI (Python 3.14, ruff, pytest) | CI green                                                                                            |

M1–M3 can proceed without M0. M4's live step and M5 need U1/U2.

---

## 11. Pre-mortem

| #   | Silent failure                                                                                                                   | Why it is plausible                                                                                                                          | Minimal diagnostic                                                                                                                                                                          |
| --- | -------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | **The wrong byline is anchored**, so a co-author's authorship is attached to the profile. Claims are sticky                      | Same surname and initial on one paper; initial-only matches ("D. Sutcliffe" vs "David L. Sutcliffe" was ~17% of OpenAlex's own false merges) | Every plan row shows byline, position, tier, owner and basis; `ambiguous`/`initial-only` never auto-apply; apply re-validates against fresh data; a classify test for each row              |
| 2   | **Title search lands on the wrong work**: a preprint instead of the proceedings version, a same-title paper, or an erratum       | Scholar BibTeX has no DOIs; versions are sometimes separate works; years are unreliable (§2.2)                                               | Auto-claim only on a single exact-title hit whose byline passes the gate; all hits listed; year never gates; compare `claim` vs `review` counts on a real Scholar export before any `--yes` |
| 3   | **Partial failure read as success**: per-item `207` errors, the 1,000/day cap hit mid-batch, a stale row returned as a duplicate | Batch APIs fail item by item; the `207` shape is still unverified                                                                            | Per-item journal written immediately; unknown shapes become `unknown`, never success; exit code 2; `pending` reconciles the journal with the server                                         |

Further risks are handled where they arise:

- **Budget silently burned** by URL-form single lookups: client invariant test (§6.3).
- **Expansion works missed** when `corpus=all` is absent: client invariant test.
- **Stale plans**: re-validation in apply step 5.
- **`python -O` stripping assertions**: no `assert` in `src/`, enforced by ruff `S101`.
- **Polluted seeds** from `raw_author_names`: surname vetting (§6.2).

---

## 12. Decision log

| Date       | Decision                                                                                                                         |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------- |
| 13/10/2026 | Python ≥ 3.14; CLI with `simple_parsing`; audit included in v1; repository `emaballarin/oacurate`                                |
| 13/10/2026 | Synchronous `httpx`; inputs are BibTeX, DOI lists and W lists (Scholar CSV deferred); two-phase plan → apply; dry run by default |
| 13/10/2026 | Single lookups only via the `doi:` / `orcid:` / ID path forms (free); every list query uses `corpus=all`; year is never a gate   |
| 13/10/2026 | Name tokenising maps hyphens and apostrophes to spaces instead of deleting them (a deliberate change from the OpenAlex recipe)   |

**Explicitly open:** the licence, U1–U7, and whether the default chunk size of 25 should
change once U3 is known.
