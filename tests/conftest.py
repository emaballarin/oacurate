"""Shared fixtures: recorded API objects, synthetic builders and a mocked client."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from oacurate.client import OpenAlexClient
from oacurate.names import build_seeds

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "sk-test-SECRET-do-not-leak"
TARGET = "https://openalex.org/A9"
MY_ORCID = "https://orcid.org/0000-0002-1825-0097"
OTHER_ORCID = "https://orcid.org/0000-0002-1694-233X"


def load(kind: str, name: str) -> Any:
    """Load a recorded or synthetic JSON fixture."""
    return json.loads((FIXTURES / kind / f"{name}.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- synthetic builders


def authorship(
    raw: str | None,
    author_id: str | None = None,
    *,
    orcid: str | None = None,
    raw_orcid: str | None = None,
    display_name: str | None = None,
) -> dict[str, Any]:
    """A synthetic authorship object in the API's shape."""
    return {
        "author_position": "middle",
        "author": {"id": author_id, "display_name": display_name or raw, "orcid": orcid},
        "raw_author_name": raw,
        "raw_orcid": raw_orcid,
        "institutions": [],
        "raw_affiliation_strings": [],
    }


def work(wid: str, authorships: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    """A synthetic work object in the API's shape."""
    data = {
        "id": f"https://openalex.org/{wid}",
        "doi": kw.pop("doi", None),
        "title": kw.pop("title", f"Title of {wid}"),
        "display_name": kw.pop("title_dn", None),
        "publication_year": kw.pop("year", 2020),
        "type": kw.pop("type", "article"),
        "is_paratext": kw.pop("is_paratext", False),
        "is_retracted": kw.pop("is_retracted", False),
        "primary_location": {"source": {"display_name": kw.pop("venue", "Journal")}},
        "authorships": authorships,
    }
    data.update(kw)
    return data


def author(
    aid: str, name: str, *, works_count: int = 10, orcid: str | None = None, **kw: Any
) -> dict[str, Any]:
    """A synthetic author object in the API's shape."""
    return {
        "id": f"https://openalex.org/{aid}",
        "display_name": name,
        "orcid": orcid,
        "works_count": works_count,
        "full_name": kw.get("full_name", name),
        "raw_author_names": kw.get("raw_author_names", [name]),
        "display_name_alternatives": kw.get("display_name_alternatives", []),
    }


@pytest.fixture
def seeds():
    """Seeds for the synthetic target author "Jane Doe"."""
    return build_seeds("Jane Doe", ["J. Doe", "Jane A. Doe", "John Smith"])


# ---------------------------------------------------------------- mocked client


class Recorder:
    """Records requests and sleeps for a mocked client."""

    def __init__(self) -> None:
        """Start empty."""
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.now = 0.0

    def sleep(self, s: float) -> None:
        """Fake sleep that advances the fake clock."""
        self.sleeps.append(s)
        self.now += s

    def clock(self) -> float:
        """Fake monotonic clock."""
        return self.now

    @property
    def paths(self) -> list[str]:
        """Request paths, in order."""
        return [r.url.path for r in self.requests]


@pytest.fixture
def make_client() -> Callable[..., tuple[OpenAlexClient, Recorder]]:
    """Build a client whose transport is ``handler(request) -> httpx.Response``."""
    clients = []

    def factory(handler, *, api_key: str | None = SECRET, max_credits: int | None = None):
        from oacurate.client import CreditBudget

        rec = Recorder()

        def transport(request: httpx.Request) -> httpx.Response:
            rec.requests.append(request)
            return handler(request)

        client = OpenAlexClient(
            api_key,
            transport=httpx.MockTransport(transport),
            sleep=rec.sleep,
            clock=rec.clock,
            budget=CreditBudget(max_credits),
        )
        clients.append(client)
        return client, rec

    yield factory
    for c in clients:
        c.close()


def json_response(
    status: int, body: Any, credits: float | None = 0, **headers: str
) -> httpx.Response:
    """An ``httpx.Response`` with a JSON body and an optional credits header."""
    h = dict(headers)
    if credits is not None:
        h["X-RateLimit-Credits-Used"] = str(credits)
    return httpx.Response(status, json=body, headers=h)


class FakeApi:
    """A tiny in-memory OpenAlex: single lookups from dicts, list and POST handlers pluggable."""

    def __init__(self, works: list[dict] = (), authors: list[dict] = ()) -> None:
        """Index the given works and authors by short ID (and DOI / ORCID)."""
        self.works = {w["id"].rsplit("/", 1)[1]: w for w in works}
        self.authors = {a["id"].rsplit("/", 1)[1]: a for a in authors}
        self.by_doi = {
            w["doi"].removeprefix("https://doi.org/").lower(): w for w in works if w.get("doi")
        }
        self.by_orcid = {a["orcid"].rsplit("/", 1)[1]: a for a in authors if a.get("orcid")}
        self.list_handler: Callable[[httpx.Request], httpx.Response] | None = None
        self.post_handler: Callable[[httpx.Request], httpx.Response] | None = None
        self.curations: Any = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Dispatch one request."""
        path = request.url.path
        if request.url.host == "user.openalex.org":
            if request.method == "GET":
                return json_response(200, self.curations, credits=None)
            if self.post_handler is None:
                raise AssertionError(f"unexpected POST {path}")
            return self.post_handler(request)
        if path in ("/works", "/authors"):
            if self.list_handler is None:
                raise AssertionError(f"unexpected billed list request {request.url}")
            return self.list_handler(request)
        ref = path.split("/", 2)[2]
        if path.startswith("/works/"):
            hit = (
                self.by_doi.get(ref[4:].lower()) if ref.startswith("doi:") else self.works.get(ref)
            )
        else:
            hit = self.by_orcid.get(ref[6:]) if ref.startswith("orcid:") else self.authors.get(ref)
        return json_response(200, hit) if hit else httpx.Response(404, text="not found")
