"""Command line: ``simple_parsing`` dataclasses mapped onto library functions."""

import json
import logging
import os
import stat
import sys
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from dataclasses import field as dfield
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NoReturn

from simple_parsing import ArgumentParser, field, subparsers

from oacurate import __version__
from oacurate.apply import ApplyOptions, ApplyReport, apply_plan, select_actions
from oacurate.audit import AuditOptions, run_audit
from oacurate.bib import InputRecord, read_bib, read_doi_list, records_from_dois, records_from_works
from oacurate.classify import Context, pick
from oacurate.client import CreditBudget, OpenAlexClient
from oacurate.errors import (
    EXIT_OK,
    EXIT_USAGE,
    AuthError,
    BudgetError,
    BudgetExceeded,
    InvalidIdentifier,
    OacurateError,
    UsageError,
)
from oacurate.ids import normalise_openalex_id, normalise_orcid, orcid_bare, short_id
from oacurate.journal import (
    Journal,
    build_revert_plan,
    claims_in_window,
    default_journal_path,
    fetch_curations,
    now_utc,
    reconcile,
    select_for_revert,
    unknown_in_range,
)
from oacurate.model import (
    STATUSES,
    WORK_KINDS,
    Action,
    AuthorView,
    Plan,
    UnresolvedInput,
    WorkView,
    load_plan,
    parse_iso_utc,
    save_plan,
)
from oacurate.names import Seeds, build_seeds
from oacurate.render import (
    Column,
    Format,
    format_age,
    format_local,
    payloads_json,
    plan_row,
    render_mapping,
    render_plan,
    render_rows,
    render_unresolved,
    status_counts,
    ticket_text,
)
from oacurate.resolve import (
    add_actions,
    merge_actions,
    remove_actions,
    resolve_by_title,
    resolve_identifiers,
    search_cost,
)

LIST_OPTIONS = ("--doi", "--work", "--bib", "--name", "--status", "--accept", "--reject", "--ids")
OWNER_SELECT = ("id", "display_name", "orcid", "works_count")


# ---------------------------------------------------------------------- argument model


@dataclass(kw_only=True)
class CommonArgs:
    """Options every command accepts (they may follow the subcommand)."""

    author: str = ""  # OpenAlex author ID (A...); defaults to $OACURATE_AUTHOR
    api_key_file: str = ""  # file holding the API key; defaults to $OPENALEX_API_KEY
    journal: str = ""  # journal file; defaults to $XDG_DATA_HOME/oacurate/<author>.jsonl
    format: Literal["table", "tsv", "json"] = "table"  # output format
    max_credits: int = 200  # never send a billed request beyond this many credits
    verbose: bool = False  # log every HTTP request to stderr (the key is redacted)


@dataclass(kw_only=True)
class Show(CommonArgs):
    """Show the profile and the journal status (0 credits)."""


@dataclass(kw_only=True)
class OrcidSet(CommonArgs):
    """Set the profile's ORCID (dry run unless --yes)."""

    orcid: str = field(positional=True)  # the ORCID to set
    force: bool = False  # replace an ORCID the profile already has
    yes: bool = False  # submit for real


@dataclass(kw_only=True)
class OrcidRemove(CommonArgs):
    """Detach an ORCID from the profile (dry run unless --yes)."""

    orcid: str = field(positional=True)  # the ORCID to detach
    yes: bool = False  # submit for real


@dataclass(kw_only=True)
class NameDisplay(CommonArgs):
    """Set the profile's display name (dry run unless --yes)."""

    name: str = field(positional=True)  # the new display name
    yes: bool = False  # submit for real


@dataclass(kw_only=True)
class NameMatch(CommonArgs):
    """Set the profile's match name, full_name (dry run unless --yes)."""

    name: str = field(positional=True)  # one of the profile's raw author names
    force: bool = False  # allow a name that is not among the raw author names
    yes: bool = False  # submit for real


@dataclass(kw_only=True)
class PlanAdd(CommonArgs):
    """Build a plan attaching works from DOIs, work IDs or BibTeX."""

    doi: list[str] = dfield(default_factory=list)  # DOIs (give all after one --doi)
    dois_file: str = ""  # file with one DOI per line; '#' starts a comment
    work: list[str] = dfield(default_factory=list)  # OpenAlex work IDs (W...)
    bib: list[str] = dfield(default_factory=list)  # BibTeX files (e.g. a Scholar export)
    name: list[str] = dfield(default_factory=list)  # extra name forms to match bylines
    orcid: str = ""  # your ORCID, when the profile does not carry it yet
    no_search: bool = False  # never run billed title searches
    splinter_max: int = 3  # owners with at most this many works count as splinters
    coauthor_min: int = 3  # co-author overlap that lets an initial-only byline through
    output: str = field(default="", alias=["-o"])  # plan file to write


