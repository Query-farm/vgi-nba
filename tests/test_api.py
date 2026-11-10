"""Tests for the NBA HTTP boundary: headers, caching, retries, and failure modes."""

from __future__ import annotations

import httpx
import pytest

from vgi_nba import __version__, api


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_sends_the_header_set_akamai_requires(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NBA_STATS_BASE_URL", "https://example.test/stats/")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/stats/leaguestandingsv3"
        assert request.url.params["LeagueID"] == "00"
        assert request.headers["User-Agent"] == f"vgi-nba/{__version__} (Query-farm/vgi-nba)"
        assert request.headers["Referer"] == "https://www.nba.com/"
        assert request.headers["Accept-Language"]
        assert "gzip" in request.headers["Accept-Encoding"]
        assert request.headers["Connection"] == "keep-alive"
        return httpx.Response(200, json={"resultSets": []}, headers={"Cache-Control": "max-age=900"})

    response = api.fetch_stats("leaguestandingsv3", {"LeagueID": "00"}, client=_client(handler))
    assert response.payload == {"resultSets": []}
    assert response.ttl == 900


def test_user_agent_has_no_url() -> None:
    # Akamai blocks any User-Agent that carries a URL or domain.
    assert "http" not in api.USER_AGENT
    assert ".com" not in api.USER_AGENT


def test_ttl_floor_applies_when_origin_max_age_is_short() -> None:
    client = _client(lambda request: httpx.Response(200, json={}, headers={"Cache-Control": "max-age=1"}))
    assert api.fetch_stats("x", {}, ttl_floor=300, client=client).ttl == 300


def test_responses_are_cached_in_process() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"n": calls})

    client = _client(handler)
    first = api.fetch_stats("franchisehistory", {"LeagueID": "00"}, client=client)
    second = api.fetch_stats("franchisehistory", {"LeagueID": "00"}, client=client)
    other = api.fetch_stats("franchisehistory", {"LeagueID": "10"}, client=client)
    assert first.payload == second.payload == {"n": 1}
    assert other.payload == {"n": 2}
    assert calls == 2


def test_cache_expires_and_evicts(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = api._Cache(max_entries=2)
    cache.store("a", api.ApiResponse({"a": 1}, 60))
    cache.store("b", api.ApiResponse({"b": 1}, 60))
    cache.store("c", api.ApiResponse({"c": 1}, 60))
    assert cache.get("a") is None
    assert cache.get("c") is not None
    now = api.time.monotonic()
    monkeypatch.setattr(api.time, "monotonic", lambda: now + 120)
    assert cache.get("c") is None


def test_retryable_response_honors_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(api.time, "sleep", sleeps.append)
    assert api.fetch_stats("x", {}, client=_client(handler)).payload == {"ok": True}
    assert attempts == 2
    assert sleeps == [2.0]


def test_server_errors_are_retried_then_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api.time, "sleep", lambda seconds: None)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(500, text="boom")

    with pytest.raises(api.NbaApiError, match="HTTP 500: boom") as error:
        api.fetch_stats("x", {}, client=_client(handler))
    assert attempts == api.MAX_ATTEMPTS
    assert error.value.status == 500


def test_connection_errors_are_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api.time, "sleep", lambda seconds: None)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("reset", request=request)
        return httpx.Response(200, json={})

    assert api.fetch_stats("x", {}, client=_client(handler)).payload == {}
    assert attempts == 2


def test_a_stalled_request_fails_fast_with_a_clear_error() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("stalled", request=request)

    with pytest.raises(api.NbaApiError, match="did not respond within 15 seconds"):
        api.fetch_stats("x", {}, client=_client(handler))
    assert attempts == 1


