"""Human- and machine-readable output: tables (table | tsv | json), payloads, ticket text."""

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from oacurate.ids import ORCID_PREFIX, short_id
from oacurate.model import WORK_KINDS, Action, UnresolvedInput

Format = Literal["table", "tsv", "json"]
ELLIPSIS = chr(0x2026)


@dataclass(frozen=True)
class Column:
    """One output column; ``width`` truncates it in ``table`` format only."""

    key: str
    header: str
    width: int | None = None


def format_local(dt: datetime | None) -> str:
    """Format a timestamp for people: local time, DD/MM/YYYY HH:mm."""
    return dt.astimezone().strftime("%d/%m/%Y %H:%M") if dt else "-"


def format_age(delta_seconds: float) -> str:
    """Format an age as ``2d 3h`` / ``5h 10m`` / ``7m``."""
    minutes = int(delta_seconds // 60)
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {mins}m" if hours else f"{mins}m"


def _cell(value: Any) -> str:
    """Render one value as a single-line string."""
    if value is None:
        return "-"
    if isinstance(value, list | tuple):
        value = "; ".join(str(v) for v in value)
    return " ".join(str(value).split())


def _truncate(s: str, width: int | None) -> str:
    """Truncate ``s`` to ``width`` characters with an ellipsis."""
    if width is None or len(s) <= width:
        return s
    return s[: max(width - 1, 0)] + ELLIPSIS


def render_rows(rows: Sequence[Mapping[str, Any]], columns: Sequence[Column], fmt: Format) -> str:
    """Render rows as a fixed-width table, TSV, or a JSON array."""
    if fmt == "json":
        return json.dumps(
            [{c.key: r.get(c.key) for c in columns} for r in rows], indent=2, ensure_ascii=False
        )
    if fmt == "tsv":
        lines = ["\t".join(c.key for c in columns)]
        lines += ["\t".join(_cell(r.get(c.key)) for c in columns) for r in rows]
        return "\n".join(lines)
    cells = [[_truncate(_cell(r.get(c.key)), c.width) for c in columns] for r in rows]
    widths = [max([len(c.header), *(len(row[i]) for row in cells)]) for i, c in enumerate(columns)]
    out = ["  ".join(c.header.ljust(w) for c, w in zip(columns, widths, strict=True)).rstrip()]
    out.append("  ".join("-" * w for w in widths))
    out += [
        "  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)).rstrip() for row in cells
    ]
    return "\n".join(out)


def render_mapping(pairs: Iterable[tuple[str, Any]], fmt: Format) -> str:
    """Render key/value pairs (``show``) in the chosen format."""
    rows = [{"field": k, "value": v} for k, v in pairs]
    if fmt == "json":
        return json.dumps({r["field"]: r["value"] for r in rows}, indent=2, ensure_ascii=False)
    return render_rows(rows, [Column("field", "field"), Column("value", "value")], fmt)


# ---------------------------------------------------------------------- plans

PLAN_COLUMNS = (
    Column("status", "status"),
    Column("id", "action"),
    Column("work", "work"),
    Column("year", "year"),
    Column("type", "type", 12),
    Column("title", "title", 40),
    Column("byline", "byline@pos", 28),
    Column("tier", "tier"),
    Column("owner", "owner (id/works/orcid)", 34),
    Column("basis", "basis"),
    Column("reasons", "reasons", 40),
)


def _orcid_short(orcid: str | None) -> str | None:
    """Drop the ``https://orcid.org/`` prefix."""
    return orcid.removeprefix(ORCID_PREFIX) if orcid else None


def plan_row(a: Action) -> dict[str, Any]:
    """Flatten one action into a table row."""
    ev = a.evidence
    if a.kind in WORK_KINDS:
        target, title = ev.get("work") or short_id(a.entity_id), ev.get("title")
    else:
        target, title = a.kind, f"{a.previous or '(none)'} -> {a.value}"
    byline = ev.get("byline") or a.raw_author_name
    owner = ev.get("owner")
    works = ev.get("owner_works_count")
    owner_cell = (
        f"{owner}/{'?' if works is None else works}/{_orcid_short(ev.get('owner_orcid')) or '-'}"
        if owner
        else None
    )
    candidates = ev.get("candidates") or ev.get("bylines")
    reasons = list(a.reasons)
    if candidates and not byline:
        reasons.append("bylines: " + "; ".join(candidates))
    return {
        "status": a.status,
        "id": a.id,
        "decision": a.decision,
        "work": target,
        "year": ev.get("year"),
        "type": ev.get("type"),
        "title": title,
        "byline": f"{byline} @{ev['position']}" if byline and ev.get("position") else byline,
        "tier": ev.get("tier"),
        "owner": owner_cell,
        "basis": ev.get("anchor") if ev.get("anchor") in ("orcid", "pick") else ev.get("basis"),
        "reasons": reasons,
        "input": ev.get("input"),
        "search_hits": ev.get("search_hits"),
    }


def render_plan(actions: Sequence[Action], fmt: Format) -> str:
    """Render the plan table."""
    return render_rows([plan_row(a) for a in actions], PLAN_COLUMNS, fmt)


def status_counts(actions: Iterable[Action]) -> list[tuple[str, int]]:
    """Counts by status, most actionable first."""
    order = (
        "claim",
        "remove",
        "review",
        "blocked",
        "protected",
        "already",
        "not_on_profile",
        "missing",
    )
    counts = Counter(a.status for a in actions)
    return [(s, counts[s]) for s in order if counts[s]]


def payloads_json(actions: Sequence[Action]) -> str:
    """The exact JSON that would be POSTed, one object per action."""
    return json.dumps([a.to_payload() for a in actions], indent=2, ensure_ascii=False)


UNRESOLVED_COLUMNS = (
    Column("reason", "reason"),
    Column("ref", "input", 40),
    Column("detail", "detail", 80),
)


def render_unresolved(items: Sequence[UnresolvedInput], fmt: Format) -> str:
    """Render the inputs that produced no action."""
    rows = [{"reason": u.reason, "ref": u.ref, "detail": u.detail} for u in items]
    return render_rows(rows, UNRESOLVED_COLUMNS, fmt)


def ticket_text(item: UnresolvedInput, author_id: str) -> str:
    """Ticket-ready text for a work that is missing from OpenAlex (openalex.org/contact)."""
    rec = item.record
    title = rec.get("title") or "(no title in the input)"
    doi, arxiv = rec.get("doi"), rec.get("arxiv_id")
    if doi:
        evidence = f"https://doi.org/{doi}"
    elif arxiv:
        evidence = f"https://arxiv.org/abs/{arxiv}"
    else:
        evidence = "(no persistent link in the input: add the publisher's page)"
    authors = rec.get("authors") or []
    author_line = "; ".join(authors) + (" and others" if rec.get("authors_truncated") else "")
    lines = [
        f"Subject: Work missing from OpenAlex: {title}",
        "",
        "The following work cannot be found in OpenAlex, so it cannot be attached to my",
        f"author profile {author_id}.",
        "",
        f"  Title:   {title}",
        f"  Authors: {author_line or '-'}",
        f"  Venue:   {rec.get('venue') or '-'}",
        f"  Year:    {rec.get('year') or '-'}",
        f"  DOI:     {doi or '-'}",
        f"  arXiv:   {arxiv or '-'}",
        "",
        f"What is wrong: the work is missing from OpenAlex ({item.detail}).",
        f"Evidence: {evidence}",
    ]
    return "\n".join(lines)
