"""Resolution order (DOI, arXiv, title search), outcome mapping, merging, and zero cost."""

from conftest import TARGET, FakeApi, author, authorship, json_response, load, work

from oacurate.bib import InputRecord
from oacurate.classify import Context
from oacurate.model import AuthorView
from oacurate.names import Seeds
from oacurate.resolve import add_actions, resolve_by_title, resolve_identifiers

PAPER = work(
    "W10", [authorship("Jane Doe")], doi="https://doi.org/10.1000/paper", title="Wavelets on Graphs"
)
PREPRINT = work(
    "W11",
    [authorship("Jane Doe")],
    doi="https://doi.org/10.48550/arxiv.2401.01234",
    title="Koopman Operators",
)
PROCEEDINGS = work("W12", [authorship("Jane Doe")], title="Koopman Operators", year=2025)


def context(seeds) -> Context:
    return Context(target_id=TARGET, my_orcid=None, seeds=seeds, owner_of=lambda _: None)


def test_identifier_resolution_order(make_client):
    client, _ = make_client(FakeApi([PAPER, PREPRINT]))
    records = [
        InputRecord(ref="doi:paper", doi="10.1000/paper"),
        InputRecord(ref="bib:pre", doi="10.1000/gone", arxiv_id="2401.01234"),
        InputRecord(ref="bib:title", doi="10.1000/gone", title="Something Else"),
        InputRecord(ref="doi:nothing", doi="10.1000/gone"),
        InputRecord(ref="work:W10", work_id="https://openalex.org/W10"),
    ]
    resolved, need_search, unresolved = resolve_identifiers(client, records)
    assert [(r.record.ref, r.hits[0].basis, r.hits[0].work_id[-3:]) for r in resolved] == [
        ("doi:paper", "doi", "W10"),
        ("bib:pre", "arxiv", "W11"),
        ("work:W10", "work-id", "W10"),
    ]
    assert [r.ref for r in need_search] == ["bib:title"]
    assert [(u.ref, u.reason) for u in unresolved] == [("doi:nothing", "missing")]
    assert client.credits_used == 0


def test_a_plan_from_a_doi_list_costs_nothing(make_client, seeds):
    api = FakeApi([PAPER])
    client, rec = make_client(api)
    resolved, _, _ = resolve_identifiers(
        client, [InputRecord(ref="doi:paper", doi="10.1000/paper")]
    )
    [action], _ = add_actions(client, resolved, context(seeds))
    assert action.status == "claim"
    assert all(r.url.path not in ("/works", "/authors") for r in rec.requests)
    assert client.credits_used == 0


def search_api(hits):
    api = FakeApi([PAPER, PREPRINT, PROCEEDINGS])
    api.list_handler = lambda r: json_response(
        200, {"meta": {"count": len(hits)}, "results": hits}, credits=10
    )
    return api


def test_title_search_request_shape(make_client):
    client, rec = make_client(search_api([]))
    resolve_by_title(client, [InputRecord(ref="bib:t", title="Koopman {O}perators -- a Survey")])
    [req] = rec.requests
    assert req.url.params["search.title"] == '"koopman operators a survey"'
    assert req.url.params["filter"] == "type:!paratext"
    assert (req.url.params["corpus"], req.url.params["per_page"]) == ("all", "10")
    assert client.credits_used == 10


def hit(w):
    return {"id": w["id"], "title": w["title"], "publication_year": w["publication_year"]}


def test_a_single_exact_hit_is_classified_normally_whatever_its_year(make_client, seeds):
    client, _ = make_client(
        search_api([hit(PAPER), {"id": "https://openalex.org/W99", "title": "Unrelated"}])
    )
    resolved, unresolved = resolve_by_title(
        client, [InputRecord(ref="bib:t", title="Wavelets on {G}raphs", year=1990)]
    )
    [action], _ = add_actions(client, resolved, context(seeds))
    assert (action.status, action.evidence["basis"], unresolved) == ("claim", "title-exact", [])


def test_several_exact_hits_are_all_capped_at_review(make_client, seeds):
    client, _ = make_client(search_api([hit(PREPRINT), hit(PROCEEDINGS)]))
    resolved, _ = resolve_by_title(client, [InputRecord(ref="bib:t", title="Koopman Operators")])
    actions, _ = add_actions(client, resolved, context(seeds))
    assert sorted(a.evidence["work"] for a in actions) == ["W11", "W12"]
    assert all(a.status == "review" and "multiple-versions" in a.reasons for a in actions)


