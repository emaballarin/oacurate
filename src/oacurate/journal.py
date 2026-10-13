"""Append-only JSONL journal of submitted curations, pending reconciliation, revert plans."""

import json
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from oacurate.errors import UsageError
from oacurate.ids import short_id
from oacurate.model import Action, JournalEntry, Plan, parse_iso_utc

SUCCESS = ("created", "duplicate")
U7_CAVEAT = (
    "how a claim and a remove interact for the same (work, author) is undocumented (U7): "
    "a revert may leave both curations on file; check the work after the next cycle"
)
_LIST_KEYS = ("results", "items", "data", "curations")


def default_journal_path(author_short: str) -> Path:
    """Return ``$XDG_DATA_HOME/oacurate/<author>.jsonl`` (default ``~/.local/share``)."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "oacurate" / f"{author_short}.jsonl"


class Journal:
    """An append-only JSONL file; every line is one ``JournalEntry``."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        """Bind to ``path``; nothing is created until the first append."""
        self.path = Path(path)

    def append(self, entry: JournalEntry) -> None:
        """Append one entry, flushed and fsynced before returning."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        line = json.dumps(entry.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    def read(self) -> tuple[list[JournalEntry], list[str]]:
        """Return every readable entry plus a warning for each unreadable line."""
        if not self.path.exists():
            return [], []
        entries, warnings = [], []
        with self.path.open(encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    entries.append(JournalEntry.from_dict(json.loads(line)))
                except (ValueError, TypeError, KeyError) as exc:
                    warnings.append(f"{self.path}:{n}: unreadable journal line skipped ({exc})")
        return entries, warnings


def claims_in_window(
    entries: Iterable[JournalEntry], now: datetime, window: timedelta = timedelta(hours=24)
) -> list[JournalEntry]:
    """Claims journalled as created or duplicate within ``window`` before ``now``."""
    start = now - window
    return [
        e
        for e in entries
        if e.kind == "claim_work" and e.outcome in SUCCESS and parse_iso_utc(e.ts_utc) > start
    ]


def known_curation_ids(entries: Iterable[JournalEntry]) -> set[str]:
    """Curation IDs already recorded in the journal."""
    return {e.curation_id for e in entries if e.curation_id}


# ---------------------------------------------------------------------- pending


def server_records(body: Any) -> list[dict[str, Any]] | None:
    """Extract curation rows from a ``GET /curations`` body; ``None`` if the shape is unknown."""
    rows = body if isinstance(body, list) else None
    if isinstance(body, dict):
        rows = next((body[k] for k in _LIST_KEYS if isinstance(body.get(k), list)), None)
    if rows is None:
        return None
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)]


def fetch_curations(get: Callable[..., Any], max_pages: int = 1000) -> list[dict[str, Any]] | None:
    """Read every ``GET /curations`` page (``meta.total_pages``); ``None`` on an unknown shape."""
    rows: list[dict[str, Any]] = []
    page, total = 1, 1
    while page <= min(total, max_pages):
        body = get(page=page)
        found = server_records(body)
        if found is None:
            return None
        rows += found
        meta = body.get("meta") if isinstance(body, dict) else None
        total = int(meta.get("total_pages") or 1) if isinstance(meta, dict) else 1
        page += 1
    return rows


@dataclass(frozen=True)
class PendingRow:
    """One line of the ``pending`` reconciliation table."""

    state: str
    curation_id: str | None
    kind: str
    entity: str
    value: str
    since: datetime | None
    applied_at: datetime | None
    note: str = ""


def _server_note(rec: dict[str, Any]) -> str:
    """The server's own remark on a curation: its last check error or what superseded it."""
    if rec.get("last_check_error"):
        return str(rec["last_check_error"])
    if rec.get("superseded_by"):
        return f"superseded by {rec['superseded_by']}"
    return ""


def mark_submitted(actions: Iterable[Action], entries: Iterable[JournalEntry]) -> list[Action]:
    """Keep rows whose identical action was already submitted successfully, but deselect them."""
    done: dict[str, str] = {}
    for e in entries:
        if e.outcome in SUCCESS and e.curation_id and e.kind in ("claim_work", "remove_work"):
            done.setdefault(e.action_id, e.curation_id)
    out = []
    for a in actions:
        cid = done.get(a.id)
        if cid and a.status in ("claim", "remove"):
            a = replace(a, decision="skip", reasons=[*a.reasons, f"already-submitted:{cid}"])
        out.append(a)
    return out


