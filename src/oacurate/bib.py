"""Input readers: BibTeX (including Google Scholar exports), DOI lists and OpenAlex W lists."""

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import bibtexparser
from bibtexparser.middlewares import LatexDecodingMiddleware
from bibtexparser.model import DuplicateBlockKeyBlock, Entry
from simple_parsing import Serializable

from oacurate.errors import InvalidIdentifier, UsageError
from oacurate.ids import arxiv_id_from, normalise_doi, normalise_openalex_id
from oacurate.model import UnresolvedInput

_DASHES = "".join(map(chr, (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212)))
_SINGLE_QUOTES = "".join(map(chr, (0x2018, 0x2019, 0x201A, 0x201B, 0x2032, 0x02BC)))
_DOUBLE_QUOTES = "".join(map(chr, (0x201C, 0x201D, 0x201E, 0x201F, 0x2033, 0x00AB, 0x00BB)))
_TO_ASCII = str.maketrans(
    {**dict.fromkeys(_DASHES, "-"), **dict.fromkeys(_SINGLE_QUOTES, "'")}
    | dict.fromkeys(_DOUBLE_QUOTES, '"')
)
_DELETED = str.maketrans("", "", "{}$\\")
_AUTHOR_SPLIT = re.compile(r"\s+and\s+", re.IGNORECASE)
_LATEX_ESCAPE = re.compile(r"\\([_%&#$])")
_YEAR = re.compile(r"\b(\d{4})\b")
_COMMENT = re.compile(r"(?:^|\s)#.*$")
_DOI_URL = re.compile(r"https?://(?:dx\.)?doi\.org/\S+", re.IGNORECASE)


@dataclass(kw_only=True)
class InputRecord(Serializable):
    """One input item to resolve to an OpenAlex work."""

    ref: str
    key: str = ""
    entry_type: str = ""
    title: str = ""
    year: int | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    work_id: str | None = None
    venue: str = ""
    authors: list[str] = field(default_factory=list)
    authors_truncated: bool = False
    notes: list[str] = field(default_factory=list)


def norm_title(s: str) -> str:
    """Normalise a title for exact and near comparison."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    s = s.translate(_TO_ASCII).translate(_DELETED)
    s = "".join(ch if ch.isalnum() else " " for ch in s)
    return " ".join(s.split())


def _clean_text(s: str) -> str:
    """Strip leftover braces and collapse whitespace in a decoded field."""
    return " ".join(s.replace("{", "").replace("}", "").split())


def _clean_identifier(s: str) -> str:
    """Undo BibTeX escaping in an identifier field taken from the undecoded parse."""
    return _LATEX_ESCAPE.sub(r"\1", s.replace("{", "").replace("}", "")).strip()


def _lower_fields(entry: Entry) -> dict[str, str]:
    """Return an entry's fields keyed by lower-cased name."""
    return {f.key.lower(): f.value for f in entry.fields if isinstance(f.value, str)}


def _record_from_entry(
    decoded: Mapping[str, str], raw: Mapping[str, str], key: str, kind: str
) -> InputRecord:
    """Build an ``InputRecord`` from one entry's decoded and undecoded fields."""
    notes: list[str] = []
    ident = {k: _clean_identifier(v) for k, v in raw.items()}
    doi = None
    if ident.get("doi"):
        try:
            doi = normalise_doi(ident["doi"])
        except InvalidIdentifier as exc:
            notes.append(f"ignored malformed doi field: {exc}")
    if doi is None:
        m = _DOI_URL.search(ident.get("url", ""))
        if m:
            try:
                doi = normalise_doi(m.group(0))
            except InvalidIdentifier as exc:
                notes.append(f"ignored malformed doi.org url: {exc}")
    authors = [
        a for a in (_clean_text(x) for x in _AUTHOR_SPLIT.split(decoded.get("author", ""))) if a
    ]
    truncated = bool(authors) and authors[-1].casefold() == "others"
    if truncated:
        authors = authors[:-1]
    year_m = _YEAR.search(decoded.get("year", "") or decoded.get("date", ""))
    return InputRecord(
        ref=f"bib:{key}",
        key=key,
        entry_type=kind.lower(),
        title=_clean_text(decoded.get("title", "")),
        year=int(year_m.group(1)) if year_m else None,
        doi=doi,
        arxiv_id=arxiv_id_from(ident),
        venue=_clean_text(decoded.get("journal") or decoded.get("booktitle") or ""),
        authors=authors,
        authors_truncated=truncated,
        notes=notes,
    )


