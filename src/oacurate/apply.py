"""Submission of a plan: selection, preflight, re-validation, daily cap, canary, chunking."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from oacurate.client import OpenAlexClient
from oacurate.errors import (
    EXIT_AUTH,
    EXIT_BUDGET,
    EXIT_OK,
    EXIT_PARTIAL,
    ApiError,
    AuthError,
    BudgetError,
    InvalidIdentifier,
    Unauthorized,
    UsageError,
)
from oacurate.ids import normalise_openalex_id, short_id
from oacurate.journal import Journal, claims_in_window, known_curation_ids, now_utc
from oacurate.model import (
    ORCID_KINDS,
    WORK_KINDS,
    Action,
    AuthorView,
    ItemOutcome,
    JournalEntry,
    Plan,
    WorkView,
    anchorable,
    parse_iso_utc,
    utc_now_iso,
)

DAILY_CLAIM_CAP = 1000
AUTH_HINT = (
    "likely causes: the profile is not claimed by this account, the claim is still "
    "pending approval, or the API key belongs to a different account"
)


@dataclass(frozen=True)
class ApplyOptions:
    """What to submit and how."""

    accept: Sequence[str] = ()
    reject: Sequence[str] = ()
    chunk: int = 25
    yes: bool = False
    daily_cap: int = DAILY_CLAIM_CAP


@dataclass
class ApplyReport:
    """Everything that happened during one ``apply``."""

    selected: list[Action]
    dry_run: bool
    stale: list[tuple[Action, str]] = field(default_factory=list)
    results: list[tuple[Action, ItemOutcome]] = field(default_factory=list)
    leftover: list[Action] = field(default_factory=list)
    window_frees_at: datetime | None = None
    stopped: str | None = None
    stop_exit: int | None = None
    notes: list[str] = field(default_factory=list)
    unsent: list[Action] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        """Submitted items by outcome."""
        out = {"created": 0, "duplicate": 0, "rejected": 0, "unknown": 0}
        for _, o in self.results:
            out[o.outcome] += 1
        return out

    @property
    def exit_code(self) -> int:
        """0 when everything sent succeeded and nothing was left over; else 2, 3 or 4."""
        if self.stop_exit is not None:
            return self.stop_exit
        c = self.counts()
        if c["rejected"] or c["unknown"] or self.leftover or self.unsent or self.stopped:
            return EXIT_PARTIAL
        return EXIT_OK


def _token_matches(action: Action, token: str) -> bool:
    """Tell whether an ``--accept``/``--reject`` token names ``action``."""
    if action.id == token:
        return True
    try:
        work = normalise_openalex_id(token, "W")[0]
    except InvalidIdentifier:
        return False
    return action.kind in WORK_KINDS and short_id(action.entity_id) == work


def select_actions(plan: Plan, accept: Sequence[str], reject: Sequence[str]) -> list[Action]:
    """Rows with decision ``apply``, plus ``accept``, minus ``reject``, in plan order."""
    chosen = {a.id for a in plan.actions if a.decision == "apply"}
    for token in accept:
        hits = [a for a in plan.actions if _token_matches(a, token)]
        if not hits:
            raise UsageError(f"--accept {token}: no such action or work in the plan")
        for a in hits:
            if a.status not in ("claim", "remove", "review"):
                raise UsageError(f"--accept {token}: a {a.status!r} row cannot be submitted")
            if a.kind == "claim_work" and not anchorable(a.raw_author_name):
                raise UsageError(
                    f"--accept {token}: row {a.id} has no byline anchor; use `plan pick` first"
                )
            chosen.add(a.id)
    for token in reject:
        hits = [a for a in plan.actions if _token_matches(a, token)]
        if not hits:
            raise UsageError(f"--reject {token}: no such action or work in the plan")
        chosen -= {a.id for a in hits}
    return [a for a in plan.actions if a.id in chosen]


def preflight(client: OpenAlexClient) -> str | None:
    """Check the key against ``GET /curations``; return a note when the check is inconclusive."""
    try:
        client.get_curations()
    except Unauthorized as exc:
        raise Unauthorized(
            f"{exc}\nhint: check the API key (OPENALEX_API_KEY or --api_key_file)",
            status=exc.status,
            body=exc.body,
        ) from exc
    except ApiError as exc:
        if exc.status is None or not 400 <= exc.status < 500:
            raise
        return (
            f"preflight GET /curations answered {exc.status} ({exc}); continuing, "
            "the canary will check authorisation"
        )
    return None


def revalidate(
    client: OpenAlexClient, plan: Plan, actions: Sequence[Action]
) -> tuple[list[Action], list[tuple[Action, str]]]:
    """Re-check each action against fresh single lookups (free); drop stale ones."""
    keep: list[Action] = []
    stale: list[tuple[Action, str]] = []
    author: AuthorView | None = None
    for a in actions:
        if a.kind in WORK_KINDS:
            data = client.get_work(a.entity_id)
            if data is None:
                stale.append((a, "the work no longer resolves"))
                continue
            work = WorkView.from_json(data)
            if work.id != a.entity_id:
                stale.append((a, f"the work was merged into {short_id(work.id)}; plan it again"))
                continue
            on_profile = bool(work.authorships_of(plan.author_id))
            if a.kind == "remove_work":
                if not on_profile:
                    stale.append(
                        (a, "not on the profile (if a claim is still pending, retry later)")
                    )
                else:
                    keep.append(a)
                continue
            reason = _claim_staleness(a, work, on_profile)
            if reason:
                stale.append((a, reason))
            else:
                keep.append(a)
            continue
        if author is None:
            data = client.get_author(plan.author_id)
            if data is None:
                stale.append((a, "the author no longer resolves"))
                continue
            author = AuthorView.from_json(data)
        if a.kind in ORCID_KINDS:
            previous = author.orcid
        elif a.kind == "set_display_name":
            previous = author.display_name
        else:
            previous = author.full_name
        keep.append(replace(a, previous=previous))
    return keep, stale


def _claim_staleness(a: Action, work: WorkView, on_profile: bool) -> str | None:
    """Return why a planned claim no longer holds, or ``None`` if it still does."""
    if on_profile:
        return "already on the profile"
    anchors = [x for x in work.authorships if x.raw_author_name == a.raw_author_name]
    if not anchors:
        return "the byline is no longer on the work"
    if len(anchors) > 1:
        return "the byline is no longer unique on the work"
    if "owner" in a.evidence:
        now = short_id(anchors[0].author_id) if anchors[0].author_id else None
        if now != a.evidence["owner"]:
            return f"the byline's owner changed from {a.evidence['owner']} to {now}"
    return None


def _apply_cap(
    actions: Sequence[Action], journal_entries: Sequence[JournalEntry], now: datetime, cap: int
) -> tuple[list[Action], list[Action], datetime | None]:
    """Split ``actions`` into what fits under the rolling 24 h claim cap and what is left."""
    window = claims_in_window(journal_entries, now)
    allowed = max(cap - len(window), 0)
    send, leftover = [], []
    for a in actions:
        if a.kind == "claim_work":
            if allowed == 0:
                leftover.append(a)
                continue
            allowed -= 1
        send.append(a)
    frees = None
    if leftover and window:
        frees = min(parse_iso_utc(e.ts_utc) for e in window) + timedelta(hours=24)
    return send, leftover, frees


def _journal(
    journal: Journal, actions: Sequence[Action], outcomes: Sequence[ItemOutcome], known: set[str]
) -> list[tuple[Action, ItemOutcome]]:
    """Journal each item (fsynced) and return the pairs, re-labelling known IDs as duplicates."""
    pairs = []
    for a, o in zip(actions, outcomes, strict=True):
        if o.outcome == "created" and o.curation_id in known:
            o = replace(o, outcome="duplicate")
        if o.curation_id:
            known.add(o.curation_id)
        journal.append(
            JournalEntry(
                ts_utc=utc_now_iso(),
                action_id=a.id,
                kind=a.kind,
                payload=a.to_payload(),
                previous=a.previous,
                raw_author_name=a.raw_author_name,
                http_status=o.http_status,
                outcome=o.outcome,
                curation_id=o.curation_id,
                message=o.message,
                raw=o.raw,
            )
        )
        pairs.append((a, o))
    return pairs


def _post(client: OpenAlexClient, batch: Sequence[Action], single: bool) -> list[ItemOutcome]:
    """POST a batch, retrying once on an HTTP-level failure."""
    payloads = [a.to_payload() for a in batch]
    body = payloads[0] if single else payloads
    try:
        return client.post_curations(body)
    except AuthError:
        raise
    except ApiError:
        return client.post_curations(body)


def submit(
    client: OpenAlexClient,
    actions: Sequence[Action],
    journal: Journal,
    known: set[str],
    report: ApplyReport,
    chunk: int,
) -> None:
    """Send a canary alone, then the rest in chunks; journal every item as it lands."""
    batches = [
        list(actions[:1]),
        *(list(actions[i : i + chunk]) for i in range(1, len(actions), chunk)),
    ]
    for n, batch in enumerate(batches):
        if not batch:
            continue
        later = [a for b in batches[n + 1 :] for a in b]
        try:
            outcomes = _post(client, batch, single=(n == 0))
        except AuthError as exc:
            if n == 0:
                raise type(exc)(f"{exc}\n{AUTH_HINT}", status=exc.status, body=exc.body) from exc
            report.stopped, report.stop_exit = f"authentication failed mid-run: {exc}", EXIT_AUTH
            report.unsent = [*batch, *later]
            return
        except BudgetError as exc:
            report.stopped, report.stop_exit = f"rate or budget limit: {exc}", EXIT_BUDGET
            report.unsent = [*batch, *later]
            return
        except ApiError as exc:
            unknown = [
                ItemOutcome(exc.status, "unknown", None, f"HTTP-level failure: {exc}", exc.body)
                for _ in batch
            ]
            report.results += _journal(journal, batch, unknown, known)
            report.stopped = f"a batch failed twice at HTTP level ({exc}); run stopped"
            report.unsent = later
            return
        report.results += _journal(journal, batch, outcomes, known)


def apply_plan(
    client: OpenAlexClient,
    plan: Plan,
    journal: Journal,
    opts: ApplyOptions,
    *,
    now: Callable[[], datetime] = now_utc,
) -> ApplyReport:
    """Run the apply pipeline; without ``opts.yes`` nothing is sent (dry run)."""
    if opts.chunk < 1:
        raise UsageError("--chunk must be at least 1")
    selected = select_actions(plan, opts.accept, opts.reject)
    report = ApplyReport(selected=selected, dry_run=not opts.yes)
    if not opts.yes or not selected:
        return report
    note = preflight(client)
    if note:
        report.notes.append(note)
    fresh, report.stale = revalidate(client, plan, selected)
    entries, warnings = journal.read()
    report.notes += warnings
    send, report.leftover, report.window_frees_at = _apply_cap(
        fresh, entries, now(), opts.daily_cap
    )
    if send:
        submit(client, send, journal, known_curation_ids(entries), report, opts.chunk)
    return report
