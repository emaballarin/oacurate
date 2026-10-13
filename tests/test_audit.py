"""Audit: profile scan, removal and addition candidates, ladder choice, duplicates, cost guard."""

import httpx
import pytest
from conftest import MY_ORCID, OTHER_ORCID, TARGET, FakeApi, author, authorship, json_response, work

from oacurate.audit import LIST_SELECT, AuditOptions, run_audit, search_filters
from oacurate.classify import Context
from oacurate.client import MAX_URL_BYTES
from oacurate.errors import BudgetExceeded
from oacurate.model import AuthorView
from oacurate.names import build_seeds, ladder_phrases

ME = author("A9", "Jane Doe", works_count=4, orcid=MY_ORCID)
DUP = "https://openalex.org/A6"
FILLER = [authorship(f"Filler {i}", f"https://openalex.org/A{100 + i}") for i in range(100)]

PROFILE = [
    work("W1", [authorship("Jane Doe", TARGET), authorship("Co One", "https://openalex.org/A11")]),
    work("W2", [authorship("Jim Roe", TARGET, raw_orcid=OTHER_ORCID)]),
    work("W3", [authorship("Jim Roe", TARGET)]),
]
CANDIDATES = [
    work("W20", [authorship("Jane Doe"), authorship("Co One", "https://openalex.org/A11")]),
    work("W21", [authorship("Bob Roe")]),
    work("W22", [*FILLER, authorship("Jane Doe")]),  # byline beyond the 100 a list returns
]
DUPLICATE_WORKS = [work("W30", [authorship("J. Doe", DUP, orcid=MY_ORCID)])]


def truncated(w):
    return {**w, "authorships": w["authorships"][:100]}


def page(results, count=None):
    return {
        "meta": {"count": len(results) if count is None else count, "next_cursor": None},
        "results": results,
    }


def fake_openalex():
    api = FakeApi(
        PROFILE + CANDIDATES + DUPLICATE_WORKS,
        [ME, author("A6", "J. Doe", works_count=1, orcid=MY_ORCID)],
    )

    def lists(request: httpx.Request) -> httpx.Response:
        p = request.url.params
        f = p["filter"]
        cost = 10 if ".search" in f else 1
        if request.url.path == "/authors":
            return json_response(200, page([ME, api.authors["A6"]]), credits=cost)
        if f.startswith("author.id:A9"):
            return json_response(200, page(PROFILE), credits=cost)
        if f.startswith("author.id:A6"):
            return json_response(200, page(DUPLICATE_WORKS), credits=cost)
        if f.startswith("raw_author_name.search:"):
            if p["per_page"] == "1":
                widened = '"doe jane"' in f
                return json_response(200, page([], count=150 if widened else 5), credits=cost)
            hits = [PROFILE[0], CANDIDATES[0], CANDIDATES[1], truncated(CANDIDATES[2])]
            return json_response(200, page(hits), credits=cost)
        raise AssertionError(f"unexpected list request {request.url}")

    api.list_handler = lists
    return api


def context(**kw):
    target = AuthorView.from_json(ME)
    seeds = build_seeds(target.display_name, ["Jane Doe"])
    return target, Context(
        target_id=TARGET,
        my_orcid=MY_ORCID,
        seeds=seeds,
        owner_of=lambda aid: None,
        **kw,
    )


def test_audit_end_to_end(make_client):
    client, rec = make_client(fake_openalex())
    target, ctx = context()
    result = run_audit(
        client, target, ctx, AuditOptions(threshold=100, max_step=5, merge_duplicates=True)
    )
    assert (result.rung, result.rung_counts) == (2, [5, 150])
    by_work = {a.evidence["work"]: (a.kind, a.status) for a in result.actions}
    assert by_work == {
        "W2": ("remove_work", "remove"),
        "W3": ("remove_work", "review"),
        "W20": ("claim_work", "claim"),
        "W22": ("claim_work", "claim"),
        "W30": ("claim_work", "claim"),
    }
    w30 = next(a for a in result.actions if a.evidence["work"] == "W30")
    assert "duplicate-profile:A6" in w30.reasons
    assert result.duplicates == ["A6"]
    # profile page 1 + ladder probes 2x10 + candidate page 10 + authors 1 + duplicate works 1
    assert client.credits_used == 33
    assert all(r.url.params.get("corpus") == "all" for r in rec.requests if r.url.path == "/works")


def test_the_estimate_is_enforced_before_anything_billed(make_client):
    client, rec = make_client(fake_openalex())
    target, ctx = context()
    with pytest.raises(BudgetExceeded, match="Nothing was sent"):
        run_audit(client, target, ctx, AuditOptions(max_credits=20))
    assert rec.requests == []


def test_long_or_lists_are_split_to_fit_the_url_limit(make_client):
    client, _ = make_client(lambda r: pytest.fail("no request expected"))
    phrases = [f'"{"x" * 25} name{i:03d}"~2' for i in range(300)]
    filters = search_filters(client, phrases)
    assert len(filters) > 1
    for f in filters:
        params = client.works_params(f, select=LIST_SELECT, per_page=100) | {"cursor": "*"}
        assert client.url_length("/works", params) <= MAX_URL_BYTES
    joined = " OR ".join(
        f.removeprefix("raw_author_name.search:").removesuffix(",type:!paratext") for f in filters
    )
    assert joined.split(" OR ") == phrases


def test_a_candidate_whose_single_lookup_fails_is_warned_about(make_client):
    api = fake_openalex()
    del api.works["W20"]
    client, _ = make_client(api)
    target, ctx = context()
    result = run_audit(client, target, ctx, AuditOptions(threshold=100, max_step=5))
    assert "W20" not in {a.evidence["work"] for a in result.actions}
    assert any(w.startswith("W20: found by a list query") for w in result.warnings)


def test_the_estimate_counts_every_probe_after_splitting(make_client):
    client, rec = make_client(fake_openalex())
    target = AuthorView.from_json(ME)
    names = [f"Jane {'q' * 40}{chr(97 + i // 26)}{chr(97 + i % 26)} Doe" for i in range(150)]
    ctx = Context(
        target_id=TARGET,
        my_orcid=MY_ORCID,
        seeds=build_seeds("Jane Doe", [], [], names),
        owner_of=lambda _: None,
    )
    probes = sum(len(search_filters(client, ladder_phrases(ctx.seeds, s))) for s in range(1, 6))
    assert probes > 5
    expected = 1 + probes * 10 + 10 + 1
    with pytest.raises(BudgetExceeded, match=f"~{expected} credits"):
        run_audit(client, target, ctx, AuditOptions(max_credits=expected - 1))
    assert rec.requests == []


def test_a_duplicate_work_whose_single_lookup_fails_is_warned_about(make_client):
    api = fake_openalex()
    del api.works["W30"]
    client, _ = make_client(api)
    target, ctx = context()
    result = run_audit(
        client, target, ctx, AuditOptions(threshold=100, max_step=5, merge_duplicates=True)
    )
    assert any(w.startswith("W30: found by a list query") for w in result.warnings)


def test_a_truncated_candidate_that_cannot_be_fetched_gets_one_accurate_warning(make_client):
    api = fake_openalex()
    del api.works["W22"]
    client, _ = make_client(api)
    target, ctx = context()
    result = run_audit(client, target, ctx, AuditOptions(threshold=100, max_step=5))
    assert [w for w in result.warnings if w.startswith("W22")] == [
        "W22: found by a list query, but its single lookup failed (see U6); not classified"
    ]
