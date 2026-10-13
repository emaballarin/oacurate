"""HTTP access to the OpenAlex read API and curation API: auth, retries, credit ledger."""

import logging
import math
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from oacurate import __version__
from oacurate.errors import (
    ApiError,
    BadRequest,
    BudgetExceeded,
    BudgetExhausted,
    Forbidden,
    NotFound,
    RateLimited,
    TransportError,
    Unauthorized,
)
from oacurate.ids import doi_path, normalise_doi, normalise_openalex_id, orcid_bare, short_id
from oacurate.model import ItemOutcome

log = logging.getLogger("oacurate.http")

READ_BASE = "https://api.openalex.org"
USER_BASE = "https://user.openalex.org"
USER_AGENT = f"oacurate/{__version__} (+https://github.com/emaballarin/oacurate)"

COST_LIST = 1
COST_SEARCH = 10
MAX_URL_BYTES = 3500
BACKOFF_SECONDS = (1, 2, 4, 8, 16)
MAX_RATE_RETRIES = 3
MAX_RETRY_AFTER = 60

_RECORD_KEYS = ("entity", "entity_id", "property", "action", "value")
_LIST_KEYS = ("results", "items", "data", "curations", "responses")
_NESTED_RECORD_KEYS = ("data", "body", "curation", "result", "record")


def estimate_cost(params: Mapping[str, Any]) -> int:
    """Estimate the credits a list request will be billed: 10 with any search, else 1."""
    has_search = any(k == "search" or k.startswith("search.") for k in params)
    return COST_SEARCH if has_search or ".search" in str(params.get("filter", "")) else COST_LIST


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of ``headers`` safe to log."""
    return {k: ("<redacted>" if k.lower() == "authorization" else v) for k, v in headers.items()}


class CreditBudget:
    """Refuse any billed request that would take the run past ``max_credits``."""

    def __init__(self, max_credits: int | None) -> None:
        """``None`` disables the guard."""
        self.max_credits = max_credits

    def check(self, spent: int, estimate: int) -> None:
        """Raise ``BudgetExceeded`` if ``spent + estimate`` would exceed the budget."""
        if self.max_credits is not None and spent + estimate > self.max_credits:
            raise BudgetExceeded(
                f"the next request needs ~{estimate} credits; {spent} of --max_credits "
                f"{self.max_credits} are already used. Nothing was sent."
            )


@dataclass
class CreditLedger:
    """Credits spent in this run, and the server's latest view of the daily budget."""

    credits_used: int = 0
    remaining: float | None = None
    reset_at: datetime | None = None


def _int_header(resp: httpx.Response, name: str) -> float | None:
    """Read a numeric header, or ``None`` when absent or malformed."""
    value = resp.headers.get(name)
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _json_or_none(resp: httpx.Response) -> Any:
    """Decode a JSON body, or return ``None``."""
    try:
        return resp.json()
    except ValueError:
        return None


def _body_retry_after(resp: httpx.Response) -> float | None:
    """Read ``retryAfter`` (seconds) from a JSON error body, or ``None``."""
    body = _json_or_none(resp)
    value = body.get("retryAfter") if isinstance(body, dict) else None
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _message(body: Any) -> str:
    """Extract a human-readable message from an error body."""
    if isinstance(body, dict):
        for key in ("message", "error", "detail"):
            if isinstance(body.get(key), str):
                return body[key]
    return "" if body is None else str(body)[:300]


