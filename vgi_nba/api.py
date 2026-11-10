"""Small, read-only client for the NBA Stats API and the NBA/WNBA content CDNs.

Every outbound request in the package goes through :func:`_get`. The client
sends the header set Akamai requires (an honest, URL-free User-Agent, a
``Referer`` of https://www.nba.com/, ``Accept``, ``Accept-Language``,
``Accept-Encoding``, and a keep-alive connection). When a request is rejected,
stats.nba.com does not answer with 4xx: it holds the socket open. Short read
timeouts turn that hang into a clear error instead of a stalled query.
"""

from __future__ import annotations

import logging
import os
import random
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from vgi_nba import __version__

DEFAULT_STATS_BASE_URL = "https://stats.nba.com/stats"
DEFAULT_NBA_CDN_BASE_URL = "https://cdn.nba.com/static/json"
DEFAULT_WNBA_CDN_BASE_URL = "https://cdn.wnba.com/static/json"

# Akamai classifies any User-Agent containing a URL or domain as a bot and
# blocks it (403 on the CDN, a silent hang on stats.nba.com), so the project is
# identified by its GitHub slug rather than a "+https://..." contact URL.
USER_AGENT = f"vgi-nba/{__version__} (Query-farm/vgi-nba)"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.nba.com/",
    "Connection": "keep-alive",
}

READ_TIMEOUT = 15.0
CONNECT_TIMEOUT = 5.0
QUERY_TIMEOUT = 60.0
MAX_ATTEMPTS = 3
MAX_RETRY_DELAY = 20.0
MAX_CONCURRENT_REQUESTS = 2
# Polite pacing: never start two requests to the same host closer together.
DEFAULT_REQUEST_INTERVAL = 0.6
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

# In-process response cache. Entries live for the longer of the origin's
# max-age and the floor chosen per call, so repeated SQL over the same data
# never re-fetches inside that window.
CACHE_MAX_ENTRIES = 96
TTL_LIVE = 30
TTL_STATS = 300
TTL_STATIC = 6 * 3600

logger = logging.getLogger(__name__)

_MAX_AGE = re.compile(r"(?:^|[,\s])max-age=(\d+)", re.IGNORECASE)