def test_forbidden_and_html_bodies_are_rejected() -> None:
    forbidden = _client(lambda request: httpx.Response(403, text="<HTML>Access Denied</HTML>"))
    with pytest.raises(api.NbaApiError, match="HTTP 403"):
        api.fetch_cdn("nba", "liveData/x.json", client=forbidden)
    html = _client(lambda request: httpx.Response(200, text="  <!doctype html><p>hi"))
    with pytest.raises(api.NbaApiError, match="HTML page instead of JSON"):
        api.fetch_cdn("wnba", "staticData/y.json", client=html)
    garbage = _client(lambda request: httpx.Response(200, text="not json"))
    with pytest.raises(api.NbaApiError, match="not JSON"):
        api.fetch_stats("z", {}, client=garbage)
    array = _client(lambda request: httpx.Response(200, json=[1, 2]))
    with pytest.raises(api.NbaApiError, match="not a JSON object"):
        api.fetch_stats("z", {}, client=array)
    with pytest.raises(api.NbaApiError, match="not a JSON object"):
        api.fetch_cdn("nba", "a.json", client=array)


def test_paths_and_endpoints_are_validated() -> None:
    with pytest.raises(ValueError, match="invalid Stats API endpoint"):
        api.fetch_stats("../admin", {})
    with pytest.raises(ValueError, match="invalid CDN path"):
        api.fetch_cdn("nba", "../../etc/passwd")
    with pytest.raises(ValueError, match="unknown CDN"):
        api.cdn_base_url("espn")


def test_cdn_base_urls_are_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NBA_CDN_BASE_URL", "http://127.0.0.1:1/nba/")
    monkeypatch.setenv("WNBA_CDN_BASE_URL", "http://127.0.0.1:1/wnba")
    assert api.cdn_base_url("nba") == "http://127.0.0.1:1/nba"
    assert api.cdn_base_url("wnba") == "http://127.0.0.1:1/wnba"
    monkeypatch.delenv("NBA_CDN_BASE_URL")
    assert api.cdn_base_url("nba") == api.DEFAULT_NBA_CDN_BASE_URL


def test_retry_delay_parsing() -> None:
    assert api._retry_delay(httpx.Response(429, headers={"Retry-After": "999"}), 0) == api.MAX_RETRY_DELAY
    dated = httpx.Response(503, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
    assert 0.5 <= api._retry_delay(dated, 0) <= 0.625  # a past date falls back to backoff
    assert 0.5 <= api._retry_delay(httpx.Response(503, headers={"Retry-After": "soon"}), 0) <= 0.625
    assert 0.5 <= api._retry_delay(None, 0) <= 0.625


def test_request_interval_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NBA_REQUEST_INTERVAL", "bogus")
    assert api.request_interval() == api.DEFAULT_REQUEST_INTERVAL
    monkeypatch.setenv("NBA_REQUEST_INTERVAL", "-3")
    assert api.request_interval() == 0.0


def test_pacing_spaces_requests_to_one_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NBA_REQUEST_INTERVAL", "0.5")
    sleeps: list[float] = []
    monkeypatch.setattr(api.time, "sleep", sleeps.append)
    api._last_request.clear()
    api._pace("stats.example")
    api._pace("stats.example")
    api._pace("cdn.example")
    assert len(sleeps) == 1 and 0.4 < sleeps[0] <= 0.5


def test_result_set_shapes() -> None:
    payload = {"resultSets": [{"name": "A", "headers": ["X", "Y"], "rowSet": [[1, 2], "junk"]}]}
    assert api.result_set(payload) == [{"X": 1, "Y": 2}]
    assert api.result_set(payload, "A") == [{"X": 1, "Y": 2}]
    single = {"resultSet": {"name": "B", "headers": ["X"], "rowSet": [[3]]}}
    assert api.result_set(single) == [{"X": 3}]
    assert api.result_set({"resultSets": []}) == []
    with pytest.raises(api.NbaApiError, match="no C result set"):
        api.result_set(payload, "C")
    with pytest.raises(api.NbaApiError, match="no result sets"):
        api.result_set({})
    with pytest.raises(api.NbaApiError, match="malformed"):
        api.result_set({"resultSets": [{"name": "A", "headers": None, "rowSet": []}]})


def test_shared_client_is_reused() -> None:
    first = api._client()
    assert api._client() is first
    assert first.headers["User-Agent"] == api.USER_AGENT
