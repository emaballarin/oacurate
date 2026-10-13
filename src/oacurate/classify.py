"""Classification of a (work, author) pair into a plan status with evidence (PLAN §6.7)."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from oacurate.ids import short_id
from oacurate.model import Action, AuthorshipView, AuthorView, WorkView, anchorable, rekeyed
from oacurate.names import Seeds, Tier, match_tier, matching_authorships

OwnerLookup = Callable[[str], AuthorView | None]
_MAX_LISTED_BYLINES = 30


@dataclass(frozen=True)
class Context:
    """Everything classification needs besides the work itself."""

    target_id: str
    my_orcid: str | None
    seeds: Seeds
    owner_of: OwnerLookup
    splinter_max: int = 3
    coauthor_min: int = 3
    coauthors: Mapping[str, int] | None = None
    force: bool = False
    extra_evidence: Mapping[str, Any] = field(default_factory=dict)


def _work_evidence(work: WorkView, basis: str, refs: Sequence[str]) -> dict[str, Any]:
    """Evidence fields that describe the work and how it was found."""
    return {
        "basis": basis,
        "doi": work.doi,
        "input": sorted(set(refs)),
        "is_retracted": work.is_retracted,
        "title": work.title,
        "type": work.type,
        "venue": work.venue,
        "work": short_id(work.id),
        "year": work.year,
    }


def _authorship_evidence(
    a: AuthorshipView, tier: Tier, owner: AuthorView | None, overlap: int | None
) -> dict[str, Any]:
    """Evidence fields that describe one candidate authorship and its current owner."""
    return {
        "byline": a.raw_author_name,
        "coauthor_overlap": overlap,
        "owner": short_id(a.author_id) if a.author_id else None,
        "owner_orcid": owner.orcid if owner else None,
        "owner_works_count": owner.works_count if owner else None,
        "position": a.position,
        "raw_orcid": a.raw_orcid,
        "tier": tier,
    }


def _bylines(work: WorkView) -> list[str]:
    """A compact ``position:byline`` listing for rows that need ``plan pick``."""
    listed = [f"{a.position}:{a.raw_author_name}" for a in work.authorships[:_MAX_LISTED_BYLINES]]
    if len(work.authorships) > _MAX_LISTED_BYLINES:
        listed.append(f"… {len(work.authorships) - _MAX_LISTED_BYLINES} more")
    return listed


def coauthor_overlap(
    work: WorkView, candidate: AuthorshipView, coauthors: Mapping[str, int] | None
) -> int | None:
    """Count distinct profile co-authors on ``work`` besides ``candidate``; ``None`` if unknown."""
    if coauthors is None:
        return None
    ids = {a.author_id for a in work.authorships if a is not candidate and a.author_id}
    return sum(1 for i in ids if coauthors.get(i, 0) > 0)


def _owner(a: AuthorshipView, ctx: Context) -> tuple[AuthorView | None, bool]:
    """Return ``(owner, lookup_failed)`` for an authorship's current author."""
    if not a.author_id:
        return None, False
    owner = ctx.owner_of(a.author_id)
    return owner, owner is None


def _claim_row(
    work: WorkView,
    ctx: Context,
    a: AuthorshipView | None,
    status: str,
    reasons: list[str],
    evidence: dict[str, Any],
) -> Action:
    """Assemble a ``claim_work`` action with the default decision for its status."""
    return Action(
        kind="claim_work",
        entity_id=work.id,
        value=ctx.target_id,
        raw_author_name=a.raw_author_name if a else None,
        status=status,  # type: ignore[arg-type]
        decision="apply" if status == "claim" else "skip",
        reasons=reasons,
        evidence=dict(sorted(evidence.items())),
    )


def post_rule(action: Action, work: WorkView) -> Action:
    """Block a claim whose byline anchor is not unique on the work or not anchorable."""
    if action.status != "claim":
        return action
    raw = action.raw_author_name
    if not anchorable(raw):
        reason = "unanchorable-byline"
    elif sum(1 for a in work.authorships if a.raw_author_name == raw) > 1:
        reason = "duplicate-byline-anchor"
    else:
        return action
    return rekeyed(action, status="blocked", decision="skip", reasons=[*action.reasons, reason])


