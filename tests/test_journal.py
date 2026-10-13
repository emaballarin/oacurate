"""Journal persistence, pending reconciliation and revert plans."""

from datetime import UTC, datetime

import pytest
from conftest import MY_ORCID, OTHER_ORCID, TARGET

from oacurate.errors import UsageError
from oacurate.journal import (
    U7_CAVEAT,
    Journal,
    build_revert_plan,
    claims_in_window,
    reconcile,
    select_for_revert,
)
from oacurate.model import JournalEntry, claim_property

W = "https://openalex.org/W1"


def entry(
    kind, value, *, cid=None, outcome="created", ts="2026-10-13T10:00:00Z", **kw
) -> JournalEntry:
    entity = W if kind in ("claim_work", "remove_work") else TARGET
    prop = {
        "claim_work": claim_property(kw.get("raw_author_name") or "Jane Doe"),
        "remove_work": "authorships.author.id",
    }.get(kind, "x")
    return JournalEntry(
        ts_utc=ts,
        action_id="a" * 10,
        kind=kind,
        payload={
            "entity": "works",
            "entity_id": entity,
            "property": prop,
            "action": "replace",
            "value": value,
        },
        outcome=outcome,
        curation_id=cid,
        **kw,
    )


def test_append_and_read_round_trip_and_skip_a_torn_line(tmp_path):
    journal = Journal(tmp_path / "sub" / "A9.jsonl")
    e = entry("claim_work", TARGET, cid="cur-1", raw_author_name='G "V" Y')
    journal.append(e)
    with journal.path.open("a") as fh:
        fh.write('{"ts_utc": "2026-10-13T1')
    entries, warnings = journal.read()
    assert entries == [e]
    assert len(warnings) == 1 and ":2:" in warnings[0]
    assert journal.path.stat().st_mode & 0o077 == 0


def test_claims_in_window_counts_only_recent_successful_claims():
    now = datetime(2026, 10, 13, 12, tzinfo=UTC)
    entries = [
        entry("claim_work", TARGET, ts="2026-10-13T11:00:00Z"),
        entry("claim_work", TARGET, ts="2026-10-13T11:00:00Z", outcome="duplicate"),
        entry("claim_work", TARGET, ts="2026-10-13T11:00:00Z", outcome="rejected"),
        entry("claim_work", TARGET, ts="2026-10-12T11:00:00Z"),
        entry("remove_work", TARGET, ts="2026-10-13T11:00:00Z"),
    ]
    assert len(claims_in_window(entries, now)) == 2


def test_reconcile_states():
    entries = [
        entry("claim_work", TARGET, cid="cur-applied"),
        entry("claim_work", TARGET, cid="cur-pending"),
        entry("claim_work", TARGET, cid="cur-gone"),
        entry("claim_work", TARGET, outcome="unknown"),
        entry("claim_work", TARGET, outcome="rejected"),
    ]
    records = [
        {
            "id": "cur-applied",
            "is_applied": True,
            "created": "2026-10-13T10:00:00Z",
            "applied_at": "2026-10-14T02:00:00Z",
        },
        {"id": "cur-pending", "is_applied": False, "created": "2026-10-13T10:00:00Z"},
        {
            "id": "cur-elsewhere",
            "is_applied": False,
            "entity": "works",
            "property": "p",
            "action": "remove",
        },
    ]
    states = {(r.state, r.curation_id) for r in reconcile(entries, records)}
    assert states == {
        ("applied", "cur-applied"),
        ("pending", "cur-pending"),
        ("journal-only", "cur-gone"),
        ("unknown-outcome", None),
        ("server-only", "cur-elsewhere"),
    }
    unverified = reconcile(entries, [], server_known=False)
    assert {r.state for r in unverified} == {"unverified", "unknown-outcome"}


def test_revert_inverts_each_kind():
    entries = [
        entry("claim_work", TARGET, cid="cur-1", raw_author_name="Jane Doe"),
        entry("remove_work", TARGET, cid="cur-2", raw_author_name="J. Doe"),
        entry("set_orcid", MY_ORCID, cid="cur-3", previous=OTHER_ORCID),
        entry("set_display_name", "Jane A. Doe", cid="cur-4", previous="Jane Doe"),
    ]
    plan = build_revert_plan(entries, TARGET)
    plan.validate(TARGET)
    got = sorted((a.kind, a.value, a.raw_author_name) for a in plan.actions)
    assert got == sorted(
        [
            ("remove_work", TARGET, "Jane Doe"),
            ("claim_work", TARGET, "J. Doe"),
            ("remove_orcid", MY_ORCID, None),
            ("set_orcid", OTHER_ORCID, None),
            ("set_display_name", "Jane Doe", None),
        ]
    )
    assert all(a.decision == "apply" for a in plan.actions)
    assert U7_CAVEAT in plan.warnings


