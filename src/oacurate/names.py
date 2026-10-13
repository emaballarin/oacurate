"""Byline tokenising, seed name forms, the byline matching gate and audit phrase ladders.

Re-implements the byline logic of OpenAlex's "Audit a profile" recipe
(https://help.openalex.org/tutorials/audit-a-profile). Deliberate departures from it:
hyphens, dots and apostrophes become token separators instead of being deleted (so
"García-Martínez" keeps two tokens); letters that NFD cannot decompose (``ø``, ``ł``, …)
are transliterated instead of dropped; generational suffixes (``Jr``, ``III``, …) are
ignored.
"""

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from oacurate.model import AuthorshipView, WorkView

Tier = Literal["full", "initial", "none"]

_TIER_RANK: dict[str, int] = {"none": 0, "initial": 1, "full": 2}
_SEPARATORS = re.compile("[-.'`\u2010-\u2015\u2212\u2018\u2019\u02bc]")
_NOT_KEPT = re.compile(r"[^a-z, ]")
_TRANSLITERATE = str.maketrans(
    {
        "\u00f8": "o",
        "\u0142": "l",
        "\u0111": "d",
        "\u00f0": "d",
        "\u00e6": "ae",
        "\u0153": "oe",
        "\u00fe": "th",
        "\u0131": "i",
    }
)
_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})


def name_tokens(name: str) -> list[str]:
    """Split a personal name into lower-case ASCII tokens, given names first."""
    s = unicodedata.normalize("NFD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.casefold().translate(_TRANSLITERATE)
    s = _NOT_KEPT.sub("", _SEPARATORS.sub(" ", s))
    if "," in s:
        last, _, first = s.partition(",")
        s = first.replace(",", " ") + " " + last
    tokens = s.split()
    if len(tokens) > 2:
        kept = [tokens[0], *(t for t in tokens[1:] if t not in _SUFFIXES)]
        if len(kept) >= 2:
            tokens = kept
    return tokens


@dataclass(frozen=True)
class Seeds:
    """The name forms a byline is compared against, with the strings they came from."""

    names: tuple[str, ...]
    forms: tuple[tuple[str, ...], ...]

    @classmethod
    def from_names(cls, names: Iterable[str]) -> Seeds:
        """Build seeds from already vetted name strings, dropping empties and duplicates."""
        kept_names: list[str] = []
        kept_forms: list[tuple[str, ...]] = []
        for name in names:
            form = tuple(name_tokens(name))
            if form and form not in kept_forms:
                kept_names.append(name)
                kept_forms.append(form)
        return cls(tuple(kept_names), tuple(kept_forms))


def build_seeds(
    display_name: str,
    raw_author_names: Iterable[str] = (),
    alternatives: Iterable[str] = (),
    extra: Iterable[str] = (),
) -> Seeds:
    """Vet profile name forms by surname and add the always-kept command-line forms."""
    display = name_tokens(display_name)
    surname = display[-1] if display else None
    vetted = [display_name] if display else []
    for name in [*raw_author_names, *alternatives]:
        tokens = name_tokens(name)
        if surname is not None and tokens and tokens[-1] == surname:
            vetted.append(name)
    return Seeds.from_names([*vetted, *extra])


def _tier(raw: Sequence[str], seed: Sequence[str]) -> Tier:
    """Compare one tokenised byline with one tokenised seed."""
    if not raw or not seed or raw[-1] != seed[-1]:
        return "none"
    if len(seed) == 1:
        return "initial"
    if len(raw) == 1:
        return "none"
    a, b = raw[0], seed[0]
    if a == b and len(a) > 1:
        return "full"
    if (len(a) == 1 or len(b) == 1) and a[0] == b[0]:
        return "initial"
    return "none"


def match_tier(raw_name: str | None, seeds: Seeds) -> Tier:
    """Return the best tier of ``raw_name`` over every seed form."""
    if not raw_name:
        return "none"
    raw = name_tokens(raw_name)
    best: Tier = "none"
    for form in seeds.forms:
        tier = _tier(raw, form)
        if _TIER_RANK[tier] > _TIER_RANK[best]:
            best = tier
    return best


def matching_authorships(work: WorkView, seeds: Seeds) -> list[tuple[AuthorshipView, Tier]]:
    """Return the authorships of ``work`` whose byline passes the gate, with their tier."""
    out = []
    for authorship in work.authorships:
        tier = match_tier(authorship.raw_author_name, seeds)
        if tier != "none":
            out.append((authorship, tier))
    return out


def _rung_phrases(form: tuple[str, ...], step: int) -> list[str]:
    """Return the unquoted phrases one seed contributes at ladder steps 1-3."""
    phrases = [" ".join(form)]
    if step >= 2 and len(form) >= 2:
        phrases.append(" ".join([form[-1], *form[:-1]]))
        if len(form) >= 3:
            phrases += [f"{form[0]} {form[-1]}", f"{form[-1]} {form[0]}"]
    if step >= 3 and len(form) >= 2 and len(form[0]) > 1:
        phrases += [f"{form[0][0]} {form[-1]}", f"{form[-1]} {form[0][0]}"]
    return phrases


def ladder_phrases(seeds: Seeds, step: int) -> list[str]:
    """Return the quoted ``raw_author_name.search`` phrases of one ladder step (1-5)."""
    if not 1 <= step <= 5:
        raise ValueError(f"ladder step must be in 1..5, got {step}")
    seen: dict[str, None] = {}
    for form in seeds.forms:
        for phrase in _rung_phrases(form, min(step, 3)):
            seen.setdefault(phrase)
    proximity = f"~{step - 3}" if step >= 4 else ""
    return [f'"{p}"{proximity}' for p in seen]