class NbaApiError(RuntimeError):
    """A failed or unusable response from the NBA Stats API or CDN."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class ApiResponse:
    """Decoded JSON payload plus how long clients may cache it, in seconds."""

    payload: Any
    ttl: int


def stats_base_url() -> str:
    """Return the Stats API root, optionally overridden for hermetic tests."""
    return os.environ.get("NBA_STATS_BASE_URL", DEFAULT_STATS_BASE_URL).rstrip("/")


def cdn_base_url(cdn: str) -> str:
    """Return the CDN root for ``nba`` or ``wnba``, optionally overridden for tests."""
    if cdn == "nba":
        return os.environ.get("NBA_CDN_BASE_URL", DEFAULT_NBA_CDN_BASE_URL).rstrip("/")
    if cdn == "wnba":
        return os.environ.get("WNBA_CDN_BASE_URL", DEFAULT_WNBA_CDN_BASE_URL).rstrip("/")
    raise ValueError(f"unknown CDN {cdn!r}")


class _Cache:
    """A small thread-safe LRU of decoded responses with absolute expiry."""

    def __init__(self, max_entries: int) -> None:
        self._entries: OrderedDict[str, tuple[float, ApiResponse]] = OrderedDict()
        self._lock = threading.Lock()
        self._max_entries = max_entries

    def get(self, key: str) -> ApiResponse | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires, response = entry
            remaining = expires - time.monotonic()
            if remaining <= 0:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return ApiResponse(response.payload, max(1, int(remaining)))

    def store(self, key: str, response: ApiResponse) -> None:
        with self._lock:
            self._entries[key] = (time.monotonic() + response.ttl, response)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_cache = _Cache(CACHE_MAX_ENTRIES)
_request_slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
_pace_lock = threading.Lock()
_last_request: dict[str, float] = {}
_client_lock = threading.Lock()
_shared_client: httpx.Client | None = None


def clear_cache() -> None:
    """Drop every cached response (used by tests)."""
    _cache.clear()


def _client() -> httpx.Client:
    """Return the process-wide pooled client, creating it on first use."""
    global _shared_client
    with _client_lock:
        if _shared_client is None:
            _shared_client = httpx.Client(
                headers=HEADERS,
                timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
                follow_redirects=True,
            )
        return _shared_client


def request_interval() -> float:
    """Return the minimum spacing between requests to one host, in seconds."""
    try:
        return max(0.0, float(os.environ.get("NBA_REQUEST_INTERVAL", DEFAULT_REQUEST_INTERVAL)))
    except ValueError:
        return DEFAULT_REQUEST_INTERVAL


def _pace(host: str) -> None:
    """Sleep so requests to one host start at least ``request_interval()`` apart."""
    with _pace_lock:
        now = time.monotonic()
        wait = _last_request.get(host, 0.0) + request_interval() - now
        _last_request[host] = now + max(0.0, wait)
    if wait > 0:
        time.sleep(wait)


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Return a bounded Retry-After delay or exponential backoff with jitter."""
    if response is not None:
        raw = response.headers.get("retry-after")
        if raw:
            try:
                delay = float(raw)
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(raw)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=UTC)
                    delay = (retry_at - datetime.now(UTC)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    delay = -1.0
            if delay >= 0:
                return min(delay, MAX_RETRY_DELAY)
    base = min(0.5 * 2**attempt, MAX_RETRY_DELAY)
    return min(base + random.uniform(0, base * 0.25), MAX_RETRY_DELAY)


def _max_age(response: httpx.Response) -> int:
    match = _MAX_AGE.search(response.headers.get("cache-control", ""))
    return int(match.group(1)) if match else 0


def _cache_key(url: str, params: Mapping[str, str | int]) -> str:
    return url + "?" + "&".join(f"{key}={params[key]}" for key in sorted(params))


def _get(
    url: str,
    params: Mapping[str, str | int],
    *,
    ttl_floor: int,
    client: httpx.Client | None = None,
) -> ApiResponse:
    """Issue one cached, paced, retried GET and decode its JSON body."""
    key = _cache_key(url, params)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    http = client or _client()
    host = urlsplit(url).netloc
    deadline = time.monotonic() + QUERY_TIMEOUT
    response: httpx.Response | None = None
    for attempt in range(MAX_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"{host} query exceeded {QUERY_TIMEOUT:g} seconds")
        if not _request_slots.acquire(timeout=remaining):
            raise TimeoutError(f"{host} query exceeded {QUERY_TIMEOUT:g} seconds")
        started = time.monotonic()
        try:
            _pace(host)
            response = http.get(
                url,
                params=params,
                headers=HEADERS,
                timeout=httpx.Timeout(
                    min(READ_TIMEOUT, max(0.1, deadline - time.monotonic())),
                    connect=CONNECT_TIMEOUT,
                ),
            )
        except httpx.TimeoutException as error:
            # Akamai answers a rejected or blocked client by holding the socket
            # open, so a timeout is not transient: retrying only multiplies the hang.
            raise NbaApiError(
                f"{host} did not respond within {READ_TIMEOUT:g} seconds. The NBA's Akamai edge "
                "silently stalls requests from blocked networks (many cloud and CI address "
                "ranges) and requests whose headers it rejects."
            ) from error
        except httpx.TransportError:
            logger.warning("nba_api_transport_error host=%s attempt=%d", host, attempt + 1)
            if attempt + 1 == MAX_ATTEMPTS:
                raise
            time.sleep(min(_retry_delay(None, attempt), max(0.0, deadline - time.monotonic())))
            continue
        finally:
            _request_slots.release()
        logger.debug(
            "nba_api_response host=%s path=%s status=%d attempt=%d duration_ms=%d",
            host,
            urlsplit(url).path,
            response.status_code,
            attempt + 1,
            round((time.monotonic() - started) * 1_000),
        )
        if response.status_code not in RETRYABLE_STATUSES or attempt + 1 == MAX_ATTEMPTS:
            break
        delay = min(_retry_delay(response, attempt), max(0.0, deadline - time.monotonic()))
        logger.warning(
            "nba_api_retry host=%s status=%d attempt=%d delay_seconds=%.3f",
            host,
            response.status_code,
            attempt + 1,
            delay,
        )
        time.sleep(delay)

    assert response is not None
    if response.status_code == 403:
        raise NbaApiError(f"{host} refused the request (HTTP 403, Akamai access denied)", 403)
    if not response.is_success:
        raise NbaApiError(
            f"{host} returned HTTP {response.status_code}: {response.text[:300]}", response.status_code
        )
    body = response.content.lstrip()
    if body[:1] == b"<":
        raise NbaApiError(f"{host} returned an HTML page instead of JSON; the request was rejected")
    try:
        payload = response.json()
    except ValueError as error:
        raise NbaApiError(f"{host} returned a body that is not JSON") from error
    result = ApiResponse(payload, max(ttl_floor, _max_age(response)))
    _cache.store(key, result)
    return result


def fetch_stats(
    endpoint: str,
    params: Mapping[str, str | int],
    *,
    ttl_floor: int = TTL_STATS,
    client: httpx.Client | None = None,
) -> ApiResponse:
    """Fetch one stats.nba.com endpoint, such as ``leaguestandingsv3``."""
    if not re.fullmatch(r"[a-z0-9]+", endpoint):
        raise ValueError(f"invalid Stats API endpoint {endpoint!r}")
    response = _get(f"{stats_base_url()}/{endpoint}", params, ttl_floor=ttl_floor, client=client)
    if not isinstance(response.payload, dict):
        raise NbaApiError(f"Stats API {endpoint} response is not a JSON object")
    return response


def fetch_cdn(
    cdn: str,
    path: str,
    *,
    ttl_floor: int = TTL_LIVE,
    client: httpx.Client | None = None,
) -> ApiResponse:
    """Fetch one static JSON document from the NBA or WNBA CDN."""
    if not re.fullmatch(r"[A-Za-z0-9_/]+\.json", path):
        raise ValueError(f"invalid CDN path {path!r}")
    response = _get(f"{cdn_base_url(cdn)}/{path}", {}, ttl_floor=ttl_floor, client=client)
    if not isinstance(response.payload, dict):
        raise NbaApiError(f"CDN {path} response is not a JSON object")
    return response


def result_set(payload: Mapping[str, Any], name: str | None = None) -> list[dict[str, Any]]:
    """Return one Stats API result set as dictionaries keyed by its headers.

    Stats API responses carry ``resultSets`` (a list) or ``resultSet`` (one
    object), each with ``headers`` and a ``rowSet`` of positional rows. With no
    ``name`` the first result set is returned.
    """
    sets = payload.get("resultSets", payload.get("resultSet"))
    if isinstance(sets, Mapping):
        sets = [sets]
    if not isinstance(sets, list):
        raise NbaApiError("Stats API response has no result sets")
    for value in sets:
        if not isinstance(value, Mapping) or (name is not None and value.get("name") != name):
            continue
        headers = value.get("headers")
        rows = value.get("rowSet")
        if not isinstance(headers, list) or not isinstance(rows, list):
            raise NbaApiError("Stats API result set is malformed")
        return [dict(zip(headers, row, strict=False)) for row in rows if isinstance(row, list)]
    if name is None:
        return []
    raise NbaApiError(f"Stats API response has no {name} result set")
