"""Curation payloads (byte-exact) and plan validation."""

import json

import pytest
from conftest import MY_ORCID

from oacurate.errors import PlanError
from oacurate.model import (
    CLAIM_PROPERTY_RE,
    Action,
    Plan,
    UnresolvedInput,
    load_plan,
    rekeyed,
    save_plan,
)

A = "https://openalex.org/A9"
W = "https://openalex.org/W1"


def claim(raw: str, **kw) -> Action:
    return Action(
        kind="claim_work",
        entity_id=W,
        value=A,
        raw_author_name=raw,
        status="claim",
        decision="apply",
        **kw,
    )


@pytest.mark.parametrize(
    ("raw", "prop"),
    [
        ('George "Vern" Yocum', 'authorships[raw_author_name="George "Vern" Yocum"].author.id'),
        ("Pat O'Brien", 'authorships[raw_author_name="Pat O\'Brien"].author.id'),
        ("Jörg Müller", 'authorships[raw_author_name="Jörg Müller"].author.id'),
    ],
)
def test_claim_property_is_byte_exact_and_survives_json_and_the_server_regex(raw, prop):
    payload = claim(raw).to_payload()
    assert payload["property"] == prop
    wire = json.dumps(payload)
    back = json.loads(wire)
    assert back == payload
    assert CLAIM_PROPERTY_RE.match(back["property"]).group(1) == raw


def test_every_payload_kind_is_exact():
    o = MY_ORCID
    cases = {
        ("remove_work", W, A): ["works", W, "authorships.author.id", "remove", A],
        ("set_orcid", A, o): ["authors", A, "orcid", "replace", o],
        ("remove_orcid", A, o): ["authors", A, "orcid", "remove", o],
        ("set_display_name", A, "Jane Doe"): ["authors", A, "display_name", "replace", "Jane Doe"],
        ("set_full_name", A, "J. Doe"): ["authors", A, "full_name", "replace", "J. Doe"],
    }
    for (kind, entity, value), expected in cases.items():
        payload = Action(kind=kind, entity_id=entity, value=value, status="claim").to_payload()
        assert list(payload) == ["entity", "entity_id", "property", "action", "value"]
        assert list(payload.values()) == expected


def test_action_id_is_stable_and_tracks_the_byline():
    a, b = claim("Jane Doe"), claim("Jane Doe", reasons=["x"])
    assert a.id == b.id and len(a.id) == 10
    assert rekeyed(a, raw_author_name="J. Doe").id != a.id


def plan_with(*actions: Action) -> Plan:
    return Plan(author_id=A, actions=list(actions))


def test_validate_accepts_a_good_plan():
    plan_with(claim("Jane Doe")).validate(A)


@pytest.mark.parametrize(
    ("plan", "message"),
    [
        (plan_with(claim("Jane Doe")), "configured author"),
        (
            plan_with(
                Action(
                    id="0000000000",
                    kind="claim_work",
                    entity_id=W,
                    value=A,
                    raw_author_name="x",
                    status="claim",
                )
            ),
            "ID does not match",
        ),
        (plan_with(claim("Jane\nDoe")), "property regex"),
        (plan_with(rekeyed(claim("Jane Doe"), status="review")), "not allowed for status"),
        (
            plan_with(
                Action(
                    kind="claim_work", entity_id="W1", value=A, raw_author_name="x", status="claim"
                )
            ),
            "long form",
        ),
        (
            plan_with(
                Action(kind="set_orcid", entity_id=A, value="0000-0002-1825-0097", status="claim")
            ),
            "canonical",
        ),
        (
            plan_with(
                Action(
                    kind="set_display_name",
                    entity_id="https://openalex.org/A8",
                    value="x",
                    status="claim",
                )
            ),
            "plan's author",
        ),
    ],
)
def test_validate_rejects(plan, message):
    expected = "https://openalex.org/A7" if message == "configured author" else A
    with pytest.raises(PlanError, match=message):
        plan.validate(expected)


def test_save_is_stable_and_load_validates(tmp_path):
    plan = Plan(
        author_id=A,
        actions=[
            claim("Zed Doe"),
            claim("Jane Doe"),
            rekeyed(claim("Al Doe"), status="review", decision="skip"),
        ],
        unresolved=[UnresolvedInput(ref="doi:10.1/x", reason="missing")],
        created_utc="2026-10-13T00:00:00Z",
    )
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    save_plan(plan, first)
    save_plan(Plan.from_dict(json.loads(first.read_text())), second)
    assert first.read_bytes() == second.read_bytes()
    loaded = load_plan(first, A)
    assert [a.status for a in loaded.actions] == ["claim", "claim", "review"]


def test_load_rejects_an_unknown_literal(tmp_path):
    path = tmp_path / "p.json"
    save_plan(plan_with(claim("Jane Doe")), path)
    data = json.loads(path.read_text())
    data["actions"][0]["status"] = "approved"
    path.write_text(json.dumps(data))
    with pytest.raises(PlanError):
        load_plan(path, A)


def test_unanchored_and_blocked_rows_survive_a_save_load_round_trip(tmp_path):
    no_byline = Action(
        kind="claim_work", entity_id=W, value=A, status="review", reasons=["no-byline"]
    )
    blocked = rekeyed(claim("Jane\nDoe"), status="blocked", decision="skip")
    path = tmp_path / "p.json"
    save_plan(plan_with(no_byline, blocked), path)
    assert {a.status for a in load_plan(path, A).actions} == {"review", "blocked"}
