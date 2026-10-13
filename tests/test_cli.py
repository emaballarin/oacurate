"""Command line: parsing contract, key handling, exit codes, and dry runs end to end."""

import json
import logging

import httpx
import pytest
from conftest import MY_ORCID, FakeApi, author, authorship, json_response, work

import oacurate.cli as cli
from oacurate.client import OpenAlexClient
from oacurate.errors import UsageError


@pytest.fixture(autouse=True)
def restore_logging():
    """``main()`` configures process-wide loggers; undo that after each test."""
    saved = {
        name: (lg.handlers[:], lg.propagate, lg.level)
        for name in ("oacurate", "httpx", "bibtexparser")
        for lg in [logging.getLogger(name)]
    }
    yield
    for name, (handlers, propagate, level) in saved.items():
        lg = logging.getLogger(name)
        lg.handlers, lg.propagate = handlers, propagate
        lg.setLevel(level)


def test_common_options_work_after_the_leaf_subcommand():
    args = cli.parse(
        [
            "plan",
            "add",
            "--doi",
            "10.1000/a",
            "10.1000/b",
            "--author",
            "A9",
            "-o",
            "p.json",
            "--max_credits",
            "5",
            "--format",
            "tsv",
        ]
    )
    assert isinstance(args, cli.PlanAdd)
    assert (args.doi, args.author, args.output, args.max_credits, args.format) == (
        ["10.1000/a", "10.1000/b"],
        "A9",
        "p.json",
        5,
        "tsv",
    )
    nested = cli.parse(["orcid", "set", "0000-0002-1825-0097", "--author", "A9", "--yes"])
    assert isinstance(nested, cli.OrcidSet) and nested.yes and nested.orcid == "0000-0002-1825-0097"


@pytest.mark.parametrize(
    "argv",
    [
        ["show", "--api_key", "secret"],
        ["show", "--api_key=secret"],
        ["plan", "add", "--api", "secret"],
        ["apply", "p.json", "--api_key", "secret"],
    ],
)
def test_there_is_no_api_key_option_and_no_abbreviation(argv, capsys):
    with pytest.raises(SystemExit) as info:
        cli.parse(argv)
    assert info.value.code == 1
    assert "unrecognized arguments" in capsys.readouterr().err


def test_a_repeated_list_option_is_refused_instead_of_silently_dropping_values():
    with pytest.raises(UsageError, match="--doi given 2 times"):
        cli.parse(["plan", "add", "--doi", "10.1000/a", "--doi", "10.1000/b"])
    with pytest.raises(UsageError, match="--name"):
        cli.parse(["audit", "--name=A B", "--name", "C D"])


def test_dashed_option_names_are_not_available():
    with pytest.raises(SystemExit):
        cli.parse(["plan", "add", "--no-search"])
    assert cli.parse(["plan", "add", "--no_search"]).no_search


# ---------------------------------------------------------------- end to end, mocked HTTP


ME = author("A9", "Jane Doe", works_count=3, raw_author_names=["Jane Doe", "J. Doe"])
PAPER = work(
    "W1", [authorship("Bob Roe"), authorship("Jane Doe")], doi="https://doi.org/10.1000/paper"
)


@pytest.fixture
def api(monkeypatch, tmp_path):
    fake = FakeApi([PAPER], [ME])
    sent: list[httpx.Request] = []

    def transport(request):
        sent.append(request)
        return fake(request)

    def client_factory(key, **kw):
        return OpenAlexClient(
            key, transport=httpx.MockTransport(transport), sleep=lambda s: None, **kw
        )

    monkeypatch.setattr(cli, "OpenAlexClient", client_factory)
    monkeypatch.setenv("OPENALEX_API_KEY", "k")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("OACURATE_AUTHOR", "A9")
    fake.sent = sent
    return fake


