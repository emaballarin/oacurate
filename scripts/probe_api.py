#!/usr/bin/env python3
"""Milestone 0 probe: settle the unknowns U1, U2, U4, U5 and U6 of PLAN.md §3 (needs a key).

Saves redacted raw responses under tests/fixtures/live/ and prints a summary. Costs fewer
than 50 credits. Writes nothing to OpenAlex unless --with-write-sample is given (U2).

Usage (fish):
    set -x OPENALEX_API_KEY (cat ~/.config/openalex/key)
    python scripts/probe_api.py
    python scripts/probe_api.py --with-write-sample --author A…   # U2: needs a claimed profile
"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "fixtures" / "live"
API = "https://api.openalex.org"
USER = "https://user.openalex.org"
UA = "oacurate-probe (+https://github.com/emaballarin/oacurate)"
REDACT_KEYS = {"user_id", "user_name", "email", "user_email", "name"}
RATE_HEADERS = [
    "X-RateLimit-Limit",
    "X-RateLimit-Remaining",
    "X-RateLimit-Credits-Used",
    "X-RateLimit-Credits-Required",
    "X-RateLimit-Reset",
    "X-RateLimit-Cost-USD",
    "Allow",
    "Content-Type",
]


def redact(value: Any, key: str) -> Any:
    """Replace personal fields and any occurrence of the key."""
    if isinstance(value, dict):
        return {k: ("<redacted>" if k in REDACT_KEYS else redact(v, key)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if isinstance(value, str) and key and key in value:
        return value.replace(key, "<key>")
    return value


def record(name: str, resp: httpx.Response, key: str) -> dict[str, Any]:
    """Save one redacted response and return its summary."""
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text[:2000]
    doc = {
        "probed_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "request": f"{resp.request.method} {resp.request.url}".replace(key, "<key>") if key else "",
        "status": resp.status_code,
        "headers": {h: resp.headers[h] for h in RATE_HEADERS if h in resp.headers},
        "body": redact(body, key),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(
        json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return doc


def credits(doc: dict[str, Any]) -> str:
    """The credits header of a recorded response."""
    return doc["headers"].get("X-RateLimit-Credits-Used", "?")


def first_curation_id(body: Any) -> str | None:
    """Find a curation ID anywhere in a GET /curations body."""
    if isinstance(body, dict):
        if str(body.get("id", "")).startswith("cur-"):
            return body["id"]
        values = list(body.values())
    elif isinstance(body, list):
        values = body
    else:
        return None
    for v in values:
        found = first_curation_id(v)
        if found:
            return found
    return None


def main() -> int:
    """Run the probes, save redacted responses, print the summary table."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--api_key_file", default="")
    parser.add_argument("--author", default="", help="your claimed profile (A…), for U2 only")
    parser.add_argument("--with-write-sample", action="store_true", help="U2: POST two items")
    args = parser.parse_args()
    key = (
        Path(args.api_key_file).read_text().strip()
        if args.api_key_file
        else os.environ.get("OPENALEX_API_KEY", "")
    )
    if not key:
        print("set OPENALEX_API_KEY or pass --api_key_file", file=sys.stderr)
        return 1
    rows: list[tuple[str, str, int, str, str]] = []
    headers = {"User-Agent": UA, "Authorization": f"Bearer {key}"}
    with httpx.Client(headers=headers, timeout=30, follow_redirects=False) as http:
        # Auth sanity: a free single lookup with the key.
        doc = record(
            "probe_auth_single_lookup",
            http.get(f"{API}/authors/A5023888391", params={"select": "id"}),
            key,
        )
        rows.append(("auth", "GET /authors/A5023888391 with key", doc["status"], credits(doc), ""))
        if doc["status"] == 401:
            print(
                "the key is rejected by api.openalex.org (401); fix it before probing",
                file=sys.stderr,
            )
            return 3

        # U1: GET /curations shapes and filters.
        u1 = {
            "probe_U1_curations_plain": {},
            "probe_U1_curations_entity_works": {"entity": "works"},
            "probe_U1_curations_entity_authors": {"entity": "authors"},
            "probe_U1_curations_status_pending": {"status": "pending"},
            "probe_U1_curations_works_pending": {"entity": "works", "status": "pending"},
            "probe_U1_curations_page2": {"page": 2, "per_page": 1},
        }
        some_id = None
        for name, params in u1.items():
            doc = record(name, http.get(f"{USER}/curations", params=params), key)
            some_id = some_id or first_curation_id(doc["body"])
            shape = type(doc["body"]).__name__
            if isinstance(doc["body"], dict):
                shape += " keys=" + ",".join(sorted(doc["body"]))[:60]
            rows.append(("U1", f"GET /curations {params}", doc["status"], credits(doc), shape))

        # U4: credits of the three billed query kinds (per_page=1 each).
        u4 = {
            "probe_U4_raw_author_name_search": {"filter": 'raw_author_name.search:"jason priem"'},
            "probe_U4_author_id_filter": {"filter": "author.id:A5023888391"},
            "probe_U4_search_title": {"search.title": "attention is all you need"},
        }
        for name, params in u4.items():
            full = {**params, "per_page": 1, "corpus": "all", "select": "id"}
            doc = record(name, http.get(f"{API}/works", params=full), key)
            rows.append(("U4", str(params), doc["status"], credits(doc), "credits header = answer"))

        # U5: OPTIONS only; never DELETE.
        cid = some_id or "cur-00000000"
        doc = record("probe_U5_options", http.options(f"{USER}/curations/{cid}"), key)
        rows.append(
            (
                "U5",
                f"OPTIONS /curations/{cid}",
                doc["status"],
                "-",
                f"Allow: {doc['headers'].get('Allow', '(none)')}",
            )
        )

        # U6: does the free DOI lookup resolve an expansion-corpus work?
        params = {
            "corpus": "expansion",
            "filter": "has_doi:true",
            "per_page": 1,
            "select": "id,doi",
        }
        doc = record("probe_U6_expansion_sample", http.get(f"{API}/works", params=params), key)
        results = (doc["body"] or {}).get("results") or [] if isinstance(doc["body"], dict) else []
        rows.append(
            (
                "U6",
                "list corpus=expansion has_doi",
                doc["status"],
                credits(doc),
                f"{len(results)} hit",
            )
        )
        if results and results[0].get("doi"):
            doi = results[0]["doi"].removeprefix("https://doi.org/")
            lookup = http.get(f"{API}/works/doi:{quote(doi, safe='/:')}", params={"select": "id"})
            doc2 = record("probe_U6_single_lookup", lookup, key)
            rows.append(
                ("U6", f"GET /works/doi:{doi}", doc2["status"], credits(doc2), "200 = resolves")
            )

        # U2 (opt-in): shape of the 207 items.
        if args.with_write_sample:
            if not args.author:
                print("--with-write-sample needs --author A…", file=sys.stderr)
                return 1
            aid = (
                args.author
                if args.author.startswith("https://")
                else f"https://openalex.org/{args.author}"
            )
            me = http.get(
                f"{API}/authors/{aid.rsplit('/', 1)[1]}", params={"select": "id,display_name"}
            ).json()
            items = [
                {
                    "entity": "authors",
                    "entity_id": aid,
                    "property": "display_name",
                    "action": "replace",
                    "value": me["display_name"],
                },
                {
                    "entity": "authors",
                    "entity_id": aid,
                    "property": "orcid",
                    "action": "replace",
                    "value": "https://orcid.org/0000-0002-1825-0098",
                },
            ]
            doc = record("probe_U2_post_array", http.post(f"{USER}/curations", json=items), key)
            rows.append(
                (
                    "U2",
                    "POST [no-op display_name, bad ORCID]",
                    doc["status"],
                    "-",
                    "see probe_U2_post_array.json",
                )
            )

    width = max(len(r[1]) for r in rows)
    for u, what, status, cost, note in rows:
        print(f"{u:4} {what:<{width}}  {status:>3}  credits {cost:>3}  {note}")
    print(
        f"\nredacted responses saved under {OUT.relative_to(ROOT)}/; review them before committing"
    )
    print("U3 is not probed; U7 needs a question to OpenAlex support (openalex.org/contact)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
