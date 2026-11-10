"""Tests for production entry-point policy."""

from __future__ import annotations

import sys

import pytest

from vgi_nba import worker


def test_http_requires_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VGI_SIGNING_KEY", raising=False)

    with pytest.raises(SystemExit, match="VGI_SIGNING_KEY is required"):
        worker.main_http()


def test_http_requires_authentication_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VGI_SIGNING_KEY", "test-signing-key")
    for name in ("VGI_BEARER_TOKENS", "VGI_JWT_ISSUER", "VGI_PROXY_PROOF_MODE", "VGI_ALLOW_ANONYMOUS"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(SystemExit, match="HTTP authentication is required"):
        worker.main_http()


def test_http_applies_safe_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VGI_SIGNING_KEY", "test-signing-key")
    monkeypatch.setenv("VGI_BEARER_TOKENS", "token=ci")
    monkeypatch.setattr(sys, "argv", ["vgi-nba-http", "--port", "8000"])
    called = False

    def fake_main() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(worker.NbaWorker, "main", fake_main)
    worker.main_http()

    assert called
    assert sys.argv == [
        "vgi-nba-http",
        "--cors-origins",
        "",
        "--no-describe",
        "--http",
        "--port",
        "8000",
    ]


def test_http_allows_explicit_anonymous(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VGI_SIGNING_KEY", "test-signing-key")
    for name in ("VGI_BEARER_TOKENS", "VGI_JWT_ISSUER", "VGI_PROXY_PROOF_MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VGI_ALLOW_ANONYMOUS", "1")
    monkeypatch.setattr(sys, "argv", ["vgi-nba-http", "--describe", "--cors-origins=https://x.test"])
    monkeypatch.setattr(worker.NbaWorker, "main", lambda: None)
    worker.main_http()
    assert sys.argv == ["vgi-nba-http", "--http", "--describe", "--cors-origins=https://x.test"]


def test_stdio_main_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []
    monkeypatch.setattr(worker.NbaWorker, "main", lambda: called.append(True))
    worker.main()
    assert called == [True]