def test_show_uses_no_credits(api, capsys):
    assert cli.main(["show", "--format", "json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["display_name"] == "Jane Doe" and shown["credits_used"] == 0


def test_orcid_set_is_a_dry_run_that_prints_the_exact_payload(api, capsys):
    assert cli.main(["orcid", "set", "0000-0002-1825-0097"]) == 0
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("[") :])
    assert payload == [
        {
            "entity": "authors",
            "entity_id": "https://openalex.org/A9",
            "property": "orcid",
            "action": "replace",
            "value": MY_ORCID,
        }
    ]
    assert all(r.method == "GET" and r.url.host == "api.openalex.org" for r in api.sent)


def test_a_bad_orcid_exits_1_before_any_request(api, capsys):
    assert cli.main(["orcid", "set", "0000-0002-1825-0098"]) == 1
    assert "check character" in capsys.readouterr().err
    assert api.sent == []


def test_plan_add_then_apply_for_real(api, tmp_path, capsys):
    plan = tmp_path / "plan.json"
    assert (
        cli.main(["plan", "add", "--doi", "10.1000/paper", "10.1234/missing", "-o", str(plan)]) == 0
    )
    saved = json.loads(plan.read_text())
    assert [(a["status"], a["raw_author_name"]) for a in saved["actions"]] == [
        ("claim", "Jane Doe")
    ]
    assert saved["credits_used"] == 0

    assert cli.main(["plan", "show", str(plan), "--status", "missing"]) == 0
    assert "Work missing from OpenAlex" in capsys.readouterr().out

    api.post_handler = lambda r: json_response(
        201, {"id": "cur-9", **json.loads(r.content)}, credits=None
    )
    assert cli.main(["apply", str(plan), "--yes"]) == 0
    journal = tmp_path / "xdg" / "oacurate" / "A9.jsonl"
    [line] = journal.read_text().splitlines()
    assert json.loads(line)["curation_id"] == "cur-9"


def test_apply_refuses_a_plan_for_another_author(api, tmp_path, capsys):
    plan = tmp_path / "plan.json"
    assert cli.main(["plan", "add", "--doi", "10.1000/paper", "-o", str(plan)]) == 0
    assert cli.main(["apply", str(plan), "--author", "A8"]) == 1
    assert "configured author" in capsys.readouterr().err


def test_title_search_over_budget_exits_4_before_searching(api, tmp_path):
    bib = tmp_path / "x.bib"
    bib.write_text("@article{k, title={Only a title}, author={Doe, Jane}, year={2020}}\n")
    code = cli.main(
        ["plan", "add", "--bib", str(bib), "--max_credits", "5", "-o", str(tmp_path / "p.json")]
    )
    assert code == 4
    assert all(r.url.path != "/works" for r in api.sent)


def test_unauthorised_exits_3(api, monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "OpenAlexClient",
        lambda key, **kw: OpenAlexClient(
            key,
            transport=httpx.MockTransport(
                lambda r: json_response(401, {"message": "API key not found"})
            ),
            **kw,
        ),
    )
    assert cli.main(["show"]) == 3
    assert "OPENALEX_API_KEY" in capsys.readouterr().err


def test_a_world_readable_key_file_warns(api, tmp_path, monkeypatch, capsys):
    key = tmp_path / "key"
    key.write_text("k\n")
    key.chmod(0o644)
    monkeypatch.delenv("OPENALEX_API_KEY")
    assert cli.main(["show", "--api_key_file", str(key)]) == 0
    assert "chmod 600" in capsys.readouterr().err


def test_a_repeated_scalar_option_is_refused_too():
    with pytest.raises(UsageError, match="--dois_file given 2 times"):
        cli.parse(["plan", "add", "--dois_file", "a.txt", "--dois_file", "b.txt"])


ON_PROFILE = work(
    "W5", [authorship("Jane Doe", "https://openalex.org/A9")], doi="https://doi.org/10.1000/mine"
)
TWINS = work("W6", [authorship("J. Doe"), authorship("J Doe")], doi="https://doi.org/10.1000/twins")