@dataclass(kw_only=True)
class PlanRemove(CommonArgs):
    """Build a plan detaching works from the profile."""

    doi: list[str] = dfield(default_factory=list)  # DOIs (give all after one --doi)
    work: list[str] = dfield(default_factory=list)  # OpenAlex work IDs (W...)
    force: bool = False  # detach even where the byline carries your ORCID
    output: str = field(default="", alias=["-o"])  # plan file to write


@dataclass(kw_only=True)
class PlanShow(CommonArgs):
    """Print a plan."""

    plan: str = field(positional=True)  # plan file
    status: list[str] = dfield(
        default_factory=list
    )  # only these statuses ('missing' prints tickets)


@dataclass(kw_only=True)
class PlanPick(CommonArgs):
    """Anchor a claim row on the byline at a given author position."""

    plan: str = field(positional=True)  # plan file (rewritten in place)
    action: str = field(positional=True)  # action ID
    position: int = 0  # 1-based author position, as displayed


@dataclass
class PlanCmd:
    """Build, inspect and adjust plan files."""

    command: PlanAdd | PlanRemove | PlanShow | PlanPick = subparsers(
        {"add": PlanAdd, "remove": PlanRemove, "show": PlanShow, "pick": PlanPick}
    )


@dataclass
class OrcidCmd:
    """Set or detach the profile's ORCID."""

    command: OrcidSet | OrcidRemove = subparsers({"set": OrcidSet, "remove": OrcidRemove})


@dataclass
class NameCmd:
    """Set the display name or the match name."""

    command: NameDisplay | NameMatch = subparsers({"display": NameDisplay, "match": NameMatch})


@dataclass(kw_only=True)
class Audit(CommonArgs):
    """Audit the profile for missing and misattributed works and duplicate profiles."""

    name: list[str] = dfield(default_factory=list)  # extra name forms to match bylines
    orcid: str = ""  # your ORCID, when the profile does not carry it yet
    threshold: int = 100  # stop widening the byline ladder at this many works
    max_step: int = 5  # widest ladder step (1-5)
    merge_duplicates: bool = False  # also propose the works of duplicate profiles
    splinter_max: int = 3  # owners with at most this many works count as splinters
    coauthor_min: int = 3  # co-author overlap that lets an initial-only byline through
    output: str = field(default="", alias=["-o"])  # plan file to write


@dataclass(kw_only=True)
class Apply(CommonArgs):
    """Submit a plan (dry run unless --yes)."""

    plan: str = field(positional=True)  # plan file
    accept: list[str] = dfield(default_factory=list)  # also submit these action or work IDs
    reject: list[str] = dfield(default_factory=list)  # never submit these action or work IDs
    chunk: int = 25  # items per POST after the canary
    yes: bool = False  # submit for real


@dataclass(kw_only=True)
class Pending(CommonArgs):
    """Reconcile the journal with the server's curation records."""


@dataclass(kw_only=True)
class Revert(CommonArgs):
    """Build the inverse plan of journalled curations."""

    since: str = ""  # revert everything submitted on or after DD/MM/YYYY (local)
    ids: list[str] = dfield(default_factory=list)  # revert these curation IDs (cur-...)
    output: str = field(default="", alias=["-o"])  # plan file to write


@dataclass
class Cli:
    """Curate your own OpenAlex author profile through the official curation API."""

    command: Show | OrcidCmd | NameCmd | PlanCmd | Audit | Apply | Pending | Revert = subparsers(
        {
            "show": Show,
            "orcid": OrcidCmd,
            "name": NameCmd,
            "plan": PlanCmd,
            "audit": Audit,
            "apply": Apply,
            "pending": Pending,
            "revert": Revert,
        }
    )


class Parser(ArgumentParser):
    """``simple_parsing`` parser with abbreviations off at every level and exit code 1 on misuse.

    ``simple_parsing`` builds subparsers with ``parser_class=type(parser)``, so forcing
    ``allow_abbrev=False`` here reaches every subcommand. Without it, ``--api_key x`` would
    silently be accepted as an abbreviation of ``--api_key_file``.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Force ``allow_abbrev=False``."""
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> NoReturn:
        """Print usage and exit with the usage exit code."""
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def build_parser() -> Parser:
    """Build the top-level parser."""
    parser = Parser(prog="oacurate", description=Cli.__doc__)
    parser.add_argument("--version", action="version", version=f"oacurate {__version__}")
    parser.add_arguments(Cli, dest="cli")
    return parser


