"""HTTP client: free path forms, corpus=all, key hygiene, retries, budget, response parsing."""

import logging

import httpx
import pytest
from conftest import SECRET, json_response, load

from oacurate.client import parse_post_response
from oacurate.errors import (
    ApiError,
    BudgetExceeded,
    BudgetExhausted,
    Forbidden,
    RateLimited,
    TransportError,
    Unauthorized,
)

SICI = "10.1002/(SICI)1097-0258(19980430)17:8<857::AID-SIM777>3.0.CO;2-E"


def ok_lists(request: httpx.Request) -> httpx.Response:
    """Answer single lookups with an empty object and lists with one empty page."""
    if request.url.path in ("/works", "/authors"):
        return json_response(
            200, {"meta": {"count": 7, "next_cursor": None}, "results": []}, credits=1
        )
    return json_response(200, {"id": "https://openalex.org/W1"})


def test_single_lookups_use_only_the_free_path_forms(make_client):
    client, rec = make_client(ok_lists)
    client.get_work("https://doi.org/10.7717/PeerJ.4375")
    client.get_work("doi:" + SICI)
    client.get_work("10.48550/arXiv.1706.03762")
    client.get_work("https://openalex.org/W2741809807")
    client.get_author("https://orcid.org/0000-0001-6187-6610")
    client.get_author("orcid:0000000161876610")  # same path: memoised, not re-sent
    client.get_author("a5023888391")
    raw_paths = [r.url.raw_path.decode() for r in rec.requests]
    assert raw_paths == [
        "/works/doi:10.7717/peerj.4375",
        "/works/doi:10.1002/%28sici%291097-0258%2819980430%2917:8%3C857::aid-sim777%3E3.0.co%3B2-e",
        "/works/doi:10.48550/arxiv.1706.03762",
        "/works/W2741809807",
        "/authors/orcid:0000-0001-6187-6610",
        "/authors/A5023888391",
    ]
    assert all("://" not in p for p in raw_paths)
    assert client.credits_used == 0


def test_every_works_list_request_carries_corpus_all(make_client):
    client, rec = make_client(ok_lists)
    client.count_works("author.id:A1")
    list(client.iter_works("author.id:A1", select=["id"]))
    client.search_works_by_title('"a title"', filter="type:!paratext", select=["id"])
    works = [r for r in rec.requests if r.url.path == "/works"]
    assert len(works) == 3
    assert all(r.url.params.get("corpus") == "all" for r in works)
    assert all("include_xpac" not in r.url.params for r in works)


def test_the_key_goes_in_the_header_and_never_in_urls_or_logs(make_client, caplog):
    client, rec = make_client(ok_lists)
    with caplog.at_level(logging.DEBUG):
        client.get_work("W1")
        client.count_works("author.id:A1")
    assert all(r.headers["Authorization"] == f"Bearer {SECRET}" for r in rec.requests)
    assert all(SECRET not in str(r.url) for r in rec.requests)
    assert caplog.records, "the HTTP log should have recorded the requests"
    assert all(SECRET not in r.getMessage() for r in caplog.records)


def test_a_budget_exhaustion_429_raises_without_retrying(make_client):
    body = load("lists", "429_budget.synthetic")
    client, rec = make_client(
        lambda r: json_response(429, body, credits=None, **{"X-RateLimit-Reset": "3600"})
    )
    with pytest.raises(BudgetExhausted) as info:
        client.count_works("author.id:A1")
    assert len(rec.requests) == 1 and rec.sleeps == []
    assert info.value.reset_at is not None


def test_a_rate_429_honours_retry_after_three_times(make_client):
    client, rec = make_client(
        lambda r: json_response(429, {"message": "too fast"}, credits=None, **{"Retry-After": "2"})
    )
    with pytest.raises(RateLimited):
        client.get_work("W1")
    assert len(rec.requests) == 4 and rec.sleeps == [2.0, 2.0, 2.0]


def test_5xx_backs_off_1_2_4_8_16_then_fails(make_client):
    client, rec = make_client(lambda r: httpx.Response(503, text="busy"))
    with pytest.raises(TransportError):
        client.get_work("W1")
    assert rec.sleeps == [1, 2, 4, 8, 16] and len(rec.requests) == 6


def test_5xx_recovers_when_the_server_does(make_client):
    answers = iter([httpx.Response(500), httpx.Response(502), json_response(200, {"id": "x"})])
    client, rec = make_client(lambda r: next(answers))
    assert client.get_work("W1") == {"id": "x"}
    assert rec.sleeps == [1, 2]


