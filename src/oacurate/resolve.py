"""Resolution of input records to OpenAlex works, and assembly of add/remove actions."""

import difflib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from oacurate.bib import InputRecord, norm_title
from oacurate.classify import Context, classify_add, classify_remove
from oacurate.client import COST_SEARCH, OpenAlexClient
from oacurate.ids import arxiv_doi, short_id
from oacurate.model import Action, UnresolvedInput, WorkView, action_sort_key, rekeyed

NEAR_TITLE_RATIO = 0.92
SEARCH_SELECT = ("id", "doi", "title", "display_name", "publication_year", "type")


@dataclass(frozen=True)
class Hit:
    """One candidate work for an input record."""

    work_id: str
    basis: str
    cap_reason: str | None = None


@dataclass(frozen=True)
class Resolution:
    """The candidate works found for one input record, plus every search hit seen."""

    record: InputRecord
    hits: tuple[Hit, ...]
    search_hits: tuple[str, ...] = ()


def _found(client: OpenAlexClient, ref: str) -> dict | None:
    """Full single lookup (free) of a work by ID or DOI."""
    return client.get_work(ref)


def resolve_identifiers(
    client: OpenAlexClient, records: Iterable[InputRecord]
) -> tuple[list[Resolution], list[InputRecord], list[UnresolvedInput]]:
    """Resolve by work ID, DOI and arXiv DOI (all free single lookups).

    Returns the resolutions, the records left for a title search, and the records that
    cannot be resolved at all.
    """
    resolved: list[Resolution] = []
    need_search: list[InputRecord] = []
    unresolved: list[UnresolvedInput] = []
    for rec in records:
        if rec.work_id:
            work = _found(client, rec.work_id)
            if work:
                resolved.append(Resolution(rec, (Hit(work["id"], "work-id"),)))
            else:
                unresolved.append(_missing(rec, f"{rec.work_id} does not resolve"))
            continue
        tried: list[str] = []
        if rec.doi:
            tried.append(f"DOI {rec.doi}")
            work = _found(client, "doi:" + rec.doi)
            if work:
                resolved.append(Resolution(rec, (Hit(work["id"], "doi"),)))
                continue
        if rec.arxiv_id:
            tried.append(f"arXiv {rec.arxiv_id}")
            work = _found(client, "doi:" + arxiv_doi(rec.arxiv_id))
            if work:
                resolved.append(Resolution(rec, (Hit(work["id"], "arxiv"),)))
                continue
        if rec.title:
            need_search.append(rec)
        else:
            detail = f"{', '.join(tried)} not found" if tried else "no DOI, arXiv ID or title"
            unresolved.append(_missing(rec, detail + " in OpenAlex"))
    return resolved, need_search, unresolved


def _missing(rec: InputRecord, detail: str) -> UnresolvedInput:
    """An unresolved input whose work is absent from OpenAlex."""
    return UnresolvedInput(ref=rec.ref, reason="missing", detail=detail, record=rec.to_dict())


def search_cost(records: Sequence[InputRecord]) -> int:
    """Estimated credits for title-searching ``records``."""
    return len(records) * COST_SEARCH


def score_title(hit_title: str, wanted: str) -> str | None:
    """Return ``title-exact``, ``title-near`` or ``None`` for one search hit."""
    a, b = norm_title(hit_title), norm_title(wanted)
    if not a or not b:
        return None
    if a == b:
        return "title-exact"
    if difflib.SequenceMatcher(None, a, b).ratio() >= NEAR_TITLE_RATIO:
        return "title-near"
    return None