def check_repeated_options(argv: Sequence[str]) -> None:
    """Refuse a repeated option: ``simple_parsing`` would silently keep only the last."""
    counts: Counter[str] = Counter()
    for token in argv:
        if token == "--":
            break
        name = token.split("=", 1)[0]
        name = "--output" if name == "-o" else name
        if name.startswith("--"):
            counts[name] += 1
    for name, n in counts.items():
        if n > 1:
            hint = f"; list every value after a single {name} (e.g. {name} A B C)"
            raise UsageError(
                f"{name} given {n} times; only the last would count"
                + (hint if name in LIST_OPTIONS else "")
            )


# ---------------------------------------------------------------------- runtime helpers


def _err(msg: str) -> None:
    """Write a diagnostic line to stderr."""
    print(msg, file=sys.stderr)


def _out(text: str) -> None:
    """Write data to stdout."""
    print(text)


def setup_logging(verbose: bool) -> None:
    """Route library logging to stderr; silence bibtexparser (failed blocks are reported)."""
    logging.getLogger("bibtexparser").setLevel(logging.CRITICAL)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    for name, level in (("oacurate", logging.DEBUG), ("httpx", logging.INFO)):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(level if verbose else logging.WARNING)


def resolve_author(arg: str, *, required: bool = True) -> str | None:
    """Return the long author ID from ``--author`` or ``$OACURATE_AUTHOR``."""
    value = arg or os.environ.get("OACURATE_AUTHOR", "")
    if not value:
        if required:
            raise UsageError("no author: pass --author A... or set OACURATE_AUTHOR")
        return None
    return normalise_openalex_id(value, "A")[1]


def load_api_key(path_arg: str) -> str | None:
    """Read the API key from ``--api_key_file`` or ``$OPENALEX_API_KEY``; never from argv."""
    if path_arg:
        path = Path(path_arg).expanduser()
        try:
            mode = path.stat().st_mode
            key = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise UsageError(f"cannot read --api_key_file {path}: {exc.strerror}") from exc
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            _err(f"warning: {path} is readable by group or others; run `chmod 600 {path}`")
        if not key:
            raise UsageError(f"--api_key_file {path} is empty")
        return key
    return os.environ.get("OPENALEX_API_KEY") or None


@dataclass
class Runtime:
    """Per-command state: the configured author, the client and the journal."""

    author_id: str
    client: OpenAlexClient
    journal: Journal
    fmt: Format

    @property
    def author_short(self) -> str:
        """The short author ID."""
        return short_id(self.author_id)


@contextmanager
def runtime(args: CommonArgs) -> Iterator[Runtime]:
    """Build the runtime for a command and close the client afterwards."""
    author_id = resolve_author(args.author)
    if author_id is None:  # pragma: no cover - resolve_author raises when required
        raise UsageError("no author")
    client = OpenAlexClient(load_api_key(args.api_key_file), budget=CreditBudget(args.max_credits))
    journal = Journal(args.journal or default_journal_path(short_id(author_id)))
    try:
        yield Runtime(author_id, client, journal, args.format)
    finally:
        client.close()


def fetch_author(rt: Runtime) -> tuple[AuthorView, dict[str, Any]]:
    """Look up the configured author (free)."""
    data = rt.client.get_author(rt.author_short)
    if data is None:
        raise UsageError(f"{rt.author_short} does not resolve in OpenAlex (merged or deleted?)")
    return AuthorView.from_json(data), data


def owner_lookup(client: OpenAlexClient) -> Callable[[str], AuthorView | None]:
    """A memoised, free lookup of an authorship's current owner."""

    def lookup(author_id: str) -> AuthorView | None:
        """Fetch the owner's ID, name, ORCID and works count, or ``None``."""
        data = client.get_author(author_id, select=OWNER_SELECT)
        return AuthorView.from_json(data) if data else None

    return lookup


def require_output(path: str) -> str:
    """Insist on ``-o PLAN``."""
    if not path:
        raise UsageError("give the plan file to write with -o PLAN")
    return path