def add_work(api, w):
    api.works[w["id"].rsplit("/", 1)[1]] = w
    api.by_doi[w["doi"].removeprefix("https://doi.org/")] = w


def test_plan_remove_and_pick(api, tmp_path, capsys):
    add_work(api, ON_PROFILE)
    add_work(api, TWINS)
    plan = tmp_path / "rm.json"
    assert cli.main(["plan", "remove", "--work", "W5", "-o", str(plan)]) == 0
    assert [a["status"] for a in json.loads(plan.read_text())["actions"]] == ["remove"]

    plan = tmp_path / "add.json"
    assert cli.main(["plan", "add", "--doi", "10.1000/twins", "-o", str(plan)]) == 0
    [row] = json.loads(plan.read_text())["actions"]
    assert (row["status"], row["reasons"]) == ("review", ["ambiguous"])
    assert cli.main(["plan", "pick", str(plan), row["id"], "--position", "2"]) == 0
    [picked] = json.loads(plan.read_text())["actions"]
    assert (picked["status"], picked["raw_author_name"]) == ("claim", "J Doe")


def dry_payload(capsys):
    out = capsys.readouterr().out
    return json.loads(out[out.index("[") :])


def test_name_and_orcid_dry_runs(api, capsys):
    assert cli.main(["name", "match", "J. Doe"]) == 0
    assert dry_payload(capsys)[0]["property"] == "full_name"
    assert cli.main(["name", "match", "Jay Doe"]) == 1
    assert cli.main(["name", "display", "Jane A. Doe"]) == 0
    assert dry_payload(capsys)[0]["value"] == "Jane A. Doe"
    assert cli.main(["orcid", "remove", "0000-0002-1825-0097"]) == 0
    assert dry_payload(capsys)[0]["action"] == "remove"
    assert not any(r.method == "POST" for r in api.sent)


def test_orcid_set_refuses_to_replace_without_force(api, capsys):
    api.authors["A9"] = {**ME, "orcid": "https://orcid.org/0000-0002-1694-233X"}
    assert cli.main(["orcid", "set", "0000-0002-1825-0097"]) == 1
    assert "--force" in capsys.readouterr().err
    assert cli.main(["orcid", "set", "0000-0002-1825-0097", "--force"]) == 0


def test_apply_then_pending_then_revert(api, tmp_path, capsys):
    plan, journal = tmp_path / "plan.json", tmp_path / "custom.jsonl"
    assert cli.main(["plan", "add", "--doi", "10.1000/paper", "-o", str(plan)]) == 0
    api.post_handler = lambda r: json_response(
        201, {"id": "cur-9", **json.loads(r.content)}, credits=None
    )
    assert cli.main(["apply", str(plan), "--yes", "--journal", str(journal)]) == 0
    assert journal.exists()

    api.curations = {
        "results": [{"id": "cur-9", "is_applied": False, "created": "2026-10-13T10:00:00Z"}]
    }
    capsys.readouterr()
    assert cli.main(["pending", "--journal", str(journal), "--format", "tsv"]) == 0
    rows = capsys.readouterr().out.splitlines()
    assert rows[1].startswith("pending\tcur-9\tclaim_work\tW1")

    undo = tmp_path / "undo.json"
    assert cli.main(["revert", "--ids", "cur-9", "--journal", str(journal), "-o", str(undo)]) == 0
    saved = json.loads(undo.read_text())
    assert [(a["kind"], a["raw_author_name"]) for a in saved["actions"]] == [
        ("remove_work", "Jane Doe")
    ]
    assert any("U7" in w for w in saved["warnings"])


def test_audit_through_the_cli(api, tmp_path, capsys):
    api.list_handler = lambda r: json_response(
        200, {"meta": {"count": 0, "next_cursor": None}, "results": []}, credits=1
    )
    plan = tmp_path / "audit.json"
    assert cli.main(["audit", "--max_step", "1", "-o", str(plan)]) == 0
    err = capsys.readouterr().err
    assert "estimated cost" in err and "used 3 credits" in err
    assert json.loads(plan.read_text())["actions"] == []


