"""Apply: dry run, preflight, canary, chunking, 207 outcomes, daily cap, stale rows."""

import itertools
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import TARGET, FakeApi, author, authorship, json_response, work

from oacurate.apply import ApplyOptions, apply_plan, select_actions
from oacurate.errors import EXIT_OK, EXIT_PARTIAL, Forbidden, Unauthorized, UsageError
from oacurate.journal import Journal
from oacurate.model import Action, JournalEntry, Plan

NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)


def claim(wid: str, raw: str = "Jane Doe", owner: str | None = None, **kw) -> Action:
    return Action(
        kind="claim_work",
        entity_id=f"https://openalex.org/{wid}",
        value=TARGET,
        raw_author_name=raw,
        status=kw.pop("status", "claim"),
        decision=kw.pop("decision", "apply"),
        evidence={"owner": owner, "work": wid},
        **kw,
    )


def removal(wid: str) -> Action:
    return Action(
        kind="remove_work",
        entity_id=f"https://openalex.org/{wid}",
        value=TARGET,
        raw_author_name="Jane Doe",
        status="remove",
        decision="apply",
    )


def api_with(*wids: str, on_profile: tuple[str, ...] = ()) -> FakeApi:
    works = [work(w, [authorship("Bob Roe"), authorship("Jane Doe")]) for w in wids]
    works += [work(w, [authorship("Jane Doe", TARGET)]) for w in on_profile]
    return FakeApi(works, [author("A9", "Jane Doe")])


def echo_posts(single_status: int = 201, item_status: int = 201, ids=None):
    """A POST handler echoing each payload back as a curation row."""
    ids = ids or (f"cur-{i}" for i in itertools.count(1))

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if isinstance(body, dict):
            return json_response(single_status, {"id": next(ids), **body}, credits=None)
        items = [{"status": item_status, "data": {"id": next(ids), **p}} for p in body]
        return json_response(207, {"results": items}, credits=None)

    return handler


def posts(rec) -> list[httpx.Request]:
    return [r for r in rec.requests if r.method == "POST"]


def run(client, plan, tmp_path, **opts):
    journal = Journal(tmp_path / "j.jsonl")
    report = apply_plan(client, plan, journal, ApplyOptions(yes=True, **opts), now=lambda: NOW)
    return report, journal


def test_dry_run_sends_nothing(make_client, tmp_path):
    client, rec = make_client(lambda r: pytest.fail(f"request sent during a dry run: {r.url}"))
    plan = Plan(author_id=TARGET, actions=[claim("W1"), removal("W2")])
    report = apply_plan(client, plan, Journal(tmp_path / "j.jsonl"), ApplyOptions())
    assert report.dry_run and [a.id for a in report.selected] == [a.id for a in plan.actions]
    assert rec.requests == [] and report.exit_code == EXIT_OK
    assert not (tmp_path / "j.jsonl").exists()


def test_preflight_401_stops_before_any_post(make_client, tmp_path):
    api = api_with("W1")
    client, rec = make_client(
        lambda r: (
            json_response(401, {"code": "unauthorized"})
            if r.method == "GET" and r.url.host == "user.openalex.org"
            else api(r)
        )
    )
    with pytest.raises(Unauthorized, match="API key"):
        run(client, Plan(author_id=TARGET, actions=[claim("W1")]), tmp_path)
    assert posts(rec) == []


def test_a_canary_403_aborts_everything(make_client, tmp_path):
    api = api_with("W1", "W2", "W3")
    api.post_handler = lambda r: json_response(403, {"message": "forbidden"}, credits=None)
    client, rec = make_client(api)
    plan = Plan(author_id=TARGET, actions=[claim("W1"), claim("W2"), claim("W3")])
    with pytest.raises(Forbidden, match="not claimed"):
        run(client, plan, tmp_path)
    assert len(posts(rec)) == 1
    assert isinstance(json.loads(posts(rec)[0].content), dict)
    assert Journal(tmp_path / "j.jsonl").read() == ([], [])


def test_a_mixed_207_is_journalled_item_by_item_and_exits_2(make_client, tmp_path):
    api = api_with("W1", "W2", "W3")

    def handler(request):
        body = json.loads(request.content)
        if isinstance(body, dict):
            return json_response(201, {"id": "cur-1", **body}, credits=None)
        return json_response(
            207,
            {
                "results": [
                    {"status": 201, "data": {"id": "cur-2", **body[0]}},
                    {"status": 400, "message": "nope"},
                ]
            },
            credits=None,
        )

    api.post_handler = handler
    client, rec = make_client(api)
    report, journal = run(
        client, Plan(author_id=TARGET, actions=[claim("W1"), claim("W2"), claim("W3")]), tmp_path
    )
    assert report.counts() == {"created": 2, "duplicate": 0, "rejected": 1, "unknown": 0}
    assert report.exit_code == EXIT_PARTIAL
    entries, _ = journal.read()
    assert [e.outcome for e in entries] == ["created", "created", "rejected"]
    assert [
        len(json.loads(p.content)) if isinstance(json.loads(p.content), list) else 1
        for p in posts(rec)
    ] == [1, 2]


