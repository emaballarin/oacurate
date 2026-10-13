"""Normalisation and validation of DOIs, ORCIDs, OpenAlex IDs and arXiv IDs."""

import re
from collections.abc import Mapping
from typing import Literal
from urllib.parse import quote

from oacurate.errors import InvalidIdentifier

OPENALEX_PREFIX = "https://openalex.org/"
ORCID_PREFIX = "https://orcid.org/"

_DOI_PREFIX = re.compile(r"^(?:doi:\s*|https?://(?:dx\.)?doi\.org/)", re.IGNORECASE)
_DOI_SHAPE = re.compile(r"^10\.\d{4,9}/\S+$")
_BRACKETS = {")": "(", "]": "[", "}": "{", ">": "<"}

_ORCID_PREFIX = re.compile(r"^(?:https?://)?(?:www\.)?orcid\.org/", re.IGNORECASE)
_ORCID_SHAPE = re.compile(r"^\d{15}[\dX]$")

_OPENALEX_ID = re.compile(r"^(?:https?://)?(?:www\.)?(?:openalex\.org/)?([A-Za-z])(\d+)$")

_ARXIV_ID = re.compile(
    r"(?<![\w.])(?P<id>\d{4}\.\d{4,5}|[a-z][a-z-]*(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?(?![\d])"
)
_ARXIV_URL = re.compile(r"arxiv\.org/(?:abs|pdf)/(?P<rest>\S+)", re.IGNORECASE)


def normalise_doi(s: str) -> str:
    """Return the bare, lower-cased DOI in ``s`` or raise ``InvalidIdentifier``."""
    doi = _DOI_PREFIX.sub("", s.strip()).strip()
    while doi:
        last = doi[-1]
        unbalanced = last in _BRACKETS and doi.count(last) > doi.count(_BRACKETS[last])
        if last not in ".,;" and not unbalanced:
            break
        doi = doi[:-1]
    doi = doi.lower()
    if not _DOI_SHAPE.match(doi):
        raise InvalidIdentifier(f"not a DOI: {s!r}")
    return doi


def doi_path(doi: str) -> str:
    """Return the free single-lookup path segment (``doi:…``) for a normalised DOI."""
    return "doi:" + quote(doi, safe="/:")


def _orcid_check_char(digits: str) -> str:
    """Compute the ISO 7064 MOD 11-2 check character over the first 15 digits."""
    total = 0
    for ch in digits[:15]:
        total = (total + int(ch)) * 2
    remainder = (12 - total % 11) % 11
    return "X" if remainder == 10 else str(remainder)


def normalise_orcid(s: str) -> str:
    """Return the canonical ``https://orcid.org/XXXX-XXXX-XXXX-XXXX`` or raise."""
    bare = _ORCID_PREFIX.sub("", s.strip()).rstrip("/").replace("-", "").upper()
    if not _ORCID_SHAPE.match(bare):
        raise InvalidIdentifier(f"not an ORCID: {s!r}")
    if _orcid_check_char(bare) != bare[15]:
        raise InvalidIdentifier(f"ORCID check character is wrong: {s!r}")
    return ORCID_PREFIX + "-".join(bare[i : i + 4] for i in range(0, 16, 4))


def orcid_bare(orcid: str) -> str:
    """Return the ``XXXX-XXXX-XXXX-XXXX`` form of an ORCID, validating it."""
    return normalise_orcid(orcid).removeprefix(ORCID_PREFIX)


def orcid_or_none(s: str | None) -> str | None:
    """Normalise an ORCID from API data, returning ``None`` when absent or malformed."""
    if not s:
        return None
    try:
        return normalise_orcid(s)
    except InvalidIdentifier:
        return None


def normalise_openalex_id(s: str, kind: Literal["W", "A"]) -> tuple[str, str]:
    """Return ``(short, long)`` for an OpenAlex work or author ID of the given kind."""
    m = _OPENALEX_ID.match(s.strip())
    if not m:
        raise InvalidIdentifier(f"not an OpenAlex ID: {s!r}")
    letter, digits = m.group(1).upper(), m.group(2)
    if letter != kind:
        raise InvalidIdentifier(f"expected an OpenAlex {kind}… ID, got {s!r}")
    short = letter + digits
    return short, OPENALEX_PREFIX + short


def short_id(long_or_short: str) -> str:
    """Strip the ``https://openalex.org/`` prefix from an OpenAlex ID, if present."""
    return long_or_short.removeprefix(OPENALEX_PREFIX)


def _arxiv_in(text: str) -> str | None:
    """Return the first arXiv ID found in ``text``, without its version suffix."""
    m = _ARXIV_ID.search(text)
    return m.group("id") if m else None


def arxiv_id_from(fields: Mapping[str, str]) -> str | None:
    """Find an arXiv ID in BibTeX fields (keys compared case-insensitively)."""
    f = {k.lower(): v for k, v in fields.items() if isinstance(v, str)}
    eprint = f.get("eprint", "").strip()
    if eprint:
        prefixed = eprint.lower().startswith("arxiv:")
        if prefixed or f.get("archiveprefix", "").strip().lower() == "arxiv":
            aid = _arxiv_in(eprint[6:] if prefixed else eprint)
            if aid:
                return aid
    journal = f.get("journal", "")
    if "arxiv" in journal.lower():
        aid = _arxiv_in(journal)
        if aid:
            return aid
    for key in ("url", "howpublished"):
        m = _ARXIV_URL.search(f.get(key, ""))
        if m:
            aid = _arxiv_in(m.group("rest").removesuffix(".pdf"))
            if aid:
                return aid
    return None


def arxiv_doi(aid: str) -> str:
    """Return the DataCite DOI that OpenAlex stores for an arXiv ID."""
    return "10.48550/arxiv." + aid.lower()