def parse_bib(text: str, source: str = "") -> tuple[list[InputRecord], list[UnresolvedInput]]:
    """Parse BibTeX text; every failed or duplicate block is returned as unresolved."""
    decoded = bibtexparser.parse_string(text, append_middleware=[LatexDecodingMiddleware()])
    raw = {e.key: e for e in bibtexparser.parse_string(text).entries}
    where = f"{source}:" if source else "line "
    unresolved = []
    for block in decoded.failed_blocks:
        line = block.start_line + 1
        if isinstance(block, DuplicateBlockKeyBlock):
            unresolved.append(
                UnresolvedInput(
                    ref=f"bib:{block.key}",
                    reason="duplicate-key",
                    detail=f"{where}{line}: key {block.key!r} repeats; the first entry is used",
                )
            )
        else:
            error = getattr(block, "error", None)
            reason = (
                getattr(error, "abort_reason", "") or str(error or "") or type(block).__name__
            ).strip()
            snippet = " ".join(block.raw.split())[:80]
            unresolved.append(
                UnresolvedInput(
                    ref=f"bib@{line}",
                    reason="parse-error",
                    detail=f"{where}{line}: {reason}: {snippet}",
                )
            )
    records = []
    for entry in decoded.entries:
        raw_fields = _lower_fields(raw[entry.key]) if entry.key in raw else {}
        records.append(
            _record_from_entry(_lower_fields(entry), raw_fields, entry.key, entry.entry_type)
        )
    return records, unresolved


def read_bib(path: str | Path) -> tuple[list[InputRecord], list[UnresolvedInput]]:
    """Read a BibTeX file (UTF-8)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError(f"cannot read BibTeX file {path}: {exc}") from exc
    return parse_bib(text, source=str(path))


def records_from_dois(
    dois: Iterable[str], origin: str = ""
) -> tuple[list[InputRecord], list[UnresolvedInput]]:
    """Turn DOI strings into records; malformed ones become unresolved inputs."""
    records, unresolved = [], []
    for i, s in enumerate(dois, 1):
        try:
            doi = normalise_doi(s)
        except InvalidIdentifier as exc:
            where = f"{origin}:{i}: " if origin else ""
            unresolved.append(
                UnresolvedInput(ref=f"doi:{s}", reason="invalid-identifier", detail=f"{where}{exc}")
            )
            continue
        records.append(InputRecord(ref=f"doi:{doi}", doi=doi))
    return records, unresolved


def records_from_works(works: Iterable[str]) -> tuple[list[InputRecord], list[UnresolvedInput]]:
    """Turn OpenAlex work IDs into records; malformed ones become unresolved inputs."""
    records, unresolved = [], []
    for s in works:
        try:
            short, long = normalise_openalex_id(s, "W")
        except InvalidIdentifier as exc:
            unresolved.append(
                UnresolvedInput(ref=f"work:{s}", reason="invalid-identifier", detail=str(exc))
            )
            continue
        records.append(InputRecord(ref=f"work:{short}", work_id=long))
    return records, unresolved


def read_doi_list(path: str | Path) -> tuple[list[InputRecord], list[UnresolvedInput]]:
    """Read one DOI per line; ``#`` at line start or after a space starts a comment."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError(f"cannot read DOI list {path}: {exc}") from exc
    records, unresolved = [], []
    for i, line in enumerate(lines, 1):
        item = _COMMENT.sub("", line).strip()
        if item:
            r, u = records_from_dois([item], origin=f"{path}:{i}")
            records += r
            unresolved += u
    return records, unresolved
