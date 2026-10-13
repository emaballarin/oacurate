"""Classification: one test per row of each table in PLAN §6.7, the post-rule, and pick."""

import pytest
from conftest import MY_ORCID, OTHER_ORCID, TARGET, author, authorship, load, work

from oacurate.classify import Context, classify_add, classify_remove, pick
from oacurate.model import AuthorView, WorkView
from oacurate.names import build_seeds

OWNERS = {
    "https://openalex.org/A5": author("A5", "J. Doe", works_count=2),
    "https://openalex.org/A6": author("A6", "Jane Doe", works_count=40, orcid=MY_ORCID),
    "https://openalex.org/A7": author("A7", "Jane Doe", works_count=2, orcid=OTHER_ORCID),
    "https://openalex.org/A8": author("A8", "Jane Doe", works_count=50),
}


def ctx(seeds, **kw) -> Context:
    def owner_of(aid):
        data = OWNERS.get(aid)
        return AuthorView.from_json(data) if data else None

    return Context(target_id=TARGET, my_orcid=MY_ORCID, seeds=seeds, owner_of=owner_of, **kw)


def add(w, seeds, **kw):
    return classify_add(WorkView.from_json(w), ctx(seeds, **kw), basis="doi", refs=["doi:x"])


BOB = authorship("Bob Roe", "https://openalex.org/A1")


def test_row1_already_on_the_profile(seeds):
    a = add(work("W1", [BOB, authorship("Jane Doe", TARGET)]), seeds)
    assert (a.status, a.decision, a.raw_author_name) == ("already", "skip", "Jane Doe")


def test_row1_on_recorded_data():
    priem = AuthorView.from_json(load("authors", "A5023888391"))
    seeds = build_seeds(priem.display_name, priem.raw_author_names)
    c = Context(target_id=priem.id, my_orcid=priem.orcid, seeds=seeds, owner_of=lambda _: None)
    w = WorkView.from_json(load("works", "W2741809807"))
    a = classify_add(w, c, basis="doi", refs=["doi:10.7717/peerj.4375"])
    assert (a.status, a.evidence["position"], a.evidence["tier"]) == ("already", 2, "full")


def test_row2_paratext(seeds):
    a = add(work("W1", [authorship("Jane Doe")], is_paratext=True), seeds)
    assert (a.status, a.reasons) == ("review", ["paratext"])


def test_row3_raw_orcid_claims_even_when_the_byline_fails_the_gate(seeds):
    w = work("W1", [BOB, authorship("Q. Smith", "https://openalex.org/A8", raw_orcid=MY_ORCID)])
    a = add(w, seeds)
    assert (a.status, a.decision, a.evidence["anchor"]) == ("claim", "apply", "orcid")
    assert (a.raw_author_name, a.evidence["tier"]) == ("Q. Smith", "none")


def test_row3_a_profile_carrying_my_orcid_is_a_duplicate(seeds):
    w = work("W1", [BOB, authorship("Jane Doe", "https://openalex.org/A6", orcid=MY_ORCID)])
    a = add(w, seeds)
    assert a.status == "claim" and "duplicate-profile:A6" in a.reasons


def test_row4_no_byline_passes_the_gate(seeds):
    a = add(work("W1", [BOB, authorship("Jim Roe")]), seeds)
    assert (a.status, a.reasons, a.raw_author_name) == ("review", ["no-byline"], None)
    assert a.evidence["bylines"] == ["1:Bob Roe", "2:Jim Roe"]


def test_row5_two_j_doe_bylines_are_ambiguous(seeds):
    a = add(work("W1", [authorship("J. Doe"), BOB, authorship("J Doe")]), seeds)
    assert (a.status, a.reasons) == ("review", ["ambiguous"])
    assert len(a.evidence["candidates"]) == 2


def test_row6_initial_only_without_coauthor_evidence(seeds):
    a = add(work("W1", [BOB, authorship("J. Doe")]), seeds)
    assert (a.status, a.reasons, a.evidence["tier"]) == ("review", ["initial-only"], "initial")


def test_row6_initial_passes_with_coauthor_overlap_on_an_unowned_byline(seeds):
    coauthors = {f"https://openalex.org/A{i}": 2 for i in (11, 12, 13)}
    w = work(
        "W1",
        [
            authorship("J. Doe"),
            *(authorship(f"Co {i}", f"https://openalex.org/A{i}") for i in (11, 12, 13)),
        ],
    )
    a = add(w, seeds, coauthors=coauthors)
    assert (a.status, a.evidence["coauthor_overlap"]) == ("claim", 3)


def test_row6_overlap_does_not_rescue_an_initial_owned_by_a_larger_profile(seeds):
    coauthors = {f"https://openalex.org/A{i}": 2 for i in (11, 12, 13)}
    w = work(
        "W1",
        [
            authorship("J. Doe", "https://openalex.org/A8"),
            *(authorship(f"Co {i}", f"https://openalex.org/A{i}") for i in (11, 12, 13)),
        ],
    )
    assert add(w, seeds, coauthors=coauthors).reasons == ["initial-only"]


def test_row7_unowned_full_byline_is_claimed(seeds):
    a = add(work("W1", [BOB, authorship("Jane Doe")]), seeds)
    assert (a.status, a.decision, a.evidence["tier"], a.evidence["owner"]) == (
        "claim",
        "apply",
        "full",
        None,
    )


def test_row8_owner_with_another_orcid(seeds):
    a = add(work("W1", [BOB, authorship("Jane Doe", "https://openalex.org/A7")]), seeds)
    assert (a.status, a.reasons) == ("review", ["conflicting-orcid"])


