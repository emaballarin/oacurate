#!/usr/bin/env python3
"""Record test fixtures from free OpenAlex single lookups (no key needed, 0 credits)."""

import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
BASE = "https://api.openalex.org"
UA = "oacurate-fixture-recorder (+https://github.com/emaballarin/oacurate)"

LOOKUPS = [
    ("works", "W2741809807", "/works/W2741809807"),
    ("works", "W2626778328", "/works/W2626778328"),
    ("works", "W2146272590", "/works/W2146272590"),
    (
        "works",
        "doi_W2146272590",
        "/works/doi:10.1002/%28SICI%291097-0258%2819980430%2917:8%3C857::AID-SIM777%3E3.0.CO%3B2-E",
    ),
    ("works", "doi_arxiv_1706.03762", "/works/doi:10.48550/arxiv.1706.03762"),
    ("authors", "A5023888391", "/authors/A5023888391"),
    ("authors", "orcid_0000-0001-6187-6610", "/authors/orcid:0000-0001-6187-6610"),
    ("works", "doi_404", "/works/doi:10.1234/oacurate-fixture-does-not-exist"),
]


def main() -> int:
    """Record every lookup in ``LOOKUPS``; stop if any is billed."""
    total = 0.0
    with httpx.Client(headers={"User-Agent": UA}, timeout=30, follow_redirects=True) as http:
        for kind, name, path in LOOKUPS:
            if "://" in path:
                raise SystemExit(f"refusing billed URL-form lookup {path}")
            resp = http.get(BASE + path)
            used = float(resp.headers.get("X-RateLimit-Credits-Used", "0") or 0)
            total += used
            if used:
                raise SystemExit(f"{path} was billed {used} credits; stopping")
            out = FIXTURES / kind / f"{name}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            body = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else resp.text
            )
            record = body if resp.status_code == 200 else {"status": resp.status_code, "body": body}
            out.write_text(
                json.dumps(record, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            print(f"{resp.status_code} {path} -> {out.relative_to(ROOT)} (credits {used:g})")
    print(f"total credits used: {total:g}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