def make_context(
    rt: Runtime, author: AuthorView, names: Sequence[str], orcid: str, **kw: Any
) -> Context:
    """Seeds, ORCID and owner lookup for classification."""
    my_orcid = normalise_orcid(orcid) if orcid else author.orcid
    seeds = build_seeds(
        author.display_name, author.raw_author_names, author.display_name_alternatives, names
    )
    return Context(
        target_id=author.id, my_orcid=my_orcid, seeds=seeds, owner_of=owner_lookup(rt.client), **kw
    )


def _counts_line(actions: Sequence[Action]) -> str:
    """``claim 3 · review 2`` style status counts."""
    return ", ".join(f"{s} {n}" for s, n in status_counts(actions)) or "no actions"


def emit_plan(
    plan: Plan, fmt: Format, *, tickets: bool = False, actions: Sequence[Action] | None = None
) -> None:
    """Print a plan: actions, unresolved inputs, warnings, counts (and ticket texts)."""
    shown = plan.actions if actions is None else list(actions)
    missing = [u for u in plan.unresolved if u.reason == "missing"]
    if fmt == "json":
        doc: dict[str, Any] = {
            "author_id": plan.author_id,
            "counts": dict(status_counts(shown)),
            "actions": [plan_row(a) for a in shown],
            "unresolved": [u.to_dict() for u in plan.unresolved],
            "warnings": plan.warnings,
            "credits_used": plan.credits_used,
        }
        if tickets:
            doc["tickets"] = [ticket_text(u, plan.author_id) for u in missing]
        _out(json.dumps(doc, indent=2, ensure_ascii=False))
        return
    _out(render_plan(shown, fmt))
    if plan.unresolved and fmt == "table":
        _out("\nUnresolved inputs:\n" + render_unresolved(plan.unresolved, fmt))
    elif plan.unresolved:
        _err(render_unresolved(plan.unresolved, "table"))
    if tickets:
        for u in missing:
            _out("\n" + ticket_text(u, plan.author_id))
    for w in plan.warnings:
        _err(f"warning: {w}")
    _err(f"{len(shown)} actions: {_counts_line(shown)}; {len(plan.unresolved)} unresolved inputs")
    if missing and not tickets:
        _err(
            f"inputs missing from OpenAlex: {len(missing)}; "
            "`plan show PLAN --status missing` prints ticket text"
        )


RESULT_COLUMNS = (
    Column("id", "action"),
    Column("target", "target"),
    Column("outcome", "outcome"),
    Column("http", "http"),
    Column("curation", "curation"),
    Column("message", "message", 60),
)


def _target(a: Action) -> str:
    """Short label of what an action touches."""
    return a.evidence.get("work") or short_id(a.entity_id) if a.kind in WORK_KINDS else a.kind


def emit_report(report: ApplyReport, fmt: Format, credits_used: int) -> int:
    """Print an apply report and return the exit code."""
    if report.dry_run:
        _out(render_plan(report.selected, fmt) if fmt != "json" else "")
        _out(payloads_json(report.selected))
        _err(
            f"dry run: nothing sent; {len(report.selected)} items would be submitted. "
            "Add --yes to submit"
        )
        return EXIT_OK
    for note in report.notes:
        _err(f"note: {note}")
    for a, why in report.stale:
        _err(f"skipped {a.id} ({_target(a)}): {why}")
    rows = [
        {
            "id": a.id,
            "target": _target(a),
            "outcome": o.outcome,
            "http": o.http_status,
            "curation": o.curation_id,
            "message": o.message,
        }
        for a, o in report.results
    ]
    if fmt == "json":
        doc = {
            "counts": report.counts(),
            "results": rows,
            "stale": [{"id": a.id, "target": _target(a), "reason": why} for a, why in report.stale],
            "leftover": [a.id for a in report.leftover],
            "unsent": [a.id for a in report.unsent],
            "stopped": report.stopped,
            "notes": report.notes,
            "credits_used": credits_used,
            "exit_code": report.exit_code,
        }
        _out(json.dumps(doc, indent=2, ensure_ascii=False))
    else:
        _out(render_rows(rows, RESULT_COLUMNS, fmt))
    dups = [r for r in rows if r["outcome"] == "duplicate"]
    if dups:
        _err(
            "already on file (duplicates, counted as success): " + ", ".join(r["id"] for r in dups)
        )
    if report.leftover:
        when = (
            f"after {format_local(report.window_frees_at)}"
            if report.window_frees_at
            else "tomorrow (this batch alone exceeds the cap)"
        )
        _err(f"{len(report.leftover)} claims exceed the 1,000-per-24 h cap; run apply again {when}")
    if report.unsent:
        _err(
            f"not sent because the run stopped ({len(report.unsent)}): "
            + ", ".join(f"{a.id} ({_target(a)})" for a in report.unsent)
        )
    if report.stopped:
        _err(f"stopped: {report.stopped}")
    counts = ", ".join(f"{k} {v}" for k, v in report.counts().items())
    _err(
        f"submitted {len(report.results)}: {counts}; stale {len(report.stale)}; "
        f"left over {len(report.leftover)}; unsent {len(report.unsent)}; "
        f"credits used {credits_used}"
    )
    return report.exit_code


