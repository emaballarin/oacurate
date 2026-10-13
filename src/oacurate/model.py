"""Plan, action and journal records, plus light read-only views of OpenAlex works and authors."""

import contextlib
import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, get_args

from simple_parsing import Serializable

from oacurate import __version__
from oacurate.errors import InvalidIdentifier, PlanError
from oacurate.ids import (
    OPENALEX_PREFIX,
    normalise_doi,
    normalise_openalex_id,
    normalise_orcid,
    orcid_or_none,
)

ActionKind = Literal[
    "claim_work",
    "remove_work",
    "set_orcid",
    "remove_orcid",
    "set_display_name",
    "set_full_name",
]
Status = Literal[
    "already", "claim", "remove", "review", "blocked", "protected", "not_on_profile", "missing"
]
Decision = Literal["apply", "skip"]
Outcome = Literal["created", "duplicate", "rejected", "unknown"]
UnresolvedReason = Literal[
    "missing",
    "lookup-failed",
    "parse-error",
    "duplicate-key",
    "invalid-identifier",
    "search-skipped",
]

ACTION_KINDS: tuple[str, ...] = get_args(ActionKind)
STATUSES: tuple[str, ...] = get_args(Status)
DECISIONS: tuple[str, ...] = get_args(Decision)
OUTCOMES: tuple[str, ...] = get_args(Outcome)
UNRESOLVED_REASONS: tuple[str, ...] = get_args(UnresolvedReason)

WORK_KINDS = frozenset({"claim_work", "remove_work"})
ORCID_KINDS = frozenset({"set_orcid", "remove_orcid"})
NAME_KINDS = frozenset({"set_display_name", "set_full_name"})

CLAIM_PROPERTY_RE = re.compile(r'^authorships\[raw_author_name="(.+)"\]\.author\.id$')
SCHEMA_VERSION = 1

_STATUS_ORDER = {
    s: i
    for i, s in enumerate(
        (
            "claim",
            "blocked",
            "review",
            "remove",
            "protected",
            "already",
            "not_on_profile",
            "missing",
        )
    )
}
_AUTHOR_KIND_ORDER = {
    k: i for i, k in enumerate(("remove_orcid", "set_orcid", "set_display_name", "set_full_name"))
}
_BASIS_RANK = {"work-id": 0, "doi": 0, "arxiv": 1, "title-exact": 2, "audit": 3, "title-near": 4}
_TIER_RANK = {"full": 0, "initial": 1, "none": 2}


def utc_now_iso() -> str:
    """Return the current UTC time as ISO 8601 with a ``Z`` suffix, to the second."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso_utc(s: str) -> datetime:
    """Parse an ISO 8601 timestamp, treating a naive value as UTC."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# --------------------------------------------------------------------------------------
# Light views of API objects
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorshipView:
    """One authorship of a work, as far as curation needs it."""

    position: int
    raw_author_name: str | None
    author_id: str | None
    author_display_name: str | None
    author_orcid: str | None
    raw_orcid: str | None

    @classmethod
    def from_json(cls, d: Mapping[str, Any], position: int) -> AuthorshipView:
        """Build from an API authorship object; ``position`` is the 1-based list index."""
        author = d.get("author") or {}
        return cls(
            position=position,
            raw_author_name=d.get("raw_author_name"),
            author_id=author.get("id") or None,
            author_display_name=author.get("display_name"),
            author_orcid=orcid_or_none(author.get("orcid")),
            raw_orcid=orcid_or_none(d.get("raw_orcid")),
        )


@dataclass(frozen=True)
class WorkView:
    """A work, as far as curation needs it."""

    id: str
    doi: str | None
    title: str
    year: int | None
    type: str | None
    venue: str | None
    is_paratext: bool
    is_retracted: bool
    authorships: tuple[AuthorshipView, ...]

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> WorkView:
        """Build from an API work object (single lookup or list item)."""
        doi = d.get("doi")
        with contextlib.suppress(InvalidIdentifier):
            doi = normalise_doi(doi) if doi else None
        source = ((d.get("primary_location") or {}).get("source")) or {}
        return cls(
            id=d["id"],
            doi=doi,
            title=d.get("title") or d.get("display_name") or "",
            year=d.get("publication_year"),
            type=d.get("type"),
            venue=source.get("display_name"),
            is_paratext=bool(d.get("is_paratext")),
            is_retracted=bool(d.get("is_retracted")),
            authorships=tuple(
                AuthorshipView.from_json(a, i) for i, a in enumerate(d.get("authorships") or [], 1)
            ),
        )

    def authorships_of(self, author_id: str) -> list[AuthorshipView]:
        """Return the authorships attributed to ``author_id`` (long form)."""
        return [a for a in self.authorships if a.author_id == author_id]