def test_everything_created_exits_0_and_chunks_after_the_canary(make_client, tmp_path):
    wids = [f"W{i}" for i in range(1, 8)]
    api = api_with(*wids)
    api.post_handler = echo_posts()
    client, rec = make_client(api)
    report, _ = run(
        client, Plan(author_id=TARGET, actions=[claim(w) for w in wids]), tmp_path, chunk=4
    )
    sizes = [len(b) if isinstance(b := json.loads(p.content), list) else 1 for p in posts(rec)]
    assert sizes == [1, 4, 2] and report.exit_code == EXIT_OK


def test_the_rolling_24h_cap_holds_and_removals_are_uncapped(make_client, tmp_path):
    journal = Journal(tmp_path / "j.jsonl")
    oldest = NOW - timedelta(hours=23)
    for i in range(999):
        ts = (oldest + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        journal.append(
            JournalEntry(
                ts_utc=ts,
                action_id="x",
                kind="claim_work",
                payload={},
                outcome="created",
                curation_id=f"old-{i}",
            )
        )
    api = api_with("W1", "W2", "W3", on_profile=("W4",))
    api.post_handler = echo_posts()
    client, _ = make_client(api)
    plan = Plan(author_id=TARGET, actions=[claim("W1"), claim("W2"), claim("W3"), removal("W4")])
    report = apply_plan(client, plan, journal, ApplyOptions(yes=True), now=lambda: NOW)
    assert len(report.results) == 2 and len(report.leftover) == 2
    assert {a.kind for a, _ in report.results} == {"claim_work", "remove_work"}
    assert report.window_frees_at == oldest + timedelta(hours=24)
    assert report.exit_code == EXIT_PARTIAL


def test_stale_rows_are_skipped(make_client, tmp_path):
    api = FakeApi(
        [
            work("W1", [authorship("Someone Else")]),
            work("W2", [authorship("Jane Doe", "https://openalex.org/A8")]),
            work("W3", [authorship("Jane Doe", TARGET)]),
            work("W4", [authorship("Jane Doe")]),
            work("W5", [authorship("Jane Doe")]),
        ]
    )
    client, rec = make_client(api)
    plan = Plan(
        author_id=TARGET,
        actions=[claim("W1"), claim("W2", owner=None), claim("W3"), removal("W4"), claim("W6")],
    )
    report, _ = run(client, plan, tmp_path)
    reasons = {a.evidence.get("work") or a.entity_id[-2:]: why for a, why in report.stale}
    assert reasons == {
        "W1": "the byline is no longer on the work",
        "W2": "the byline's owner changed from None to A8",
        "W3": "already on the profile",
        "W4": "not on the profile (if a claim is still pending, retry later)",
        "W6": "the work no longer resolves",
    }
    assert posts(rec) == [] and report.results == []


def test_a_known_curation_id_is_a_duplicate(make_client, tmp_path):
    journal = Journal(tmp_path / "j.jsonl")
    journal.append(
        JournalEntry(
            ts_utc="2026-10-01T00:00:00Z",
            action_id="x",
            kind="claim_work",
            payload={},
            outcome="created",
            curation_id="cur-1",
        )
    )
    api = api_with("W1")
    api.post_handler = echo_posts()
    client, _ = make_client(api)
    report = apply_plan(
        client,
        Plan(author_id=TARGET, actions=[claim("W1")]),
        journal,
        ApplyOptions(yes=True),
        now=lambda: NOW,
    )
    assert [o.outcome for _, o in report.results] == ["duplicate"] and report.exit_code == EXIT_OK


def test_a_chunk_failing_twice_is_unknown_and_stops_the_run(make_client, tmp_path):
    api = api_with("W1", "W2", "W3", "W4")
    ok = echo_posts()

    def handler(request):
        body = json.loads(request.content)
        return (
            ok(request)
            if isinstance(body, dict)
            else json_response(413, {"message": "too big"}, credits=None)
        )

    api.post_handler = handler
    client, rec = make_client(api)
    report, journal = run(
        client,
        Plan(author_id=TARGET, actions=[claim(w) for w in ("W1", "W2", "W3", "W4")]),
        tmp_path,
        chunk=2,
    )
    assert [o.outcome for _, o in report.results] == ["created", "unknown", "unknown"]
    assert report.stopped and report.exit_code == EXIT_PARTIAL
    assert len(posts(rec)) == 3  # canary, chunk, one retry; the last chunk is never sent
    assert len(journal.read()[0]) == 3
    assert [a.evidence["work"] for a in report.unsent] == ["W4"]


def test_accept_and_reject():
    review = claim("W2", status="review", decision="skip")
    unanchored = Action(
        kind="claim_work", entity_id="https://openalex.org/W3", value=TARGET, status="review"
    )
    plan = Plan(author_id=TARGET, actions=[claim("W1"), review, unanchored])
    assert [a.id for a in select_actions(plan, ["W2"], ["W1"])] == [review.id]
    assert [a.id for a in select_actions(plan, [review.id], [])] == [plan.actions[0].id, review.id]
    with pytest.raises(UsageError, match="plan pick"):
        select_actions(plan, ["W3"], [])
    with pytest.raises(UsageError, match="no such"):
        select_actions(plan, [], ["W99"])


def test_a_mid_run_rate_limit_lists_everything_left_unsent(make_client, tmp_path):
    wids = [f"W{i}" for i in range(1, 6)]
    api = api_with(*wids)
    ok = echo_posts()

    def handler(request):
        if isinstance(json.loads(request.content), dict):
            return ok(request)
        return json_response(429, {"message": "slow"}, credits=None, **{"Retry-After": "3600"})

    api.post_handler = handler
    client, _ = make_client(api)
    report, journal = run(
        client, Plan(author_id=TARGET, actions=[claim(w) for w in wids]), tmp_path, chunk=2
    )
    assert len(report.results) == 1
    assert [a.evidence["work"] for a in report.unsent] == ["W2", "W3", "W4", "W5"]
    assert report.exit_code == 4
    assert len(journal.read()[0]) == 1


def test_a_merged_work_is_stale(make_client, tmp_path):
    api = api_with("W1")
    api.works["W1"] = {**api.works["W1"], "id": "https://openalex.org/W100"}
    client, rec = make_client(api)
    report, _ = run(client, Plan(author_id=TARGET, actions=[claim("W1")]), tmp_path)
    assert [why for _, why in report.stale] == ["the work was merged into W100; plan it again"]
    assert posts(rec) == []


def test_preflight_inconclusive_4xx_is_a_note_not_an_abort(make_client, tmp_path):
    api = api_with("W1")
    api.post_handler = echo_posts()

    def handler(request):
        if request.method == "GET" and request.url.host == "user.openalex.org":
            return json_response(404, {"message": "no such route"}, credits=None)
        return api(request)

    client, _ = make_client(handler)
    report, _ = run(client, Plan(author_id=TARGET, actions=[claim("W1")]), tmp_path)
    assert report.exit_code == EXIT_OK and "404" in report.notes[0]


def test_accept_refuses_an_unanchorable_byline():
    bad = claim("W2", raw="Jane\nDoe", status="review", decision="skip")
    with pytest.raises(UsageError, match="plan pick"):
        select_actions(Plan(author_id=TARGET, actions=[bad]), ["W2"], [])


def test_a_mid_run_auth_failure_exits_3_and_lists_the_unsent(make_client, tmp_path):
    wids = ["W1", "W2", "W3"]
    api = api_with(*wids)
    ok = echo_posts()

    def handler(request):
        if isinstance(json.loads(request.content), dict):
            return ok(request)
        return json_response(403, {"message": "forbidden"}, credits=None)

    api.post_handler = handler
    client, _ = make_client(api)
    report, _ = run(client, Plan(author_id=TARGET, actions=[claim(w) for w in wids]), tmp_path)
    assert report.exit_code == 3 and "authentication failed mid-run" in report.stopped
    assert [a.evidence["work"] for a in report.unsent] == ["W2", "W3"]


def test_author_actions_refresh_previous_and_an_orcid_revert_removes_first(make_client, tmp_path):
    from conftest import MY_ORCID, OTHER_ORCID

    api = FakeApi([], [author("A9", "Jane Doe", orcid=MY_ORCID)])
    api.post_handler = echo_posts()
    client, rec = make_client(api)
    actions = [
        Action(
            kind="set_orcid", entity_id=TARGET, value=OTHER_ORCID, status="claim", decision="apply"
        ),
        Action(
            kind="remove_orcid", entity_id=TARGET, value=MY_ORCID, status="remove", decision="apply"
        ),
    ]
    report, journal = run(client, Plan(author_id=TARGET, actions=actions).sorted(), tmp_path)
    sent = [json.loads(p.content) for p in posts(rec)]
    assert [b["action"] if isinstance(b, dict) else [x["action"] for x in b] for b in sent] == [
        "remove",
        ["replace"],
    ]
    assert [e.previous for e in journal.read()[0]] == [MY_ORCID, MY_ORCID]
    assert report.exit_code == EXIT_OK


def test_a_preflight_403_is_inconclusive_and_the_canary_decides(make_client, tmp_path):
    api = api_with("W1")
    api.post_handler = echo_posts()

    def handler(request):
        if request.method == "GET" and request.url.host == "user.openalex.org":
            return json_response(403, {"message": "listing not allowed"}, credits=None)
        return api(request)

    client, _ = make_client(handler)
    report, _ = run(client, Plan(author_id=TARGET, actions=[claim("W1")]), tmp_path)
    assert report.exit_code == EXIT_OK and "canary" in report.notes[0]