# ---------------------------------------------------------------------- command handlers


def cmd_show(a: Show) -> int:
    """Profile summary plus journal status."""
    with runtime(a) as rt:
        author, data = fetch_author(rt)
        entries, warnings = rt.journal.read()
        for w in warnings:
            _err(f"warning: {w}")
        outcomes = Counter(e.outcome for e in entries)
        recent = claims_in_window(entries, now_utc())
        last = max((parse_iso_utc(e.ts_utc) for e in entries), default=None)
        institutions = [i.get("display_name") for i in data.get("last_known_institutions") or []]
        pairs = [
            ("author", rt.author_short),
            ("display_name", author.display_name),
            ("full_name", author.full_name),
            ("orcid", author.orcid),
            ("works_count", author.works_count),
            ("raw_author_names", list(author.raw_author_names)),
            ("display_name_alternatives", list(author.display_name_alternatives)),
            ("last_known_institutions", institutions),
            ("journal", str(rt.journal.path)),
            ("journal_entries", len(entries)),
            (
                "journal_outcomes",
                dict(outcomes)
                if rt.fmt == "json"
                else ", ".join(f"{k} {v}" for k, v in sorted(outcomes.items())),
            ),
            ("claims_last_24h", len(recent)),
            ("last_submission", format_local(last)),
            ("credits_used", rt.client.credits_used),
        ]
        _out(render_mapping(pairs, rt.fmt))
        return EXIT_OK


def _submit(rt: Runtime, plan: Plan, opts: ApplyOptions) -> int:
    """Show what will be sent, run the apply pipeline and report."""
    if opts.yes:
        selected = select_actions(plan, opts.accept, opts.reject)
        _err(f"sending {len(selected)} items:\n" + render_plan(selected, "table"))
    report = apply_plan(rt.client, plan, rt.journal, opts)
    return emit_report(report, rt.fmt, rt.client.credits_used)


def _run_single(rt: Runtime, action: Action, yes: bool, display_name: str) -> int:
    """Pass a one-action plan through the normal apply path."""
    plan = Plan(author_id=rt.author_id, author_display_name=display_name, actions=[action])
    plan.validate(rt.author_id)
    return _submit(rt, plan, ApplyOptions(yes=yes))


def _author_action(
    author: AuthorView, kind: str, value: str, previous: str | None, status: str = "claim"
) -> Action:
    """An author-level action ready to apply."""
    return Action(
        kind=kind,  # type: ignore[arg-type]
        entity_id=author.id,
        value=value,
        previous=previous,
        status=status,  # type: ignore[arg-type]
        decision="apply",
    )


def cmd_orcid_set(a: OrcidSet) -> int:
    """Set the profile's ORCID after local and remote checks."""
    orcid = normalise_orcid(a.orcid)
    with runtime(a) as rt:
        author, _ = fetch_author(rt)
        other = rt.client.get_author("orcid:" + orcid_bare(orcid), select=OWNER_SELECT)
        if other and other.get("id") != author.id:
            _err(
                f"warning: {orcid} already resolves to {short_id(other['id'])} "
                f"({other.get('display_name')}, {other.get('works_count')} works), "
                "probably a duplicate "
                "profile. Setting the ORCID will not merge them; see `audit --merge_duplicates`."
            )
        if author.orcid == orcid:
            _err("the profile already carries this ORCID; nothing to do")
            return EXIT_OK
        if author.orcid and not a.force:
            raise UsageError(
                f"the profile already carries {author.orcid}; use --force to replace it"
            )
        return _run_single(
            rt, _author_action(author, "set_orcid", orcid, author.orcid), a.yes, author.display_name
        )


def cmd_orcid_remove(a: OrcidRemove) -> int:
    """Detach an ORCID from the profile."""
    orcid = normalise_orcid(a.orcid)
    with runtime(a) as rt:
        author, _ = fetch_author(rt)
        if author.orcid != orcid:
            _err(
                f"warning: the profile currently shows ORCID {author.orcid or '(none)'}, "
                f"not {orcid}"
            )
        action = _author_action(author, "remove_orcid", orcid, author.orcid, status="remove")
        return _run_single(rt, action, a.yes, author.display_name)