@dataclass(frozen=True)
class AuthorView:
    """An author profile, as far as curation needs it."""

    id: str
    display_name: str
    orcid: str | None
    works_count: int
    full_name: str | None
    raw_author_names: tuple[str, ...]
    display_name_alternatives: tuple[str, ...]

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> AuthorView:
        """Build from an API author object."""
        return cls(
            id=d["id"],
            display_name=d.get("display_name") or "",
            orcid=orcid_or_none(d.get("orcid")),
            works_count=int(d.get("works_count") or 0),
            full_name=d.get("full_name"),
            raw_author_names=tuple(d.get("raw_author_names") or ()),
            display_name_alternatives=tuple(d.get("display_name_alternatives") or ()),
        )


# --------------------------------------------------------------------------------------
# Plan records
# --------------------------------------------------------------------------------------


def action_id(kind: str, entity_id: str, value: str, raw_author_name: str | None) -> str:
    """Return the stable short hash identifying an action."""
    blob = json.dumps([kind, entity_id, value, raw_author_name], ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:10]


def claim_property(raw_author_name: str) -> str:
    """Return the claim property string anchoring ``raw_author_name`` byte for byte."""
    return f'authorships[raw_author_name="{raw_author_name}"].author.id'


def anchorable(raw_author_name: str | None) -> bool:
    """Tell whether the server regex recovers ``raw_author_name`` exactly from a claim."""
    if not raw_author_name:
        return False
    m = CLAIM_PROPERTY_RE.match(claim_property(raw_author_name))
    return m is not None and m.group(1) == raw_author_name


@dataclass(kw_only=True)
class Action(Serializable):
    """One curation to submit (or a row explaining why nothing is submitted)."""

    id: str = ""
    kind: ActionKind
    entity_id: str
    value: str
    raw_author_name: str | None = None
    previous: str | None = None
    status: Status
    decision: Decision = "skip"
    reasons: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Fill in the stable ID when it was not given."""
        if not self.id:
            self.id = self.computed_id()

    def computed_id(self) -> str:
        """Return the ID this action's identity fields hash to."""
        return action_id(self.kind, self.entity_id, self.value, self.raw_author_name)

    def to_payload(self) -> dict[str, str]:
        """Build the exact curation JSON object for this action."""
        match self.kind:
            case "claim_work":
                if self.raw_author_name is None:
                    raise PlanError(f"action {self.id}: a claim needs raw_author_name")
                return _payload(
                    "works",
                    self.entity_id,
                    claim_property(self.raw_author_name),
                    "replace",
                    self.value,
                )
            case "remove_work":
                return _payload(
                    "works", self.entity_id, "authorships.author.id", "remove", self.value
                )
            case "set_orcid":
                return _payload("authors", self.entity_id, "orcid", "replace", self.value)
            case "remove_orcid":
                return _payload("authors", self.entity_id, "orcid", "remove", self.value)
            case "set_display_name":
                return _payload("authors", self.entity_id, "display_name", "replace", self.value)
            case "set_full_name":
                return _payload("authors", self.entity_id, "full_name", "replace", self.value)
        raise PlanError(f"action {self.id}: unknown kind {self.kind!r}")


def _payload(entity: str, entity_id: str, prop: str, action: str, value: str) -> dict[str, str]:
    """Assemble one curation object in the documented key order."""
    return {
        "entity": entity,
        "entity_id": entity_id,
        "property": prop,
        "action": action,
        "value": value,
    }


def rekeyed(action: Action, **changes: Any) -> Action:
    """Return a copy of ``action`` with ``changes`` applied and its ID recomputed."""
    return replace(action, id="", **changes)


@dataclass(kw_only=True)
class UnresolvedInput(Serializable):
    """An input that produced no action: unparseable, invalid, or absent from OpenAlex."""

    ref: str
    reason: UnresolvedReason
    detail: str = ""
    record: dict[str, Any] = field(default_factory=dict)


@dataclass(kw_only=True)
class Plan(Serializable):
    """A reviewable set of actions for one author, written to and read from a JSON file."""

    schema_version: int = SCHEMA_VERSION
    tool_version: str = __version__
    created_utc: str = field(default_factory=utc_now_iso)
    author_id: str
    author_display_name: str = ""
    seeds: list[str] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    unresolved: list[UnresolvedInput] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    credits_used: int = 0

    def validate(self, expected_author: str | None = None) -> None:
        """Check every invariant explicitly; raise ``PlanError`` listing all problems."""
        problems = _plan_problems(self, expected_author)
        if problems:
            raise PlanError("invalid plan:\n  " + "\n  ".join(problems))

    def sorted(self) -> Plan:
        """Return a copy with actions and unresolved inputs in the stable file order."""
        return replace(
            self,
            actions=sorted(self.actions, key=action_sort_key),
            unresolved=sorted(self.unresolved, key=lambda u: (u.reason, u.ref)),
        )


def action_sort_key(a: Action) -> tuple[Any, ...]:
    """Stable plan order: author-level rows (ORCID removal first), then status and evidence."""
    ev = a.evidence
    return (
        _AUTHOR_KIND_ORDER.get(a.kind, len(_AUTHOR_KIND_ORDER)),
        _STATUS_ORDER.get(a.status, 99),
        0 if ev.get("anchor") == "orcid" else 1,
        _TIER_RANK.get(ev.get("tier", "none"), 3),
        _BASIS_RANK.get(ev.get("basis", ""), 5),
        a.kind,
        a.entity_id,
        a.raw_author_name or "",
        a.id,
    )


