"""Profile audit: removal candidates, addition candidates and duplicate profiles (PLAN §6.8).

Follows OpenAlex's "Audit a profile" recipe (https://help.openalex.org/tutorials/audit-a-profile)
and extends it with the classification tables of ``classify.py``.
"""

import math
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace

from oacurate.classify import Context, classify_add, classify_remove
from oacurate.client import COST_LIST, COST_SEARCH, MAX_URL_BYTES, OpenAlexClient
from oacurate.errors import BudgetExceeded
from oacurate.ids import orcid_bare, short_id
from oacurate.model import Action, AuthorView, WorkView
from oacurate.names import ladder_phrases, match_tier, matching_authorships
from oacurate.resolve import merge_actions

Notify = Callable[[str], None]
LIST_SELECT = (
    "id",
    "doi",
    "title",
    "display_name",
    "publication_year",
    "type",
    "is_paratext",
    "is_retracted",
    "primary_location",
    "authorships",
)
TRUNCATED_AUTHORSHIPS = 100
PAGE = 100


@dataclass(frozen=True)
class AuditOptions:
    """Audit knobs (command-line flags)."""

    threshold: int = 100
    max_step: int = 5
    merge_duplicates: bool = False
    max_credits: int | None = 200


@dataclass
class AuditResult:
    """The audit's actions plus what the summary needs."""

    actions: list[Action] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    estimate: int = 0
    rung: int = 0
    rung_count: int = 0
    rung_counts: list[int] = field(default_factory=list)


def estimate_cost(works_count: int, probes: int, threshold: int, has_orcid: bool) -> int:
    """Credits estimated before anything billed runs (upper bound on ladder probes)."""
    profile_pages = math.ceil(max(works_count, 1) / PAGE) * COST_LIST
    probes = probes * COST_SEARCH
    candidate_pages = math.ceil(max(threshold, 1) / PAGE) * COST_SEARCH
    return profile_pages + probes + candidate_pages + (COST_LIST if has_orcid else 0)


def _full(client: OpenAlexClient, work: dict, warnings: list[str]) -> WorkView:
    """Re-fetch a list item in full (free) when its authorship list may be truncated."""
    if len(work.get("authorships") or []) >= TRUNCATED_AUTHORSHIPS:
        full = client.get_work(work["id"])
        if full is None:
            warnings.append(
                f"{short_id(work['id'])}: full lookup failed; judged on a truncated byline list"
            )
        work = full or work
    return WorkView.from_json(work)


def _lookup_failed(wid: str) -> str:
    """Warning for a candidate whose single lookup failed."""
    return (
        f"{short_id(wid)}: found by a list query, but its single lookup failed (see U6); "
        "not classified"
    )


def search_filters(client: OpenAlexClient, phrases: Sequence[str]) -> list[str]:
    """OR the phrases into ``raw_author_name.search`` filters, split to fit the URL limit."""
    groups: list[list[str]] = []
    current: list[str] = []
    for phrase in phrases:
        trial = [*current, phrase]
        params = client.works_params(_rung_filter(trial), select=LIST_SELECT, per_page=PAGE)
        params["cursor"] = "*"
        if current and client.url_length("/works", params) > MAX_URL_BYTES:
            groups.append(current)
            current = [phrase]
        else:
            current = trial
    if current:
        groups.append(current)
    return [_rung_filter(g) for g in groups]


def _rung_filter(phrases: Sequence[str]) -> str:
    """The works filter for one OR group of byline phrases."""
    return f"raw_author_name.search:{' OR '.join(phrases)},type:!paratext"


def _iter_union(client: OpenAlexClient, filters: Sequence[str]) -> Iterator[dict]:
    """Page through each filter and yield each work once."""
    seen: set[str] = set()
    for f in filters:
        for work in client.iter_works(f, select=LIST_SELECT, per_page=PAGE):
            if work["id"] not in seen:
                seen.add(work["id"])
                yield work