def cmd_name_display(a: NameDisplay) -> int:
    """Set the display name."""
    name = a.name.strip()
    with runtime(a) as rt:
        author, _ = fetch_author(rt)
        if name == author.display_name:
            _err("the profile already has this display name; nothing to do")
            return EXIT_OK
        action = _author_action(author, "set_display_name", name, author.display_name)
        return _run_single(rt, action, a.yes, author.display_name)


def cmd_name_match(a: NameMatch) -> int:
    """Set the match name (``full_name``)."""
    name = a.name.strip()
    with runtime(a) as rt:
        author, _ = fetch_author(rt)
        if name not in author.raw_author_names and not a.force:
            known = "; ".join(author.raw_author_names) or "(none)"
            raise UsageError(
                f"{name!r} is not one of the profile's raw author names ({known}); use --force"
            )
        if name == author.full_name:
            _err("the profile already has this match name; nothing to do")
            return EXIT_OK
        action = _author_action(author, "set_full_name", name, author.full_name)
        return _run_single(rt, action, a.yes, author.display_name)


def _gather(a: PlanAdd) -> tuple[list[InputRecord], list[UnresolvedInput]]:
    """Collect input records from every input option."""
    records: list[InputRecord] = []
    unresolved: list[UnresolvedInput] = []
    sources: list[tuple[list[InputRecord], list[UnresolvedInput]]] = [
        records_from_dois(a.doi),
        records_from_works(a.work),
    ]
    if a.dois_file:
        sources.append(read_doi_list(a.dois_file))
    sources += [read_bib(path) for path in a.bib]
    for r, u in sources:
        records += r
        unresolved += u
    return records, unresolved


def _record_warnings(records: Sequence[InputRecord]) -> list[str]:
    """Plan warnings carried by individual input records."""
    return [f"{r.ref}: {n}" for r in records for n in r.notes]


def cmd_plan_add(a: PlanAdd) -> int:
    """Resolve inputs, classify every candidate work and write the plan."""
    out = require_output(a.output)
    records, unresolved = _gather(a)
    if not records and not unresolved:
        raise UsageError("no input: give --doi, --dois_file, --work or --bib")
    with runtime(a) as rt:
        author, _ = fetch_author(rt)
        ctx = make_context(
            rt, author, a.name, a.orcid, splinter_max=a.splinter_max, coauthor_min=a.coauthor_min
        )
        resolved, need_search, missing = resolve_identifiers(rt.client, records)
        unresolved += missing
        if need_search and a.no_search:
            unresolved += [
                UnresolvedInput(
                    ref=r.ref,
                    reason="search-skipped",
                    detail="title-only input and --no_search",
                    record=r.to_dict(),
                )
                for r in need_search
            ]
        elif need_search:
            cost = search_cost(need_search)
            _err(f"{len(need_search)} title-only inputs: title search will cost ~{cost} credits")
            if rt.client.credits_used + cost > a.max_credits:
                raise BudgetExceeded(
                    f"title search needs ~{cost} credits, over --max_credits {a.max_credits}; "
                    "raise it or pass --no_search. Nothing was searched."
                )
            more, still = resolve_by_title(rt.client, need_search)
            resolved += more
            unresolved += still
        actions, failed = add_actions(rt.client, resolved, ctx)
        plan = Plan(
            author_id=author.id,
            author_display_name=author.display_name,
            seeds=list(ctx.seeds.names),
            actions=actions,
            unresolved=unresolved + failed,
            warnings=_record_warnings(records),
            credits_used=rt.client.credits_used,
        ).sorted()
        save_plan(plan, out)
        emit_plan(plan, rt.fmt)
        _err(f"plan written to {out}; credits used {rt.client.credits_used}")
        return EXIT_OK


def cmd_plan_remove(a: PlanRemove) -> int:
    """Resolve the given works and classify each for detachment."""
    out = require_output(a.output)
    r1, u1 = records_from_dois(a.doi)
    r2, u2 = records_from_works(a.work)
    records, unresolved = r1 + r2, u1 + u2
    if not records and not unresolved:
        raise UsageError("no input: give --doi or --work")
    with runtime(a) as rt:
        author, _ = fetch_author(rt)
        ctx = make_context(rt, author, (), "", force=a.force)
        resolved, _, missing = resolve_identifiers(rt.client, records)
        actions, failed = remove_actions(rt.client, resolved, ctx)
        plan = Plan(
            author_id=author.id,
            author_display_name=author.display_name,
            seeds=list(ctx.seeds.names),
            actions=actions,
            unresolved=unresolved + missing + failed,
            credits_used=rt.client.credits_used,
        ).sorted()
        save_plan(plan, out)
        emit_plan(plan, rt.fmt)
        _err(f"plan written to {out}; credits used {rt.client.credits_used}")
        return EXIT_OK