def test_network_errors_are_retried(make_client):
    calls = []

    def flaky(request):
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectError("down", request=request)
        return json_response(200, {"id": "x"})

    client, rec = make_client(flaky)
    assert client.get_work("W1") == {"id": "x"}
    assert rec.sleeps == [1, 2]


def test_the_budget_guard_refuses_before_sending(make_client):
    client, rec = make_client(ok_lists, max_credits=10)
    client.count_works("author.id:A1")  # 1 credit
    with pytest.raises(BudgetExceeded):
        client.count_works('raw_author_name.search:"jane doe"')  # would need 10 more
    assert len(rec.requests) == 1
    assert client.credits_used == 1


def test_single_lookups_are_free_memoised_and_none_on_404(make_client):
    client, rec = make_client(lambda r: httpx.Response(404, text="<html>"))
    assert client.get_work("doi:10.1234/missing") is None
    assert client.get_work("doi:10.1234/missing") is None
    assert len(rec.requests) == 1


@pytest.mark.parametrize(("status", "error"), [(401, Unauthorized), (403, Forbidden)])
def test_auth_errors_are_typed(make_client, status, error):
    client, _ = make_client(lambda r: json_response(status, {"code": "x", "message": "no"}))
    with pytest.raises(error):
        client.get_curations()


PAYLOAD = {
    "entity": "works",
    "entity_id": "https://openalex.org/W1",
    "property": 'authorships[raw_author_name="Jane Doe"].author.id',
    "action": "replace",
    "value": "https://openalex.org/A9",
}
RECORD = {"id": "cur-1", **PAYLOAD, "created": "2026-10-13T10:00:00Z", "is_applied": False}


@pytest.mark.parametrize(
    ("status", "body", "outcome"),
    [
        (201, RECORD, "created"),
        (200, RECORD, "duplicate"),
        (400, {"message": "bad ORCID"}, "rejected"),
        (201, {"id": "cur-1", **PAYLOAD, "value": "https://openalex.org/A8"}, "unknown"),
        (201, {"ok": True}, "unknown"),
        (201, {"id": "cur-1"}, "unknown"),
        (200, {"id": "cur-1", "value": PAYLOAD["value"]}, "unknown"),
    ],
)
def test_single_post_outcomes(status, body, outcome):
    [result] = parse_post_response(status, body, [PAYLOAD], single=True)
    assert result.outcome == outcome


def test_a_mixed_207_is_parsed_item_by_item_and_unknown_shapes_are_never_success():
    body = load("curations", "207_mixed.synthetic")
    payloads = [
        {**PAYLOAD, "entity_id": "https://openalex.org/W1"},
        {**PAYLOAD, "entity_id": "https://openalex.org/W2"},
        {**PAYLOAD, "entity_id": "https://openalex.org/W3"},
        {**PAYLOAD, "entity_id": "https://openalex.org/W4"},
    ]
    results = parse_post_response(207, body, payloads, single=False)
    assert [r.outcome for r in results] == ["created", "duplicate", "rejected", "unknown"]
    assert results[0].curation_id == "cur-0001"
    assert results[2].message == "raw_author_name not found on work"


@pytest.mark.parametrize("body", [{"results": []}, {"something": "else"}, None, [RECORD]])
def test_an_unrecognised_207_marks_every_item_unknown(body):
    results = parse_post_response(207, body, [PAYLOAD, PAYLOAD], single=False)
    assert [r.outcome for r in results] == ["unknown", "unknown"]


def test_an_array_post_failing_at_http_level_raises():
    with pytest.raises(ApiError):
        parse_post_response(413, {"message": "too large"}, [PAYLOAD], single=False)


def test_a_long_retry_after_is_not_slept_through(make_client):
    client, rec = make_client(
        lambda r: json_response(
            429, {"message": "slow down"}, credits=None, **{"Retry-After": "86400"}
        )
    )
    with pytest.raises(RateLimited, match="86400"):
        client.post_curations({**PAYLOAD})
    assert rec.sleeps == [] and len(rec.requests) == 1


def test_a_429_without_retry_after_waits_the_body_retry_after(make_client):
    answers = iter(
        [json_response(429, {"retryAfter": 7}, credits=None), json_response(200, {"id": "x"})]
    )
    client, rec = make_client(lambda r: next(answers))
    assert client.get_work("W1") == {"id": "x"}
    assert rec.sleeps == [7.0]