def run_audit(
    client: OpenAlexClient,
    author: AuthorView,
    ctx: Context,
    opts: AuditOptions,
    notify: Notify = lambda _: None,
) -> AuditResult:
    """Run steps A-F and return the classified actions."""
    result = AuditResult()
    filters_by_step = {
        s: search_filters(client, ladder_phrases(ctx.seeds, s)) for s in range(1, opts.max_step + 1)
    }
    probes = sum(len(f) for f in filters_by_step.values())
    result.estimate = estimate_cost(author.works_count, probes, opts.threshold, bool(ctx.my_orcid))
    notify(f"estimated cost: ~{result.estimate} credits (upper bound on ladder probes)")
    if opts.max_credits is not None and result.estimate > opts.max_credits:
        raise BudgetExceeded(
            f"estimated ~{result.estimate} credits exceeds --max_credits {opts.max_credits}; "
            "raise --max_credits or lower --max_step / --threshold. Nothing was sent."
        )
    target_short = short_id(author.id)

    # A. profile works
    profile: list[WorkView] = [
        _full(client, w, result.warnings)
        for w in client.iter_works(
            f"author.id:{target_short},type:!paratext", select=LIST_SELECT, per_page=PAGE
        )
    ]
    on_profile = {w.id for w in profile}
    coauthors: Counter[str] = Counter(
        a.author_id
        for w in profile
        for a in w.authorships
        if a.author_id and a.author_id != author.id
    )
    notify(f"profile: {len(profile)} works, {len(coauthors)} distinct co-authors")
    ctx = replace(ctx, coauthors=coauthors)

    # B. removal candidates
    actions: list[Action] = []
    for w in profile:
        mine = w.authorships_of(author.id)
        if mine and all(match_tier(a.raw_author_name, ctx.seeds) == "none" for a in mine):
            actions.append(
                classify_remove(w, ctx, origin="audit", basis="audit", refs=["audit:profile"])
            )

    # C. addition candidates
    chosen, chosen_counts = opts.max_step, []
    for step in range(1, opts.max_step + 1):
        counts = [client.count_works(f) for f in filters_by_step[step]]
        result.rung_counts.append(sum(counts))
        chosen_counts = counts
        notify(f"ladder step {step}: {sum(counts)} works")
        if sum(counts) >= opts.threshold:
            chosen = step
            break
    result.rung, result.rung_count = chosen, sum(chosen_counts)
    pages = sum(math.ceil(c / PAGE) for c in chosen_counts) * COST_SEARCH
    notify(f"paging ladder step {chosen}: ~{pages} credits")
    client.budget.check(client.credits_used, pages)
    candidates = 0
    for item in _iter_union(client, filters_by_step[chosen]):
        if item["id"] in on_profile:
            continue
        truncated = len(item.get("authorships") or []) >= TRUNCATED_AUTHORSHIPS
        data = client.get_work(item["id"]) if truncated else item
        if data is None:
            result.warnings.append(_lookup_failed(item["id"]))
            continue
        work = WorkView.from_json(data)
        if not matching_authorships(work, ctx.seeds) and not _has_orcid(work, ctx.my_orcid):
            continue
        full = data if truncated else client.get_work(work.id)
        if full is None:
            result.warnings.append(_lookup_failed(work.id))
            continue
        candidates += 1
        actions.append(
            classify_add(WorkView.from_json(full), ctx, basis="audit", refs=[f"audit:step{chosen}"])
        )
    notify(f"addition candidates passing the byline gate: {candidates}")

    # D. duplicate profiles
    if ctx.my_orcid:
        others = [
            a
            for a in client.list_authors(
                f"orcid:{orcid_bare(ctx.my_orcid)}", select=["id", "display_name", "works_count"]
            )
            if a.get("id") != author.id
        ]
        for other in others:
            dup = short_id(other["id"])
            result.duplicates.append(dup)
            result.warnings.append(
                f"duplicate profile {dup} ({other.get('display_name')}, "
                f"{other.get('works_count')} works) carries this ORCID; "
                "setting the ORCID does not merge profiles"
            )
            if opts.merge_duplicates:
                for item in client.iter_works(
                    f"author.id:{dup},type:!paratext", select=LIST_SELECT, per_page=PAGE
                ):
                    if item["id"] in on_profile:
                        continue
                    full = client.get_work(item["id"])
                    if full is None:
                        result.warnings.append(_lookup_failed(item["id"]))
                    else:
                        actions.append(
                            classify_add(
                                WorkView.from_json(full),
                                ctx,
                                basis="audit",
                                refs=[f"audit:duplicate:{dup}"],
                            )
                        )
    result.actions = merge_actions(actions)
    return result


def _has_orcid(work: WorkView, my_orcid: str | None) -> bool:
    """Tell whether any authorship of ``work`` carries ``my_orcid``."""
    return bool(my_orcid) and any(
        my_orcid in (a.raw_orcid, a.author_orcid) for a in work.authorships
    )
