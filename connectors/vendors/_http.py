"""Shared HTTP plumbing for the commercial-FinOps-tool connectors.

Every vendor in `connectors/vendors/` talks JSON over HTTPS with some flavour of
bearer token, custom header, or basic auth. They differ in a hundred small ways
-- Cloudability puts the API key in the Basic-auth *username*, CloudZero omits
the `Bearer ` prefix, Finout wants two custom headers -- but they share one set
of cross-cutting concerns: honour `Retry-After` on a 429, back off on a 5xx,
carry a sane timeout, and NEVER let a credential leak into a log line or a repr.

This module centralises exactly those concerns so no vendor module has to get
them right on its own.

  * `VendorSession` -- a thin wrapper over `requests.Session` with bounded
    retry/backoff (it reads `Retry-After`, seconds or HTTP-date), a default
    timeout, small `get_json` / `post_json` helpers, and a `__repr__` that never
    prints headers or auth.
  * `VendorConnector` -- a `Connector` subclass giving every vendor the same
    `_probe(...)` scaffold (times the call, catches everything, scrubs secrets
    out of the error string) and the same "not configured" result, so their
    `test_connection()` implementations stay one line each and can never raise.

No vendor SDKs. `requests` only.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

import requests

import focus
from connectors.base import ConnectionResult, Connector

# Statuses we transparently retry. 429 is rate limiting (honour Retry-After);
# the 5xx set is transient server error.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_RETRIES = 4
_DEFAULT_BACKOFF = 0.5  # seconds; doubled each attempt
_MAX_BACKOFF = 60.0


class VendorSession:
    """A `requests.Session` with retries, a timeout, and a redacting repr.

    Construct once per connector call-path. `base_url` is prepended to any path
    that does not already start with `http`, so callers pass `/v3/budgets` and
    get the right regional host without string-building.
    """

    def __init__(
        self,
        base_url: str = "",
        *,
        headers: Optional[Dict[str, str]] = None,
        auth: Optional[Tuple[str, str]] = None,
        timeout: float = _DEFAULT_TIMEOUT,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        backoff: float = _DEFAULT_BACKOFF,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self._session = requests.Session()
        if headers:
            self._session.headers.update({k: v for k, v in headers.items() if v is not None})
        if auth:
            self._session.auth = auth

    # -- header mutation (for token-refresh flows) -----------------------

    def set_header(self, name: str, value: str) -> None:
        self._session.headers[name] = value

    # -- core request with bounded retry ---------------------------------

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        kwargs.setdefault("timeout", self.timeout)

        attempt = 0
        while True:
            resp = self._session.request(method, url, **kwargs)
            if resp.status_code in _RETRY_STATUS and attempt < self.max_retries:
                time.sleep(self._retry_wait(resp, attempt))
                attempt += 1
                continue
            return resp

    def _retry_wait(self, resp: requests.Response, attempt: int) -> float:
        """Seconds to wait before the next attempt.

        A 429 (or any response) may carry `Retry-After` as an integer count of
        seconds or as an HTTP-date; both are honoured. Otherwise exponential
        backoff, capped so a misbehaving server cannot park a Streamlit worker.
        """
        header = resp.headers.get("Retry-After")
        if header:
            try:
                return min(float(header), _MAX_BACKOFF)
            except ValueError:
                try:
                    when = parsedate_to_datetime(header)
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    return max(0.0, min((when - datetime.now(timezone.utc)).total_seconds(), _MAX_BACKOFF))
                except (TypeError, ValueError):
                    pass
        return min(self.backoff * (2 ** attempt), _MAX_BACKOFF)

    # -- verb + json helpers ---------------------------------------------

    def get(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("DELETE", path, **kwargs)

    def get_json(self, path: str, **kwargs: Any) -> Any:
        resp = self.get(path, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def post_json(self, path: str, **kwargs: Any) -> Any:
        resp = self.post(path, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self._session.close()

    def __repr__(self) -> str:  # never print headers or auth
        return f"<VendorSession base_url={self.base_url!r} timeout={self.timeout}>"


class VendorConnector(Connector):
    """Common scaffolding for every commercial-tool connector.

    Gives subclasses a uniform `test_connection` shape via `_probe` -- which
    times the call, returns a not-configured result when credentials are absent
    (so no network happens), and turns any raised exception into `ok=False`
    with the credential scrubbed out of the message. Subclasses implement a tiny
    `call()` that performs one cheap authenticated request and returns a detail
    dict.
    """

    # -- secret hygiene ---------------------------------------------------

    def _scrub(self, text: Any) -> str:
        """Redact any known secret value that wandered into a string."""
        out = str(text)
        for value in self._secrets.values():
            if value:
                out = out.replace(str(value), "***")
        return out

    # -- shared results ---------------------------------------------------

    def _unconfigured_result(self) -> ConnectionResult:
        missing = self.missing_secrets()
        return ConnectionResult(
            ok=False,
            message=f"Not configured. Missing secret(s): {', '.join(missing)}",
            detail={"missing": missing},
        )

    def _probe(self, call: Callable[[], Optional[Dict[str, Any]]]) -> ConnectionResult:
        """Run `call` as a connection test. Never raises."""
        if not self.configured:
            return self._unconfigured_result()
        start = time.perf_counter()
        try:
            detail = call() or {}
            return ConnectionResult(
                ok=True,
                message=f"Connected to {self.spec.display_name}.",
                detail=detail,
                latency_ms=(time.perf_counter() - start) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001 -- test_connection must not raise
            return ConnectionResult(
                ok=False,
                message=self._scrub(f"{type(exc).__name__}: {exc}"),
                latency_ms=(time.perf_counter() - start) * 1000.0,
            )

    # -- shared empties ---------------------------------------------------

    def _empty_costs(self):
        """A conformant, empty FOCUS frame -- the safe answer when a connector
        is unconfigured or a fetch fails. Callers can always concatenate it."""
        return focus.empty_frame()


def first(mapping: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Return the first present, non-null value among `keys` in `mapping`.

    Vendor payloads spell the same concept a dozen ways (`amount`, `cost`,
    `total_cost`, `costAmortized`); this keeps the mapping code readable.
    """
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return default


def chunk_date_ranges(start, end, max_days: int) -> Iterable[Tuple[Any, Any]]:
    """Yield [a, b) sub-ranges no longer than `max_days`.

    Finout caps a V2 query at 60 days and V1 at 90; CloudZero and others reward
    smaller windows. Connectors that need chunking share this.
    """
    from datetime import timedelta

    span = timedelta(days=max_days)
    cursor = start
    while cursor < end:
        stop = min(cursor + span, end)
        yield cursor, stop
        cursor = stop