def cmd_plan_show(a: PlanShow) -> int:
    """Print a plan, optionally filtered by status."""
    plan = load_plan(a.plan, resolve_author(a.author, required=False))
    unknown = [s for s in a.status if s not in STATUSES]
    if unknown:
        raise UsageError(f"unknown status {', '.join(unknown)}; choose from {', '.join(STATUSES)}")
    shown = [x for x in plan.actions if not a.status or x.status in a.status]
    emit_plan(plan, a.format, tickets="missing" in a.status, actions=shown)
    return EXIT_OK


def cmd_plan_pick(a: PlanPick) -> int:
    """Re-anchor one claim row on a hand-chosen byline."""
    if a.position < 1:
        raise UsageError("give --position N (1-based, as displayed)")
    plan = load_plan(a.plan, resolve_author(a.author, required=False))
    target = next((x for x in plan.actions if x.id == a.action), None)
    if target is None:
        raise UsageError(f"no action {a.action} in {a.plan}")
    if target.kind != "claim_work" or target.status == "already":
        raise UsageError(
            f"action {a.action} is a {target.status} {target.kind} row; "
            "only claim rows can be picked"
        )
    with runtime(replace(a, author=a.author or plan.author_id)) as rt:
        data = rt.client.get_work(target.entity_id)
        if data is None:
            raise UsageError(f"{short_id(target.entity_id)} no longer resolves")
        ctx = Context(
            target_id=plan.author_id,
            my_orcid=None,
            seeds=Seeds.from_names(plan.seeds),
            owner_of=owner_lookup(rt.client),
        )
        try:
            picked = pick(target, WorkView.from_json(data), a.position, ctx)
        except ValueError as exc:
            raise UsageError(str(exc)) from exc
        others = [x for x in plan.actions if x is not target]
        plan = replace(plan, actions=merge_actions([*others, picked])).sorted()
        save_plan(plan, a.plan)
        _out(render_plan([picked], rt.fmt))
        ev = picked.evidence
        if ev.get("tier") == "none":
            _err("warning: the picked byline matches none of the plan's name seeds")
        if ev.get("owner") and (
            ev.get("owner_orcid") or (ev.get("owner_works_count") or 0) > ctx.splinter_max
        ):
            _err(
                f"warning: the picked byline belongs to {ev['owner']} "
                f"({ev.get('owner_works_count')} works, ORCID {ev.get('owner_orcid') or 'none'})"
            )
        _err(
            f"action {target.id} is now {picked.id} ({picked.status}, "
            f"decision {picked.decision}); {a.plan} updated"
        )
        return EXIT_OK


def cmd_audit(a: Audit) -> int:
    """Run the audit and write its plan."""
    out = require_output(a.output)
    if not 1 <= a.max_step <= 5:
        raise UsageError("--max_step must be between 1 and 5")
    with runtime(a) as rt:
        author, _ = fetch_author(rt)
        ctx = make_context(
            rt, author, a.name, a.orcid, splinter_max=a.splinter_max, coauthor_min=a.coauthor_min
        )
        opts = AuditOptions(a.threshold, a.max_step, a.merge_duplicates, a.max_credits)
        result = run_audit(rt.client, author, ctx, opts, notify=_err)
        plan = Plan(
            author_id=author.id,
            author_display_name=author.display_name,
            seeds=list(ctx.seeds.names),
            actions=result.actions,
            warnings=result.warnings,
            credits_used=rt.client.credits_used,
        ).sorted()
        save_plan(plan, out)
        emit_plan(plan, rt.fmt)
        _err(
            f"plan written to {out}; estimated ~{result.estimate}, "
            f"used {rt.client.credits_used} credits"
        )
        return EXIT_OK


def cmd_apply(a: Apply) -> int:
    """Submit a plan."""
    with runtime(a) as rt:
        plan = load_plan(a.plan, rt.author_id)
        opts = ApplyOptions(accept=a.accept, reject=a.reject, chunk=a.chunk, yes=a.yes)
        return _submit(rt, plan, opts)