def test_a_near_title_hit_only_is_review(make_client, seeds):
    client, _ = make_client(search_api([hit(PAPER)]))
    resolved, _ = resolve_by_title(client, [InputRecord(ref="bib:t", title="Wavelets on Graph")])
    [action], _ = add_actions(client, resolved, context(seeds))
    assert (action.status, action.reasons) == ("review", ["title-near"])


def test_no_hit_is_missing(make_client):
    client, _ = make_client(
        search_api([{"id": "https://openalex.org/W99", "title": "Something else entirely"}])
    )
    resolved, unresolved = resolve_by_title(
        client, [InputRecord(ref="bib:t", title="Wavelets on Graphs")]
    )
    assert resolved == [] and [(u.ref, u.reason) for u in unresolved] == [("bib:t", "missing")]


def test_the_same_work_from_two_inputs_becomes_one_row(make_client, seeds):
    client, _ = make_client(search_api([hit(PAPER)]))
    by_doi, _, _ = resolve_identifiers(client, [InputRecord(ref="doi:paper", doi="10.1000/paper")])
    by_title, _ = resolve_by_title(
        client, [InputRecord(ref="bib:paper", title="Wavelets on Graph")]
    )
    [action], _ = add_actions(client, by_doi + by_title, context(seeds))
    assert action.status == "claim" and action.evidence["basis"] == "doi"
    assert action.evidence["input"] == ["bib:paper", "doi:paper"]


def test_near_versions_next_to_a_single_exact_hit_are_review_rows_and_every_hit_is_listed(
    make_client, seeds
):
    near = work("W13", [authorship("Jane Doe")], title="Wavelets on Graph")
    api = search_api(
        [hit(PAPER), hit(near), {"id": "https://openalex.org/W99", "title": "Unrelated"}]
    )
    api.works["W13"] = near
    client, _ = make_client(api)
    resolved, _ = resolve_by_title(client, [InputRecord(ref="bib:t", title="Wavelets on Graphs")])
    actions, _ = add_actions(client, resolved, context(seeds))
    rows = {a.evidence["work"]: (a.status, a.reasons) for a in actions}
    assert rows == {"W10": ("claim", []), "W13": ("review", ["title-near"])}
    assert actions[0].evidence["search_hits"] == [
        "W10 [title-exact] Wavelets on Graphs",
        "W13 [title-near] Wavelets on Graph",
        "W99 [no match] Unrelated",
    ]


def test_a_hit_whose_single_lookup_fails_is_reported_not_dropped(make_client, seeds):
    ghost = {"id": "https://openalex.org/W77", "title": "Wavelets on Graphs"}
    client, _ = make_client(search_api([ghost]))
    resolved, _ = resolve_by_title(client, [InputRecord(ref="bib:t", title="Wavelets on Graphs")])
    actions, unresolved = add_actions(client, resolved, context(seeds))
    assert actions == []
    assert [(u.ref, u.reason) for u in unresolved] == [("bib:t", "lookup-failed")]


def test_a_doi_lookup_may_return_a_work_with_another_doi_and_an_odd_year(make_client):
    recorded = load("works", "W2626778328")
    api = FakeApi([recorded])
    api.by_doi["10.48550/arxiv.1706.03762"] = recorded
    client, _ = make_client(api)
    rec = InputRecord(ref="bib:vaswani", arxiv_id="1706.03762", year=2017)
    resolved, _, _ = resolve_identifiers(client, [rec])
    larger = AuthorView.from_json(author("A5021878400", "Noam Shazeer", works_count=58))
    ctx = Context(
        target_id=TARGET,
        my_orcid=None,
        seeds=Seeds.from_names(["Noam Shazeer"]),
        owner_of=lambda _: larger,
    )
    [action], _ = add_actions(client, resolved, ctx)
    ev = action.evidence
    assert (ev["basis"], ev["doi"], ev["year"], ev["position"]) == (
        "arxiv",
        "10.65215/2q58a426",
        2025,
        2,
    )
    assert (action.status, action.reasons) == ("review", ["owned-by-larger-profile"])


def test_merging_keeps_the_search_hits_of_the_weaker_row(make_client, seeds):
    client, _ = make_client(search_api([hit(PAPER)]))
    by_doi, _, _ = resolve_identifiers(client, [InputRecord(ref="doi:paper", doi="10.1000/paper")])
    by_title, _ = resolve_by_title(
        client, [InputRecord(ref="bib:paper", title="Wavelets on Graphs")]
    )
    [action], _ = add_actions(client, by_doi + by_title, context(seeds))
    assert action.evidence["basis"] == "doi"
    assert action.evidence["search_hits"] == ["W10 [title-exact] Wavelets on Graphs"]
