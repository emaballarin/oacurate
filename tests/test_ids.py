"""Identifier normalisation: DOIs, ORCIDs, OpenAlex IDs and arXiv IDs."""

import pytest

from oacurate.errors import InvalidIdentifier
from oacurate.ids import (
    arxiv_doi,
    arxiv_id_from,
    doi_path,
    normalise_doi,
    normalise_openalex_id,
    normalise_orcid,
)

SICI = "10.1002/(SICI)1097-0258(19980430)17:8<857::AID-SIM777>3.0.CO;2-E"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10.7717/peerj.4375", "10.7717/peerj.4375"),
        ("  10.7717/PeerJ.4375 ", "10.7717/peerj.4375"),
        ("doi:10.7717/peerj.4375", "10.7717/peerj.4375"),
        ("DOI: 10.7717/peerj.4375", "10.7717/peerj.4375"),
        ("https://doi.org/10.7717/peerj.4375", "10.7717/peerj.4375"),
        ("http://dx.doi.org/10.7717/peerj.4375", "10.7717/peerj.4375"),
        ("HTTPS://DOI.ORG/10.7717/peerj.4375", "10.7717/peerj.4375"),
        ("10.7717/peerj.4375.", "10.7717/peerj.4375"),
        ("10.7717/peerj.4375,;", "10.7717/peerj.4375"),
        ("10.1000/xyz)", "10.1000/xyz"),
        ("10.1000/abc(1)", "10.1000/abc(1)"),
        ("10.1000/abc(1))", "10.1000/abc(1)"),
        (SICI, SICI.lower()),
        (SICI + ".", SICI.lower()),
    ],
)
def test_normalise_doi(raw, expected):
    assert normalise_doi(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "not-a-doi", "10.12/abc", "10.1234/", "https://example.org/10.1234/x"]
)
def test_normalise_doi_rejects(raw):
    with pytest.raises(InvalidIdentifier):
        normalise_doi(raw)


def test_doi_path_matches_the_probed_encoding():
    assert doi_path(normalise_doi(SICI)) == (
        "doi:10.1002/%28sici%291097-0258%2819980430%2917:8%3C857::aid-sim777%3E3.0.co%3B2-e"
    )


VALID_ORCIDS = [
    "0000-0002-1825-0097",
    "0000-0001-6187-6610",
    "0000-0002-0889-9220",
    "0000-0002-1694-233X",
]


@pytest.mark.parametrize("orcid", VALID_ORCIDS)
@pytest.mark.parametrize(
    "form",
    [
        "{}",
        "{}/",
        "https://orcid.org/{}",
        "http://orcid.org/{}",
        "https://www.orcid.org/{}/",
        "orcid.org/{}",
    ],
)
def test_normalise_orcid_valid(orcid, form):
    assert normalise_orcid(form.format(orcid)) == f"https://orcid.org/{orcid}"


def test_normalise_orcid_unhyphenated_and_lower_x():
    assert normalise_orcid("000000021694233x") == "https://orcid.org/0000-0002-1694-233X"


@pytest.mark.parametrize(
    "orcid", ["0000-0002-1825-0098", "0000-0002-1825-0079", "0000-0002-1825", "abcd"]
)
def test_normalise_orcid_rejects(orcid):
    with pytest.raises(InvalidIdentifier):
        normalise_orcid(orcid)


@pytest.mark.parametrize(
    "raw",
    ["A5023888391", "a5023888391", "openalex.org/A5023888391", "https://openalex.org/A5023888391"],
)
def test_normalise_openalex_id(raw):
    assert normalise_openalex_id(raw, "A") == ("A5023888391", "https://openalex.org/A5023888391")


def test_normalise_openalex_id_rejects_the_wrong_kind():
    with pytest.raises(InvalidIdentifier):
        normalise_openalex_id("W2741809807", "A")
    with pytest.raises(InvalidIdentifier):
        normalise_openalex_id("https://openalex.org/A1x", "A")


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"eprint": "2401.01234v3", "archivePrefix": "arXiv"}, "2401.01234"),
        ({"eprint": "arXiv:2401.0123"}, "2401.0123"),
        ({"eprint": "2401.01234"}, None),
        ({"journal": "arXiv preprint arXiv:2401.01234"}, "2401.01234"),
        ({"journal": "ArXiv e-prints hep-th/9901001v2"}, "hep-th/9901001"),
        ({"journal": "Physical Review 2401.01234"}, None),
        ({"url": "https://arxiv.org/abs/1706.03762v7"}, "1706.03762"),
        ({"url": "http://arxiv.org/pdf/1706.03762v2.pdf"}, "1706.03762"),
        ({"howpublished": "https://arxiv.org/abs/math.GT/0309136"}, "math.GT/0309136"),
        ({"eprint": "math/0309136", "archiveprefix": "ARXIV"}, "math/0309136"),
    ],
)
def test_arxiv_id_from(fields, expected):
    assert arxiv_id_from(fields) == expected


def test_arxiv_doi_is_lower_case():
    assert arxiv_doi("1706.03762") == "10.48550/arxiv.1706.03762"
    assert arxiv_doi("math.GT/0309136") == "10.48550/arxiv.math.gt/0309136"