def test_verbose_logs_requests_with_the_key_redacted(api, capsys):
    assert cli.main(["show", "--verbose"]) == 0
    err = capsys.readouterr().err
    assert "oacurate.http: GET https://api.openalex.org/authors/A9" in err
    assert "<redacted>" in err and "Bearer" not in err


def test_a_json_dry_run_is_valid_json(api, tmp_path, capsys):
    plan = tmp_path / "plan.json"
    assert cli.main(["plan", "add", "--doi", "10.1000/paper", "-o", str(plan)]) == 0
    capsys.readouterr()
    assert cli.main(["apply", str(plan), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["action"] == "replace"


def test_revert_reports_unknown_entries_even_when_nothing_succeeded(api, tmp_path, capsys):
    journal = tmp_path / "j.jsonl"
    entry = {
        "ts_utc": "2026-10-13T10:00:00Z",
        "action_id": "abcdef0123",
        "kind": "claim_work",
        "payload": {"entity_id": "https://openalex.org/W1", "value": "https://openalex.org/A9"},
        "outcome": "unknown",
    }
    journal.write_text(json.dumps(entry) + "\n")
    code = cli.main(
        [
            "revert",
            "--since",
            "12/10/2026",
            "--journal",
            str(journal),
            "-o",
            str(tmp_path / "u.json"),
        ]
    )
    assert code == 1
    assert "1 journal entries in range have an unknown outcome" in capsys.readouterr().err


def test_the_report_lists_unsent_items_and_a_cap_without_a_window(capsys):
    from oacurate.apply import ApplyReport
    from oacurate.model import Action

    a = Action(
        kind="claim_work",
        entity_id="https://openalex.org/W1",
        value="https://openalex.org/A9",
        raw_author_name="x",
        status="claim",
    )
    report = ApplyReport(
        selected=[a], dry_run=False, leftover=[a], unsent=[a], stopped="rate or budget limit"
    )
    assert cli.emit_report(report, "table", 0) == 2
    err = capsys.readouterr().err
    assert "run apply again tomorrow" in err
    assert f"not sent because the run stopped (1): {a.id} (W1)" in err
    assert "stopped: rate or budget limit" in err


def test_short_and_long_spellings_of_one_option_count_as_a_repeat():
    with pytest.raises(UsageError, match="--output given 2 times"):
        cli.parse(["plan", "add", "-o", "x.json", "--output", "y.json"])


def test_a_json_apply_report_is_one_json_document(api, tmp_path, capsys):
    plan = tmp_path / "plan.json"
    assert cli.main(["plan", "add", "--doi", "10.1000/paper", "-o", str(plan)]) == 0
    api.post_handler = lambda r: json_response(
        201, {"id": "cur-9", **json.loads(r.content)}, credits=None
    )
    capsys.readouterr()
    assert cli.main(["apply", str(plan), "--yes", "--format", "json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["counts"]["created"] == 1 and doc["results"][0]["curation"] == "cur-9"
    assert (doc["unsent"], doc["leftover"], doc["exit_code"]) == ([], [], 0)


def test_a_second_plan_does_not_reselect_what_was_already_submitted(api, tmp_path, capsys):
    plan = tmp_path / "plan.json"
    assert cli.main(["plan", "add", "--doi", "10.1000/paper", "-o", str(plan)]) == 0
    api.post_handler = lambda r: json_response(
        201, {"id": "cur-9", **json.loads(r.content)}, credits=None
    )
    assert cli.main(["apply", str(plan), "--yes"]) == 0
    again = tmp_path / "again.json"
    assert cli.main(["plan", "add", "--doi", "10.1000/paper", "-o", str(again)]) == 0
    [row] = json.loads(again.read_text())["actions"]
    assert (row["status"], row["decision"], row["reasons"]) == (
        "claim",
        "skip",
        ["already-submitted:cur-9"],
    )
    capsys.readouterr()
    assert cli.main(["apply", str(again), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