def classify_add(work: WorkView, ctx: Context, *, basis: str, refs: Sequence[str]) -> Action:
    """Classify a work the target author may want attached (the add table of §6.7)."""
    ev = _work_evidence(work, basis, refs) | dict(ctx.extra_evidence)
    notes = ["retracted"] if work.is_retracted else []

    def row(a: AuthorshipView | None, status: str, reason: str | None, **more: Any) -> Action:
        """Assemble a claim row carrying the work evidence plus ``more``."""
        reasons = [*([reason] if reason else []), *notes]
        return _claim_row(work, ctx, a, status, reasons, ev | more)

    # 1. already attached
    mine = work.authorships_of(ctx.target_id)
    if mine:
        a = mine[0]
        return row(
            a,
            "already",
            None,
            **_authorship_evidence(a, match_tier(a.raw_author_name, ctx.seeds), None, None),
        )
    # 2. paratext
    if work.is_paratext:
        return row(None, "review", "paratext", bylines=_bylines(work))
    # 3. ORCID evidence
    if ctx.my_orcid:
        by_orcid = [
            a
            for a in work.authorships
            if a.raw_orcid == ctx.my_orcid
            or (a.author_orcid == ctx.my_orcid and a.author_id != ctx.target_id)
        ]
        if len(by_orcid) == 1:
            a = by_orcid[0]
            owner, _ = _owner(a, ctx)
            extra = []
            if a.author_orcid == ctx.my_orcid and a.author_id:
                extra.append(f"duplicate-profile:{short_id(a.author_id)}")
            tier = match_tier(a.raw_author_name, ctx.seeds)
            action = row(
                a, "claim", None, anchor="orcid", **_authorship_evidence(a, tier, owner, None)
            )
            action = rekeyed(action, reasons=[*extra, *action.reasons])
            return post_rule(action, work)
        if len(by_orcid) > 1:
            return row(None, "review", "ambiguous", anchor="orcid", bylines=_bylines(work))
    # 4-5. byline gate
    matches = matching_authorships(work, ctx.seeds)
    if not matches:
        return row(None, "review", "no-byline", bylines=_bylines(work))
    if len(matches) > 1:
        return row(
            None,
            "review",
            "ambiguous",
            candidates=[f"{a.position}:{a.raw_author_name} ({t})" for a, t in matches],
        )
    a, tier = matches[0]
    owner, lookup_failed = _owner(a, ctx)
    overlap = coauthor_overlap(work, a, ctx.coauthors)
    aev = {"anchor": "byline", **_authorship_evidence(a, tier, owner, overlap)}
    if lookup_failed:
        return row(a, "review", "owner-lookup-failed", **aev)
    is_splinter = owner is not None and owner.works_count <= ctx.splinter_max
    # 6. initial-only
    if tier == "initial" and not (
        overlap is not None and overlap >= ctx.coauthor_min and (owner is None or is_splinter)
    ):
        return row(a, "review", "initial-only", **aev)
    # 7. unowned
    if owner is None:
        return post_rule(row(a, "claim", None, **aev), work)
    # 8. owner carries another ORCID
    if owner.orcid and owner.orcid != ctx.my_orcid:
        return row(a, "review", "conflicting-orcid", **aev)
    # 9. splinter profile
    if is_splinter:
        return post_rule(row(a, "claim", None, **aev), work)
    # 10. larger profile
    return row(a, "review", "owned-by-larger-profile", **aev)


RemoveOrigin = Literal["explicit", "audit"]


def classify_remove(
    work: WorkView, ctx: Context, *, origin: RemoveOrigin, basis: str, refs: Sequence[str]
) -> Action:
    """Classify a work the target author may want detached (the remove table of §6.7)."""
    ev = _work_evidence(work, basis, refs) | dict(ctx.extra_evidence)
    mine = work.authorships_of(ctx.target_id)

    def row(status: str, reasons: list[str], a: AuthorshipView | None, **more: Any) -> Action:
        """Assemble a removal row with the default decision for its status."""
        return Action(
            kind="remove_work",
            entity_id=work.id,
            value=ctx.target_id,
            raw_author_name=a.raw_author_name if a else None,
            status=status,  # type: ignore[arg-type]
            decision="apply" if status == "remove" else "skip",
            reasons=[*reasons, *(["retracted"] if work.is_retracted else [])],
            evidence=dict(sorted((ev | more).items())),
        )

    if not mine:
        return row("not_on_profile", [], None)
    # A removal detaches every authorship the target holds, so every one of them is checked.
    own = [m for m in mine if ctx.my_orcid and m.raw_orcid == ctx.my_orcid]
    a = own[0] if own else mine[0]
    aev = _authorship_evidence(a, match_tier(a.raw_author_name, ctx.seeds), None, None)
    if len(mine) > 1:
        aev["other_positions"] = [m.position for m in mine if m is not a]
    if own:
        if ctx.force:
            return row("remove", ["forced-despite-orcid"], a, **aev)
        return row("protected", ["raw-orcid-is-mine"], a, **aev)
    if origin == "explicit":
        return row("remove", [], a, **aev)
    if ctx.my_orcid and all(m.raw_orcid and m.raw_orcid != ctx.my_orcid for m in mine):
        return row("remove", ["foreign-raw-orcid"], a, **aev)
    return row("review", ["byline-mismatch"], a, **aev)


def pick(action: Action, work: WorkView, position: int, ctx: Context) -> Action:
    """Anchor a claim row on the authorship at ``position`` (1-based) chosen by hand."""
    chosen = next((a for a in work.authorships if a.position == position), None)
    if chosen is None:
        raise ValueError(f"{short_id(work.id)} has no authorship at position {position}")
    owner, _ = _owner(chosen, ctx)
    tier = match_tier(chosen.raw_author_name, ctx.seeds)
    evidence = dict(action.evidence)
    for key in ("bylines", "candidates"):
        evidence.pop(key, None)
    evidence |= {
        "anchor": "pick",
        **_authorship_evidence(chosen, tier, owner, evidence.get("coauthor_overlap")),
    }
    picked = rekeyed(
        action,
        raw_author_name=chosen.raw_author_name,
        status="claim",
        decision="apply",
        reasons=[*action.reasons, f"picked-position-{position}"],
        evidence=dict(sorted(evidence.items())),
    )
    return post_rule(picked, work)