def resolve_by_title(
    client: OpenAlexClient, records: Iterable[InputRecord]
) -> tuple[list[Resolution], list[UnresolvedInput]]:
    """Title-search each record (billed: one search each) and keep exact or near hits."""
    resolved: list[Resolution] = []
    unresolved: list[UnresolvedInput] = []
    for rec in records:
        query = norm_title(rec.title)
        results = client.search_works_by_title(
            f'"{query}"', filter="type:!paratext", select=SEARCH_SELECT
        )
        exact, near, seen = [], [], []
        for hit in results:
            title = hit.get("title") or hit.get("display_name") or ""
            score = score_title(title, rec.title)
            seen.append(f"{short_id(hit['id'])} [{score or 'no match'}] {title}")
            if score == "title-exact":
                exact.append(hit["id"])
            elif score == "title-near":
                near.append(hit["id"])
        cap = "multiple-versions" if len(exact) > 1 else None
        hits = [Hit(w, "title-exact", cap) for w in exact]
        hits += [Hit(w, "title-near", "title-near") for w in near]
        if hits:
            resolved.append(Resolution(rec, tuple(hits), tuple(seen)))
        else:
            listing = "; ".join(seen) or "none"
            unresolved.append(
                _missing(rec, f"no exact or near title match in OpenAlex (hits: {listing})")
            )
    return resolved, unresolved


def cap_at_review(action: Action, reason: str) -> Action:
    """Demote an applicable claim to ``review`` with ``reason``."""
    if action.status != "claim":
        return action
    return rekeyed(action, status="review", decision="skip", reasons=[*action.reasons, reason])


def merge_actions(actions: Iterable[Action]) -> list[Action]:
    """Merge rows for the same action ID, keeping the strongest and uniting input refs."""
    by_id: dict[str, Action] = {}
    for a in actions:
        kept = by_id.get(a.id)
        if kept is None:
            by_id[a.id] = a
            continue
        best, other = sorted([kept, a], key=action_sort_key)
        refs = sorted(set(best.evidence.get("input", [])) | set(other.evidence.get("input", [])))
        merged = best.evidence | {"input": refs}
        hits = [*best.evidence.get("search_hits", []), *other.evidence.get("search_hits", [])]
        if hits:
            merged["search_hits"] = list(dict.fromkeys(hits))
        by_id[a.id] = rekeyed(best, evidence=dict(sorted(merged.items())))
    return list(by_id.values())


def _lookup_failed(res: Resolution, hit: Hit) -> UnresolvedInput:
    """An input whose candidate work was found but cannot be fetched by single lookup."""
    return UnresolvedInput(
        ref=res.record.ref,
        reason="lookup-failed",
        detail=f"{short_id(hit.work_id)} ({hit.basis}) was found, but its single lookup failed "
        "(an expansion-corpus work? see U6 in docs/api-notes.md)",
        record=res.record.to_dict(),
    )


def add_actions(
    client: OpenAlexClient, resolutions: Iterable[Resolution], ctx: Context
) -> tuple[list[Action], list[UnresolvedInput]]:
    """Fetch every hit in full (free) and classify it for attachment."""
    actions, unresolved = [], []
    for res in resolutions:
        for hit in res.hits:
            data = client.get_work(hit.work_id)
            if data is None:
                unresolved.append(_lookup_failed(res, hit))
                continue
            action = classify_add(
                WorkView.from_json(data), ctx, basis=hit.basis, refs=[res.record.ref]
            )
            if res.search_hits:
                evidence = dict(
                    sorted((action.evidence | {"search_hits": list(res.search_hits)}).items())
                )
                action = rekeyed(action, evidence=evidence)
            if hit.cap_reason:
                action = cap_at_review(action, hit.cap_reason)
            actions.append(action)
    return merge_actions(actions), unresolved


def remove_actions(
    client: OpenAlexClient, resolutions: Iterable[Resolution], ctx: Context
) -> tuple[list[Action], list[UnresolvedInput]]:
    """Fetch every hit in full (free) and classify it for an explicit removal."""
    actions, unresolved = [], []
    for res in resolutions:
        for hit in res.hits:
            data = client.get_work(hit.work_id)
            if data is None:
                unresolved.append(_lookup_failed(res, hit))
                continue
            work = WorkView.from_json(data)
            actions.append(
                classify_remove(
                    work, ctx, origin="explicit", basis=hit.basis, refs=[res.record.ref]
                )
            )
    return merge_actions(actions), unresolved