def test_row9_splinter_owner_is_claimed(seeds):
    a = add(work("W1", [BOB, authorship("Jane Doe", "https://openalex.org/A5")]), seeds)
    assert (a.status, a.evidence["owner_works_count"]) == ("claim", 2)


def test_row10_larger_owner_needs_review(seeds):
    a = add(work("W1", [BOB, authorship("Jane Doe", "https://openalex.org/A8")]), seeds)
    assert (a.status, a.reasons) == ("review", ["owned-by-larger-profile"])


def test_a_failed_owner_lookup_is_never_claimed(seeds):
    a = add(work("W1", [BOB, authorship("Jane Doe", "https://openalex.org/A404")]), seeds)
    assert (a.status, a.reasons) == ("review", ["owner-lookup-failed"])


def test_post_rule_blocks_a_claim_on_a_repeated_byline(seeds):
    w = work("W1", [authorship("Jane Doe", raw_orcid=MY_ORCID), authorship("Jane Doe")])
    a = add(w, seeds)
    assert (a.status, a.decision, a.reasons) == ("blocked", "skip", ["duplicate-byline-anchor"])


def test_post_rule_blocks_a_byline_the_server_regex_cannot_recover(seeds):
    a = add(work("W1", [authorship("Jane\nDoe", raw_orcid=MY_ORCID)]), seeds)
    assert (a.status, a.reasons) == ("blocked", ["unanchorable-byline"])


def test_retraction_is_noted_without_changing_the_status(seeds):
    a = add(work("W1", [authorship("Jane Doe")], is_retracted=True), seeds)
    assert a.status == "claim" and "retracted" in a.reasons


def test_thresholds_are_parameters(seeds):
    w = work("W1", [authorship("Jane Doe", "https://openalex.org/A8")])
    assert add(w, seeds, splinter_max=50).status == "claim"


# ---------------------------------------------------------------- removal table


def remove(w, seeds, origin="explicit", **kw):
    return classify_remove(
        WorkView.from_json(w), ctx(seeds, **kw), origin=origin, basis="doi", refs=["doi:x"]
    )


def test_remove_not_on_profile(seeds):
    assert remove(work("W1", [BOB]), seeds).status == "not_on_profile"


def test_remove_is_refused_when_the_byline_carries_my_orcid(seeds):
    w = work("W1", [authorship("Jane Doe", TARGET, raw_orcid=MY_ORCID)])
    a = remove(w, seeds)
    assert (a.status, a.decision) == ("protected", "skip")
    forced = remove(w, seeds, force=True)
    assert (forced.status, forced.decision, forced.reasons) == (
        "remove",
        "apply",
        ["forced-despite-orcid"],
    )


def test_remove_explicit(seeds):
    a = remove(work("W1", [authorship("Jane Doe", TARGET)]), seeds)
    assert (a.status, a.decision, a.raw_author_name) == ("remove", "apply", "Jane Doe")


def test_remove_audit_with_a_foreign_raw_orcid(seeds):
    a = remove(
        work("W1", [authorship("Jim Roe", TARGET, raw_orcid=OTHER_ORCID)]), seeds, origin="audit"
    )
    assert (a.status, a.decision) == ("remove", "apply")


def test_remove_audit_otherwise_needs_review(seeds):
    a = remove(work("W1", [authorship("Jim Roe", TARGET)]), seeds, origin="audit")
    assert (a.status, a.decision) == ("review", "skip")


# ---------------------------------------------------------------- pick


def test_pick_anchors_the_chosen_byline_and_reruns_the_post_rule(seeds):
    w = work("W1", [authorship("J. Doe"), BOB, authorship("J Doe")])
    row = add(w, seeds)
    picked = pick(row, WorkView.from_json(w), 3, ctx(seeds))
    assert (picked.status, picked.decision, picked.raw_author_name) == ("claim", "apply", "J Doe")
    assert picked.id != row.id and "candidates" not in picked.evidence

    twin = work("W2", [authorship("Jane Doe"), authorship("Jane Doe")])
    twin_row = add(twin, seeds)
    blocked = pick(twin_row, WorkView.from_json(twin), 1, ctx(seeds))
    assert blocked.status == "blocked"

    with pytest.raises(ValueError):
        pick(row, WorkView.from_json(w), 9, ctx(seeds))


def test_remove_checks_every_authorship_the_target_holds(seeds):
    mixed = work(
        "W1", [authorship("J. Doe", TARGET), authorship("Jane Doe", TARGET, raw_orcid=MY_ORCID)]
    )
    assert remove(mixed, seeds).status == "protected"
    assert remove(mixed, seeds, origin="audit").status == "protected"
    half = work(
        "W2", [authorship("J. Roe", TARGET, raw_orcid=OTHER_ORCID), authorship("Jim Roe", TARGET)]
    )
    assert remove(half, seeds, origin="audit").status == "review"


def test_audit_never_auto_removes_on_a_raw_orcid_when_my_orcid_is_unknown(seeds):
    w = work("W1", [authorship("Jim Roe", TARGET, raw_orcid=OTHER_ORCID)])
    a = classify_remove(
        WorkView.from_json(w),
        Context(target_id=TARGET, my_orcid=None, seeds=seeds, owner_of=lambda _: None),
        origin="audit",
        basis="audit",
        refs=["audit:profile"],
    )
    assert (a.status, a.decision) == ("review", "skip")
