"""Byline tokenising, seed vetting, the matching gate and the audit ladder."""

import pytest
from conftest import load

from oacurate.model import AuthorView
from oacurate.names import Seeds, build_seeds, ladder_phrases, match_tier, name_tokens


@pytest.mark.parametrize(
    ("name", "tokens"),
    [
        ("Jörg Müller", ["jorg", "muller"]),
        ("Juan García-Martínez", ["juan", "garcia", "martinez"]),
        ("Müller, Jörg", ["jorg", "muller"]),
        ("Priem, Jason R.", ["jason", "r", "priem"]),
        ("Pat O'Brien", ["pat", "o", "brien"]),
        ("Pat O" + chr(0x2019) + "Brien", ["pat", "o", "brien"]),
        ("J.-P. Dupont", ["j", "p", "dupont"]),
        ("Søren Kierkegaard", ["soren", "kierkegaard"]),
        ("Łukasz Nowak", ["lukasz", "nowak"]),
        ("John Smith Jr.", ["john", "smith"]),
        ("Smith, John, Jr.", ["john", "smith"]),
        ("  ", []),
    ],
)
def test_name_tokens(name, tokens):
    assert name_tokens(name) == tokens


def test_seed_vetting_keeps_same_surname_forms_and_every_command_line_form():
    priem = AuthorView.from_json(load("authors", "A5023888391"))
    seeds = build_seeds(
        priem.display_name,
        priem.raw_author_names,
        priem.display_name_alternatives,
        ["J R Priem-Smith"],
    )
    # "Jason, Priem" is a stray-comma byline: tokenised surname-first it ends in "jason",
    # so surname vetting drops it; the command-line form is kept although its surname differs.
    assert seeds.names == ("Jason R Priem", "J. Priem", "Jason Priem", "J R Priem-Smith")


def test_seed_vetting_drops_other_peoples_names():
    seeds = build_seeds("Jane Doe", ["J. Doe", "John Smith"], ["Doe, Jane A."])
    assert seeds.names == ("Jane Doe", "J. Doe", "Doe, Jane A.")


SEEDS = Seeds.from_names(["Jane Doe"])


@pytest.mark.parametrize(
    ("raw", "tier"),
    [
        ("Jane Doe", "full"),
        ("Doe, Jane", "full"),
        ("Jane A. Doe", "full"),
        ("J. Doe", "initial"),
        ("J Doe", "initial"),
        ("Doe J", "none"),
        ("John Doe", "none"),
        ("Jane Roe", "none"),
        ("Doe", "none"),
        ("", "none"),
        (None, "none"),
    ],
)
def test_match_tier(raw, tier):
    assert match_tier(raw, SEEDS) == tier


def test_an_initial_seed_against_a_full_byline_is_initial_only():
    assert match_tier("Jane Doe", Seeds.from_names(["J. Doe"])) == "initial"


def test_a_single_token_seed_can_only_reach_initial():
    assert match_tier("Plato", Seeds.from_names(["Plato"])) == "initial"
    assert match_tier("A. Plato", Seeds.from_names(["Plato"])) == "initial"


def test_best_tier_over_all_seeds_wins():
    seeds = Seeds.from_names(["J. Doe", "Jane Doe"])
    assert match_tier("Jane Doe", seeds) == "full"


def test_ladder_follows_the_recipe():
    seeds = Seeds.from_names(["Jason R Priem"])
    assert ladder_phrases(seeds, 1) == ['"jason r priem"']
    assert ladder_phrases(seeds, 2) == [
        '"jason r priem"',
        '"priem jason r"',
        '"jason priem"',
        '"priem jason"',
    ]
    step3 = ladder_phrases(seeds, 3)
    assert step3[-2:] == ['"j priem"', '"priem j"']
    assert ladder_phrases(seeds, 4) == [p + "~1" for p in step3]
    assert ladder_phrases(seeds, 5) == [p + "~2" for p in step3]


def test_ladder_skips_initial_forms_for_an_initial_seed():
    assert ladder_phrases(Seeds.from_names(["J. Doe"]), 3) == ['"j doe"', '"doe j"']
