"""Opt-in smoke tests against free OpenAlex single lookups (``pytest -m live``); keyless."""

import pytest

from oacurate.client import OpenAlexClient

pytestmark = pytest.mark.live

SICI = "10.1002/(SICI)1097-0258(19980430)17:8<857::AID-SIM777>3.0.CO;2-E"


@pytest.fixture
def client():
    with OpenAlexClient(None) as c:
        yield c


def test_free_single_lookups(client):
    assert client.get_author("A5023888391")["orcid"] == "https://orcid.org/0000-0001-6187-6610"
    assert client.get_author("orcid:0000-0001-6187-6610")["id"].endswith("A5023888391")
    assert client.get_work("doi:" + SICI)["id"].endswith("W2146272590")
    assert client.get_work("10.48550/arXiv.1706.03762")["id"].endswith("W2626778328")
    assert client.get_work("doi:10.1234/oacurate-fixture-does-not-exist") is None
    assert client.credits_used == 0