def _when(value: Any) -> datetime | None:
    """Parse a server or journal timestamp, or ``None``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_iso_utc(value)
    except ValueError:
        return None


def reconcile(
    entries: Sequence[JournalEntry],
    records: Sequence[dict[str, Any]],
    *,
    server_known: bool = True,
) -> list[PendingRow]:
    """Match journal entries to server rows by curation ID.

    States: ``applied``, ``pending``, ``journal-only`` (journalled, absent on the server),
    ``server-only`` (on the server, absent from this journal), ``unknown-outcome``
    (journalled with no curation ID and an unknown outcome) and ``unverified`` (the server
    response could not be read).
    """
    by_id = {r["id"]: r for r in records}
    rows: list[PendingRow] = []
    seen: set[str] = set()
    for e in entries:
        entity = short_id(str(e.payload.get("entity_id", "")))
        value = str(e.payload.get("value", ""))
        journalled = _when(e.ts_utc)
        if not e.curation_id:
            if e.outcome == "unknown":
                rows.append(
                    PendingRow("unknown-outcome", None, e.kind, entity, value, journalled, None)
                )
            continue
        if e.curation_id in seen:
            continue
        seen.add(e.curation_id)
        rec = by_id.get(e.curation_id)
        note = _server_note(rec) if rec else ""
        if not server_known:
            state, since, applied = "unverified", journalled, None
        elif rec is None:
            state, since, applied = "journal-only", journalled, None
        else:
            state = "applied" if rec.get("is_applied") else "pending"
            since, applied = _when(rec.get("created")) or journalled, _when(rec.get("applied_at"))
        rows.append(PendingRow(state, e.curation_id, e.kind, entity, value, since, applied, note))
    for rec in records:
        if rec["id"] in seen:
            continue
        kind = f"{rec.get('entity', '?')}.{rec.get('property', '?')}:{rec.get('action', '?')}"
        rows.append(
            PendingRow(
                "server-only",
                rec["id"],
                kind,
                short_id(str(rec.get("entity_id", ""))),
                str(rec.get("value", "")),
                _when(rec.get("created")),
                _when(rec.get("applied_at")),
                _server_note(rec),
            )
        )
    return rows


# ---------------------------------------------------------------------- revert


def select_for_revert(
    entries: Sequence[JournalEntry], *, since: datetime | None, ids: Sequence[str]
) -> list[JournalEntry]:
    """Successful entries newer than ``since`` or with one of ``ids``, oldest first."""
    if (since is None) == (not ids):
        raise UsageError("give exactly one of --since or --ids")
    wanted = set(ids)
    chosen = [
        e
        for e in entries
        if e.outcome in SUCCESS
        and ((e.curation_id in wanted) if wanted else parse_iso_utc(e.ts_utc) >= since)  # type: ignore[operator]
    ]
    if wanted:
        missing = wanted - {e.curation_id for e in chosen}
        if missing:
            raise UsageError(
                f"not found among successful journal entries: {', '.join(sorted(missing))}"
            )
    return sorted(chosen, key=lambda e: e.ts_utc)


def unknown_in_range(entries: Sequence[JournalEntry], since: datetime) -> list[JournalEntry]:
    """Unknown-outcome entries since ``since`` that no later submission of the action settled."""
    settled = {(e.action_id, e.ts_utc) for e in entries if e.outcome in SUCCESS}
    return [
        e
        for e in entries
        if e.outcome == "unknown"
        and parse_iso_utc(e.ts_utc) >= since
        and not any(aid == e.action_id and ts > e.ts_utc for aid, ts in settled)
    ]


def _inverse_action(
    kind: str, entity: str, value: str, raw: str | None, ref: str, prev: str | None = None
) -> Action:
    """An inverse action, ready to apply."""
    status = "remove" if kind in ("remove_work", "remove_orcid") else "claim"
    return Action(
        kind=kind,  # type: ignore[arg-type]
        entity_id=entity,
        value=value,
        raw_author_name=raw,
        previous=prev,
        status=status,  # type: ignore[arg-type]
        decision="apply",
        reasons=["revert"],
        evidence={"reverts": ref},
    )


def build_revert_plan(
    entries: Sequence[JournalEntry], author_id: str, author_display_name: str = ""
) -> Plan:
    """Build the inverse plan of ``entries`` (which must be successful submissions)."""
    actions: list[Action] = []
    warnings: list[str] = []
    author_level: dict[str, list[JournalEntry]] = {}
    for e in entries:
        ref = e.curation_id or e.action_id
        entity, value = str(e.payload.get("entity_id", "")), str(e.payload.get("value", ""))
        if e.kind == "claim_work":
            actions.append(_inverse_action("remove_work", entity, value, e.raw_author_name, ref))
        elif e.kind == "remove_work":
            if not e.raw_author_name:
                warnings.append(
                    f"{ref}: no stored byline, so the removal of {entity} cannot be inverted"
                )
                continue
            actions.append(_inverse_action("claim_work", entity, value, e.raw_author_name, ref))
        else:
            prop = "orcid" if e.kind in ("set_orcid", "remove_orcid") else e.kind
            author_level.setdefault(prop, []).append(e)
    for prop, group in author_level.items():
        first, last = group[0], group[-1]
        entity, ref = str(first.payload.get("entity_id", "")), first.curation_id or first.action_id
        if prop == "orcid":
            before = (
                first.previous if first.kind == "set_orcid" else str(first.payload.get("value"))
            )
            after = str(last.payload.get("value")) if last.kind == "set_orcid" else None
            if after and after != before:
                actions.append(_inverse_action("remove_orcid", entity, after, None, ref))
            if before and before != after:
                actions.append(_inverse_action("set_orcid", entity, before, None, ref, prev=after))
        else:
            before = first.previous
            if before is None:
                warnings.append(f"{ref}: no stored previous value, so {prop} cannot be reverted")
                continue
            if before != last.payload.get("value"):
                actions.append(
                    _inverse_action(
                        prop, entity, before, None, ref, prev=str(last.payload.get("value"))
                    )
                )
    if any(a.kind in ("claim_work", "remove_work") for a in actions):
        warnings.append(U7_CAVEAT)
    return Plan(
        author_id=author_id,
        author_display_name=author_display_name,
        actions=actions,
        warnings=warnings,
    )


def now_utc() -> datetime:
    """The current time in UTC (a seam for tests)."""
    return datetime.now(UTC)