def test_revert_of_an_orcid_removal_sets_it_again_and_chains_collapse():
    plan = build_revert_plan([entry("remove_orcid", MY_ORCID, cid="cur-1")], TARGET)
    assert [(a.kind, a.value) for a in plan.actions] == [("set_orcid", MY_ORCID)]
    chain = [
        entry("set_orcid", OTHER_ORCID, cid="cur-1", previous=None, ts="2026-10-13T10:00:00Z"),
        entry("set_orcid", MY_ORCID, cid="cur-2", previous=OTHER_ORCID, ts="2026-10-13T11:00:00Z"),
    ]
    plan = build_revert_plan(chain, TARGET)
    assert [(a.kind, a.value) for a in plan.actions] == [("remove_orcid", MY_ORCID)]
    assert U7_CAVEAT not in plan.warnings


def test_select_for_revert():
    entries = [
        entry("claim_work", TARGET, cid="cur-1", ts="2026-10-10T10:00:00Z"),
        entry("claim_work", TARGET, cid="cur-2", ts="2026-10-12T10:00:00Z"),
        entry("claim_work", TARGET, cid="cur-3", ts="2026-10-12T11:00:00Z", outcome="rejected"),
    ]
    since = datetime(2026, 10, 11, tzinfo=UTC)
    assert [e.curation_id for e in select_for_revert(entries, since=since, ids=[])] == ["cur-2"]
    assert [e.curation_id for e in select_for_revert(entries, since=None, ids=["cur-1"])] == [
        "cur-1"
    ]
    with pytest.raises(UsageError):
        select_for_revert(entries, since=None, ids=["cur-3"])
    with pytest.raises(UsageError):
        select_for_revert(entries, since=since, ids=["cur-1"])


def test_an_orcid_revert_removes_before_it_sets():
    plan = build_revert_plan(
        [entry("set_orcid", MY_ORCID, cid="cur-1", previous=OTHER_ORCID)], TARGET
    )
    assert [a.kind for a in plan.sorted().actions] == ["remove_orcid", "set_orcid"]


def test_fetch_curations_follows_the_recorded_paging_envelope():
    from conftest import load

    from oacurate.journal import fetch_curations

    envelope = load("live", "probe_U1_curations_plain")["body"]
    pages = {
        1: {
            "meta": {**envelope["meta"], "page": 1, "total_pages": 2},
            "results": [{"id": "cur-1"}],
        },
        2: {
            "meta": {**envelope["meta"], "page": 2, "total_pages": 2},
            "results": [{"id": "cur-2"}],
        },
    }
    calls = []

    def get(**params):
        calls.append(params)
        return pages[params["page"]]

    assert [r["id"] for r in fetch_curations(get)] == ["cur-1", "cur-2"]
    assert [c["page"] for c in calls] == [1, 2]
    assert fetch_curations(lambda **p: envelope) == []
    assert fetch_curations(lambda **p: {"unexpected": True}) is None


def test_a_recorded_get_curations_page_reconciles_with_its_journal_entries():
    from conftest import load

    from oacurate.journal import fetch_curations

    page = load("live", "get_curations_two_pending")["body"]
    records = fetch_curations(lambda **p: page)
    entries = [entry("claim_work", TARGET, cid=r["id"]) for r in records]
    assert {(r.state, r.curation_id) for r in reconcile(entries, records)} == {
        ("pending", "cur-hCE9CwDwyTFu"),
        ("pending", "cur-YNLF3QKkzQEV"),
    }


def test_rows_already_submitted_are_kept_but_not_selected():
    from oacurate.journal import mark_submitted
    from oacurate.model import Action

    def claim(raw):
        return Action(
            kind="claim_work",
            entity_id=W,
            value=TARGET,
            raw_author_name=raw,
            status="claim",
            decision="apply",
        )

    sent, other = claim("Jane Doe"), claim("J. Doe")
    entries = [
        JournalEntry(
            ts_utc="2026-10-13T10:00:00Z",
            action_id=sent.id,
            kind="claim_work",
            payload={},
            outcome="duplicate",
            curation_id="cur-1",
        ),
        JournalEntry(
            ts_utc="2026-10-13T10:00:00Z",
            action_id=other.id,
            kind="claim_work",
            payload={},
            outcome="rejected",
        ),
    ]
    marked = mark_submitted([sent, other], entries)
    assert [(a.id, a.status, a.decision) for a in marked] == [
        (sent.id, "claim", "skip"),
        (other.id, "claim", "apply"),
    ]
    assert marked[0].reasons == ["already-submitted:cur-1"]


def test_pending_rows_carry_the_server_check_error():
    entries = [entry("claim_work", TARGET, cid="cur-1")]
    records = [
        {
            "id": "cur-1",
            "is_applied": False,
            "last_check_error": "raw_author_name not found",
            "superseded_by": None,
        }
    ]
    [row] = reconcile(entries, records)
    assert (row.state, row.note) == ("pending", "raw_author_name not found")