class OpenAlexClient:
    """Synchronous client for the read API and the curation API."""

    def __init__(
        self,
        api_key: str | None,
        *,
        base: str = READ_BASE,
        user_base: str = USER_BASE,
        max_rps: float = 10,
        timeout: float = 30.0,
        budget: CreditBudget | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create the HTTP client; ``transport``, ``sleep`` and ``clock`` exist for tests."""
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.has_key = bool(api_key)
        self.base = base.rstrip("/")
        self.user_base = user_base.rstrip("/")
        self.budget = budget or CreditBudget(None)
        self.ledger = CreditLedger()
        self._min_interval = 1.0 / max_rps if max_rps > 0 else 0.0
        self._last_request: float | None = None
        self._sleep = sleep
        self._clock = clock
        self._memo: dict[tuple[str, tuple[str, ...] | None], Any] = {}
        self._http = httpx.Client(
            headers=headers, timeout=timeout, transport=transport, follow_redirects=True
        )

    # ------------------------------------------------------------------ plumbing

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._http.close()

    def __enter__(self) -> OpenAlexClient:
        """Use as a context manager."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close on exit."""
        self.close()

    @property
    def credits_used(self) -> int:
        """Credits billed so far in this run."""
        return self.ledger.credits_used

    @property
    def remaining(self) -> float | None:
        """The server's last reported remaining daily budget."""
        return self.ledger.remaining

    @property
    def reset_at(self) -> datetime | None:
        """Local time at which the server's daily budget resets."""
        return self.ledger.reset_at

    def url_length(self, path: str, params: Mapping[str, Any]) -> int:
        """Return the length in bytes of the read-API URL for ``path`` and ``params``."""
        return len(str(httpx.URL(self.base + path, params=dict(params))).encode())

    def _throttle(self) -> None:
        """Keep request starts at least ``1 / max_rps`` seconds apart."""
        now = self._clock()
        if self._last_request is not None:
            wait = self._min_interval - (now - self._last_request)
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
        self._last_request = now

    def _account(self, resp: httpx.Response, billed: int) -> None:
        """Update the credit ledger from the response headers."""
        used = _int_header(resp, "X-RateLimit-Credits-Used")
        if used is not None:
            self.ledger.credits_used += math.ceil(used)
        elif billed and resp.status_code < 400:
            self.ledger.credits_used += billed
        remaining = _int_header(resp, "X-RateLimit-Remaining")
        if remaining is not None:
            self.ledger.remaining = remaining
        reset = _int_header(resp, "X-RateLimit-Reset")
        if reset is not None:
            self.ledger.reset_at = datetime.now().astimezone() + timedelta(seconds=reset)

    @staticmethod
    def _check_request(url: str, path: str, params: Mapping[str, Any] | None) -> None:
        """Enforce the request invariants: free path forms, ``corpus=all``, no key in URL."""
        for prefix in ("/works/", "/authors/"):
            if path.startswith(prefix) and "://" in path[len(prefix) :]:
                raise ValueError(f"refusing a billed URL-form single lookup: {path}")
        if params and any(k.lower() == "api_key" for k in params):
            raise ValueError("the API key must never be sent as a query parameter")
        if path == "/works" and (params or {}).get("corpus") != "all":
            raise ValueError("every /works list request must carry corpus=all")
        if len(url.encode()) > 4096:
            raise ValueError(f"request URL is {len(url.encode())} bytes; split the query")

    def _send(
        self,
        method: str,
        base: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        billed: int = 0,
    ) -> httpx.Response:
        """Send one request with budget guard, throttling, retries and accounting."""
        url = str(httpx.URL(base + path, params=dict(params or {})))
        self._check_request(url, path, params)
        if billed:
            self.budget.check(self.ledger.credits_used, billed)
        backoffs = list(BACKOFF_SECONDS)
        rate_retries = 0
        while True:
            self._throttle()
            try:
                resp = self._http.request(method, url, json=json_body)
            except httpx.TransportError as exc:
                if not backoffs:
                    raise TransportError(f"{method} {base}{path} failed: {exc}") from exc
                delay = backoffs.pop(0)
                log.debug("%s %s: %s; retrying in %ss", method, url, exc, delay)
                self._sleep(delay)
                continue
            self._account(resp, billed)
            log.debug(
                "%s %s -> %s (credits used %s, headers %s)",
                method,
                url,
                resp.status_code,
                resp.headers.get("X-RateLimit-Credits-Used"),
                redact_headers(resp.request.headers),
            )
            if resp.status_code >= 500:
                if not backoffs:
                    raise TransportError(
                        f"{method} {base}{path} kept failing with {resp.status_code}",
                        status=resp.status_code,
                        body=_json_or_none(resp),
                    )
                self._sleep(backoffs.pop(0))
                continue
            if resp.status_code == 429:
                self._raise_if_exhausted(resp)
                if rate_retries >= MAX_RATE_RETRIES:
                    raise RateLimited("rate limited (429) after honouring Retry-After 3 times")
                wait = _int_header(resp, "Retry-After") or _body_retry_after(resp) or 1.0
                if wait > MAX_RETRY_AFTER:
                    raise RateLimited(
                        f"rate limited (429) with Retry-After {wait:g} s; not waiting that long"
                    )
                rate_retries += 1
                self._sleep(wait)
                continue
            return resp

    def _raise_if_exhausted(self, resp: httpx.Response) -> None:
        """Raise ``BudgetExhausted`` when a ``429`` body reports the daily budget as spent."""
        body = _json_or_none(resp)
        if not isinstance(body, dict):
            return
        retry_after = body.get("retryAfter")
        exhausted = body.get("dailyRemainingUsd") == 0 or (
            isinstance(retry_after, int | float) and retry_after > 60
        )
        if not exhausted:
            return
        reset_at = self.ledger.reset_at
        if reset_at is None and isinstance(retry_after, int | float):
            reset_at = datetime.now().astimezone() + timedelta(seconds=retry_after)
        when = f"; it resets at {reset_at:%d/%m/%Y %H:%M}" if reset_at else ""
        raise BudgetExhausted(
            f"the daily OpenAlex credit budget is exhausted{when}", reset_at=reset_at
        )

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        """Map an error status to the typed exception hierarchy."""
        status = resp.status_code
        if status < 400:
            return
        body = _json_or_none(resp)
        msg = f"{resp.request.method} {resp.request.url.path} -> {status}: {_message(body)}"
        if status == 400:
            raise BadRequest(msg, status=status, body=body)
        if status == 401:
            hint = " (check OPENALEX_API_KEY or --api_key_file)"
            raise Unauthorized(msg + hint, status=status, body=body)
        if status == 403:
            raise Forbidden(msg, status=status, body=body)
        if status == 404:
            raise NotFound(msg, status=status, body=body)
        raise ApiError(msg, status=status, body=body)

    # ------------------------------------------------------------------ read API

    @staticmethod
    def _work_path(ref: str) -> str:
        """Return the free single-lookup path for a work ID or DOI reference."""
        r = ref.strip()
        if r.lower().startswith("doi:") or r.startswith("10.") or "doi.org/" in r.lower():
            return "/works/" + doi_path(normalise_doi(r))
        return "/works/" + normalise_openalex_id(r, "W")[0]

    @staticmethod
    def _author_path(ref: str) -> str:
        """Return the free single-lookup path for an author ID or ORCID reference."""
        r = ref.strip()
        if r.lower().startswith("orcid:"):
            return "/authors/orcid:" + orcid_bare(r[len("orcid:") :])
        if "orcid.org/" in r.lower():
            return "/authors/orcid:" + orcid_bare(r)
        return "/authors/" + normalise_openalex_id(r, "A")[0]

    def _get_single(self, path: str, select: Sequence[str] | None) -> dict[str, Any] | None:
        """Fetch one entity (free); memoised; ``None`` on ``404``."""
        key = (path, tuple(select) if select else None)
        if key in self._memo:
            return self._memo[key]
        params = {"select": ",".join(select)} if select else None
        resp = self._send("GET", self.base, path, params=params)
        if resp.status_code == 404:
            data = None
        else:
            self._raise_for_status(resp)
            data = resp.json()
        self._memo[key] = data
        if data is not None and select is None and isinstance(data.get("id"), str):
            kind = "/works/" if path.startswith("/works/") else "/authors/"
            self._memo[(kind + short_id(data["id"]), None)] = data
        return data

    def get_work(self, ref: str, select: Sequence[str] | None = None) -> dict[str, Any] | None:
        """Look up one work by ``W…`` ID or DOI (free); ``None`` when not found."""
        return self._get_single(self._work_path(ref), select)

    def get_author(self, ref: str, select: Sequence[str] | None = None) -> dict[str, Any] | None:
        """Look up one author by ``A…`` ID or ORCID (free); ``None`` when not found."""
        return self._get_single(self._author_path(ref), select)

    @staticmethod
    def _list_params(
        filter: str, *, search: str | None, select: Sequence[str] | None, per_page: int
    ) -> dict[str, Any]:
        """Build list-request parameters; ``corpus=all`` is always present."""
        params: dict[str, Any] = {"filter": filter, "per_page": per_page, "corpus": "all"}
        if search:
            params["search"] = search
        if select:
            params["select"] = ",".join(select)
        return params

    def _get_list(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Send one billed list request and return the decoded page."""
        resp = self._send("GET", self.base, path, params=params, billed=estimate_cost(params))
        self._raise_for_status(resp)
        return resp.json()

    def count_works(self, filter: str, search: str | None = None) -> int:
        """Return ``meta.count`` for a works query (billed, ``per_page=1``)."""
        params = self._list_params(filter, search=search, select=["id"], per_page=1)
        return int(self._get_list("/works", params)["meta"]["count"])

    def works_params(
        self,
        filter: str,
        *,
        search: str | None = None,
        select: Sequence[str] | None = None,
        per_page: int = 100,
    ) -> dict[str, Any]:
        """Return the parameters a works list request would use (for URL-length checks)."""
        return self._list_params(filter, search=search, select=select, per_page=per_page)

    def search_works_by_title(
        self, title: str, *, filter: str, select: Sequence[str], per_page: int = 10
    ) -> list[dict[str, Any]]:
        """Run one ``search.title`` query (billed as a search) and return its first page."""
        params = self._list_params(filter, search=None, select=select, per_page=per_page)
        params["search.title"] = title
        return list(self._get_list("/works", params).get("results") or [])

    def iter_works(
        self,
        filter: str,
        *,
        search: str | None = None,
        select: Sequence[str] | None = None,
        per_page: int = 100,
    ) -> Iterator[dict[str, Any]]:
        """Yield every work of a query using cursor paging (billed per page)."""
        params = self._list_params(filter, search=search, select=select, per_page=per_page)
        yield from self._iter_pages("/works", params)

    def list_authors(
        self, filter: str, select: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """Return every author matching ``filter`` (billed per page)."""
        params: dict[str, Any] = {"filter": filter, "per_page": 100}
        if select:
            params["select"] = ",".join(select)
        return list(self._iter_pages("/authors", params))

    def _iter_pages(self, path: str, params: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
        """Follow ``meta.next_cursor`` from ``cursor=*`` until the results run out."""
        cursor: str | None = "*"
        while cursor:
            page = self._get_list(path, {**params, "cursor": cursor})
            results = page.get("results") or []
            yield from results
            cursor = (page.get("meta") or {}).get("next_cursor") if results else None

    # ------------------------------------------------------------------ curation API

    def _require_key(self) -> None:
        """Raise ``Unauthorized`` when no API key is configured."""
        if not self.has_key:
            raise Unauthorized(
                "the curation API needs an API key: set OPENALEX_API_KEY or --api_key_file"
            )

    def get_curations(self, **params: Any) -> Any:
        """``GET /curations`` on the curation API and return the decoded body as is."""
        self._require_key()
        resp = self._send("GET", self.user_base, "/curations", params=params or None)
        self._raise_for_status(resp)
        return _json_or_none(resp)

    def options_curation(self, cid: str) -> dict[str, str]:
        """``OPTIONS /curations/<id>`` and return the response headers (Milestone 0 only)."""
        self._require_key()
        resp = self._send("OPTIONS", self.user_base, f"/curations/{cid}")
        return {"status": str(resp.status_code), **dict(resp.headers)}

    def post_curations(
        self, items: Mapping[str, Any] | Sequence[Mapping[str, Any]]
    ) -> list[ItemOutcome]:
        """POST one curation object, or an array of them, and return one outcome per item.

        Raises ``AuthError`` on ``401``/``403`` and ``ApiError`` on any other HTTP-level
        failure (the caller decides whether to retry the batch).
        """
        self._require_key()
        single = isinstance(items, Mapping)
        payloads = [dict(items)] if single else [dict(i) for i in items]
        body = payloads[0] if single else payloads
        resp = self._send("POST", self.user_base, "/curations", json_body=body)
        if resp.status_code in (401, 403):
            self._raise_for_status(resp)
        return parse_post_response(resp.status_code, _json_or_none(resp), payloads, single=single)


# ---------------------------------------------------------------------- response parsing


def _same(a: Any, b: Any) -> bool:
    """Compare two payload values, tolerating short vs long OpenAlex IDs."""
    return isinstance(a, str) and isinstance(b, str) and short_id(a) == short_id(b)


def record_matches(record: Any, payload: Mapping[str, Any]) -> bool:
    """Tell whether ``record`` is a curation row echoing every field of ``payload``."""
    if not isinstance(record, dict) or not str(record.get("id", "")).startswith("cur-"):
        return False
    return all(k in record and _same(record[k], payload[k]) for k in _RECORD_KEYS)


def _item_status(item: Mapping[str, Any]) -> int | None:
    """Find a per-item HTTP status in a multi-status entry."""
    for key in ("status", "status_code", "http_status", "code"):
        value = item.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _item_record(item: Mapping[str, Any]) -> Any:
    """Find the curation row in a multi-status entry."""
    if str(item.get("id", "")).startswith("cur-"):
        return item
    for key in _NESTED_RECORD_KEYS:
        if isinstance(item.get(key), dict):
            return item[key]
    return None


def _outcome(status: int | None, record: Any, payload: Mapping[str, Any], raw: Any) -> ItemOutcome:
    """Classify one item; anything not positively recognised is ``unknown``."""
    if status in (200, 201) and record_matches(record, payload):
        return ItemOutcome(
            http_status=status,
            outcome="created" if status == 201 else "duplicate",
            curation_id=record["id"],
            message="",
            raw=raw,
        )
    if status is not None and 400 <= status < 500:
        msg = _message(raw.get("body") if isinstance(raw, dict) and "body" in raw else raw)
        return ItemOutcome(
            http_status=status, outcome="rejected", curation_id=None, message=msg, raw=raw
        )
    cid = record.get("id") if isinstance(record, dict) else None
    return ItemOutcome(
        http_status=status,
        outcome="unknown",
        curation_id=cid if isinstance(cid, str) else None,
        message="unrecognised response shape",
        raw=raw,
    )


def parse_post_response(
    status: int, body: Any, payloads: Sequence[Mapping[str, Any]], *, single: bool
) -> list[ItemOutcome]:
    """Turn a ``POST /curations`` response into one outcome per submitted payload.

    The ``207`` item shape is unverified (Milestone 0, U2): only entries carrying an
    explicit per-item status and, for successes, a curation row echoing the payload are
    classified; everything else is ``unknown``, never success.
    """
    if single:
        if status in (200, 201, 400):
            return [_outcome(status, body, payloads[0], body)]
        raise ApiError(f"POST /curations -> {status}: {_message(body)}", status=status, body=body)
    if status != 207:
        raise ApiError(f"POST /curations -> {status}: {_message(body)}", status=status, body=body)
    entries = body if isinstance(body, list) else None
    if isinstance(body, dict):
        entries = next((body[k] for k in _LIST_KEYS if isinstance(body.get(k), list)), None)
    if entries is None or len(entries) != len(payloads):
        return [
            ItemOutcome(status, "unknown", None, "unrecognised 207 body", body) for _ in payloads
        ]
    out = []
    for entry, payload in zip(entries, payloads, strict=True):
        if not isinstance(entry, dict):
            out.append(ItemOutcome(None, "unknown", None, "unrecognised 207 item", entry))
            continue
        out.append(_outcome(_item_status(entry), _item_record(entry), payload, entry))
    return out