PENDING_COLUMNS = (
    Column("state", "state"),
    Column("curation", "curation"),
    Column("kind", "kind", 28),
    Column("entity", "entity"),
    Column("value", "value", 40),
    Column("since", "since"),
    Column("age", "age"),
    Column("applied_at", "applied"),
)


def cmd_pending(a: Pending) -> int:
    """Reconcile the journal with ``GET /curations``."""
    with runtime(a) as rt:
        entries, warnings = rt.journal.read()
        for w in warnings:
            _err(f"warning: {w}")
        records = fetch_curations(rt.client.get_curations)
        if records is None:
            _err("warning: unrecognised GET /curations response shape; showing the journal only")
        rows = reconcile(entries, records or [], server_known=records is not None)
        now = datetime.now(UTC)
        table = [
            {
                "state": r.state,
                "curation": r.curation_id,
                "kind": r.kind,
                "entity": r.entity,
                "value": r.value,
                "since": format_local(r.since),
                "age": format_age((now - r.since).total_seconds()) if r.since else None,
                "applied_at": format_local(r.applied_at),
            }
            for r in rows
        ]
        _out(render_rows(table, PENDING_COLUMNS, rt.fmt))
        _err(
            ", ".join(f"{k} {v}" for k, v in Counter(r.state for r in rows).items())
            or "nothing journalled"
        )
        return EXIT_OK


def cmd_revert(a: Revert) -> int:
    """Build the inverse plan of journalled curations."""
    out = require_output(a.output)
    since = None
    if a.since:
        try:
            since = datetime.strptime(a.since, "%d/%m/%Y").astimezone().astimezone(UTC)
        except ValueError as exc:
            raise UsageError(f"--since wants DD/MM/YYYY, got {a.since!r}") from exc
    with runtime(a) as rt:
        entries, warnings = rt.journal.read()
        for w in warnings:
            _err(f"warning: {w}")
        chosen = select_for_revert(entries, since=since, ids=a.ids)
        unknown = unknown_in_range(entries, since) if since else []
        unknown_note = (
            f"{len(unknown)} journal entries in range have an unknown outcome and are not "
            "reverted ("
            + ", ".join(
                f"{e.action_id} {e.kind} {short_id(str(e.payload.get('entity_id', '')))}"
                for e in unknown
            )
            + "); run `pending` to see whether they reached the server"
        )
        if not chosen:
            raise UsageError(
                "no successful journal entries match" + (f"; {unknown_note}" if unknown else "")
            )
        author, _ = fetch_author(rt)
        plan = build_revert_plan(chosen, rt.author_id, author.display_name)
        if unknown:
            plan = replace(plan, warnings=[*plan.warnings, unknown_note])
        plan.validate(rt.author_id)
        plan = plan.sorted()
        save_plan(plan, out)
        emit_plan(plan, rt.fmt)
        _err(
            f"inverse plan for {len(chosen)} curations written to {out}; "
            f"review it, then `oacurate apply {out}`"
        )
        return EXIT_OK


HANDLERS: dict[type, Callable[[Any], int]] = {
    Show: cmd_show,
    OrcidSet: cmd_orcid_set,
    OrcidRemove: cmd_orcid_remove,
    NameDisplay: cmd_name_display,
    NameMatch: cmd_name_match,
    PlanAdd: cmd_plan_add,
    PlanRemove: cmd_plan_remove,
    PlanShow: cmd_plan_show,
    PlanPick: cmd_plan_pick,
    Audit: cmd_audit,
    Apply: cmd_apply,
    Pending: cmd_pending,
    Revert: cmd_revert,
}


def parse(argv: Sequence[str]) -> CommonArgs:
    """Parse ``argv`` down to the leaf command's dataclass."""
    check_repeated_options(argv)
    node: Any = build_parser().parse_args(list(argv)).cli
    while not isinstance(node, CommonArgs):
        node = node.command
    return node


def _label(exc: OacurateError) -> str:
    """A short label for the error class."""
    if isinstance(exc, AuthError):
        return "authentication error"
    if isinstance(exc, BudgetError):
        return "budget"
    if isinstance(exc, InvalidIdentifier):
        return "invalid identifier"
    return "error"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return the exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        leaf = parse(args)
        setup_logging(leaf.verbose)
        return HANDLERS[type(leaf)](leaf)
    except OacurateError as exc:
        _err(f"oacurate: {_label(exc)}: {exc}")
        return exc.exit_code
    except KeyboardInterrupt:
        _err("interrupted")
        return 130