def _plan_problems(plan: Plan, expected_author: str | None) -> list[str]:
    """Collect every invariant violation in ``plan``."""
    problems: list[str] = []
    if plan.schema_version != SCHEMA_VERSION:
        problems.append(f"schema_version {plan.schema_version} is not {SCHEMA_VERSION}")
    try:
        _, author_long = normalise_openalex_id(plan.author_id, "A")
        if author_long != plan.author_id:
            problems.append(f"author_id {plan.author_id!r} is not in long form")
    except InvalidIdentifier as exc:
        return [*problems, str(exc)]
    if expected_author is not None and plan.author_id != expected_author:
        problems.append(
            f"plan is for {plan.author_id}, but the configured author is {expected_author}"
        )
    seen: set[str] = set()
    for a in plan.actions:
        problems += [f"action {a.id}: {p}" for p in _action_problems(a, plan.author_id)]
        if a.id in seen:
            problems.append(f"action {a.id}: duplicate action ID")
        seen.add(a.id)
    for u in plan.unresolved:
        if u.reason not in UNRESOLVED_REASONS:
            problems.append(f"unresolved {u.ref}: unknown reason {u.reason!r}")
    return problems


def _action_problems(a: Action, author_id: str) -> list[str]:
    """Collect invariant violations of one action."""
    problems: list[str] = []
    if a.kind not in ACTION_KINDS:
        return [f"unknown kind {a.kind!r}"]
    if a.status not in STATUSES:
        problems.append(f"unknown status {a.status!r}")
    if a.decision not in DECISIONS:
        problems.append(f"unknown decision {a.decision!r}")
    if not all(isinstance(r, str) for r in a.reasons):
        problems.append("reasons must be strings")
    if a.id != a.computed_id():
        problems.append("ID does not match its kind, entity, value and byline")
    if a.kind in WORK_KINDS:
        try:
            if normalise_openalex_id(a.entity_id, "W")[1] != a.entity_id:
                problems.append(f"work ID {a.entity_id!r} is not in long form")
        except InvalidIdentifier as exc:
            problems.append(str(exc))
        if a.value != author_id:
            problems.append(f"value {a.value!r} is not the plan's author")
        submittable = a.status == "claim" or a.decision == "apply"
        if a.kind == "claim_work" and submittable and not anchorable(a.raw_author_name):
            problems.append("claim byline does not survive the server's property regex")
    else:
        if a.entity_id != author_id:
            problems.append(f"entity {a.entity_id!r} is not the plan's author")
        if a.kind in ORCID_KINDS:
            try:
                if normalise_orcid(a.value) != a.value:
                    problems.append(f"ORCID {a.value!r} is not in canonical form")
            except InvalidIdentifier as exc:
                problems.append(str(exc))
        elif not a.value.strip() or any(ord(c) < 32 for c in a.value):
            problems.append("name is empty or contains control characters")
    if a.decision == "apply" and a.status not in ("claim", "remove"):
        problems.append(f"decision 'apply' is not allowed for status {a.status!r}")
    return problems


def save_plan(plan: Plan, path: str | os.PathLike[str]) -> None:
    """Write ``plan`` in stable order, atomically, as indented UTF-8 JSON."""
    data = json.dumps(plan.sorted().to_dict(), indent=2, ensure_ascii=False) + "\n"
    target = Path(path)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(target)


def load_plan(path: str | os.PathLike[str], expected_author: str | None = None) -> Plan:
    """Read and validate a plan file."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"cannot read plan {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PlanError(f"plan {path} is not a JSON object")
    try:
        plan = Plan.from_dict(data)
    except (TypeError, ValueError, KeyError) as exc:
        raise PlanError(f"plan {path} does not match the schema: {exc}") from exc
    plan.validate(expected_author)
    return plan


# --------------------------------------------------------------------------------------
# Journal records and per-item submission outcomes
# --------------------------------------------------------------------------------------


@dataclass(kw_only=True)
class JournalEntry(Serializable):
    """One submitted curation item, as journalled the moment its response arrived."""

    ts_utc: str
    action_id: str
    kind: ActionKind
    payload: dict[str, Any]
    previous: str | None = None
    raw_author_name: str | None = None
    http_status: int | None = None
    outcome: Outcome
    curation_id: str | None = None
    message: str = ""
    raw: Any = None


@dataclass(frozen=True)
class ItemOutcome:
    """The server's verdict on one submitted curation item."""

    http_status: int | None
    outcome: Outcome
    curation_id: str | None
    message: str
    raw: Any


def is_long_author_id(s: str) -> bool:
    """Tell whether ``s`` is a long-form OpenAlex author ID."""
    return s.startswith(OPENALEX_PREFIX + "A") and s[len(OPENALEX_PREFIX) + 1 :].isdigit()
