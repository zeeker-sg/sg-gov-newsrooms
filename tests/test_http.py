"""HTTP-layer behaviour: httpx client + tenacity retries, driven by MockTransport."""

from __future__ import annotations

import httpx
import pytest
import tenacity
import zeeker
from conftest import load_module, read_fixture_bytes

agc = load_module("agc_news")

URL = "https://www.agc.gov.sg/newsroom/x/"
RETRYING = [
    ("agc_news", "fetch_article", "agc_article.html"),
    ("ipos_news", "fetch_article", "ipos_article.html"),
    ("mlaw_news", "fetch_article", "mlaw_article.html"),
    ("judiciary_news", "fetch_article_content", "judiciary_article.html"),
]


def _fast(func):
    """Same retry policy, no real backoff sleeping."""
    return func.retry_with(wait=tenacity.wait_none())


@pytest.mark.parametrize("module,func,fixture", RETRYING)
def test_5xx_then_200_is_retried(mock_client, module: str, func: str, fixture: str) -> None:
    mod = load_module(module)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, content=read_fixture_bytes(fixture))

    result = _fast(getattr(mod, func))(URL, mock_client(handler))
    assert len(calls) == 2
    assert result  # parsed content, not an error


@pytest.mark.parametrize("module,func,fixture", RETRYING)
def test_transport_error_is_retried(mock_client, module: str, func: str, fixture: str) -> None:
    mod = load_module(module)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectTimeout("slow", request=request)
        return httpx.Response(200, content=read_fixture_bytes(fixture))

    assert _fast(getattr(mod, func))(URL, mock_client(handler))
    assert len(calls) == 3


@pytest.mark.parametrize("module,func,fixture", RETRYING)
def test_persistent_failure_raises_retry_error_after_max_attempts(
    mock_client, module: str, func: str, fixture: str
) -> None:
    mod = load_module(module)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500)

    with pytest.raises(tenacity.RetryError) as exc:
        _fast(getattr(mod, func))(URL, mock_client(handler))
    assert len(calls) == mod.MAX_RETRIES
    # the underlying cause is httpx's raise_for_status error
    assert isinstance(exc.value.last_attempt.exception(), httpx.HTTPStatusError)


def test_production_retry_policy_is_configured() -> None:
    """The shipped decorator (not the test override) keeps its attempt cap and backoff."""
    retry = agc.fetch_article.retry
    assert isinstance(retry.stop, tenacity.stop_after_attempt)
    assert retry.stop.max_attempt_number == agc.MAX_RETRIES == 3
    assert isinstance(retry.wait, tenacity.wait_exponential)


def test_redirects_are_followed_by_the_real_client(mock_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old/":
            return httpx.Response(301, headers={"Location": "/newsroom/x/"})
        return httpx.Response(200, content=read_fixture_bytes("agc_article.html"))

    art = _fast(agc.fetch_article)("https://www.agc.gov.sg/old/", mock_client(handler))
    assert art["published_date"] == "2026-10-22"


def test_fetch_data_sends_identifying_user_agent(patch_client, monkeypatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500)

    patch_client(agc, handler)
    with pytest.raises(zeeker.Skip):
        agc.fetch_data(None)
    assert seen and all(r.headers["user-agent"].startswith("ZeekerBot/") for r in seen)


def test_discovery_failure_raises_zeeker_skip_blocked(patch_client) -> None:
    patch_client(agc, lambda r: httpx.Response(500))
    with pytest.raises(zeeker.Skip) as exc:
        agc.fetch_data(None)
    assert exc.value.kind == "blocked"


@pytest.mark.parametrize("module", ["mlaw_news", "judiciary_news"])
def test_discovery_failure_is_skip_for_other_resources(patch_client, module: str) -> None:
    mod = load_module(module)
    patch_client(mod, lambda r: httpx.Response(502))
    with pytest.raises(zeeker.Skip) as exc:
        mod.fetch_data(None)
    assert exc.value.kind == "blocked"


def test_circuit_breaker_trips_after_consecutive_article_failures(
    patch_client, monkeypatch
) -> None:
    """Sitemap works, every article 500s: breaker aborts and the run is skipped (no rows)."""
    patch_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        patch_calls.append(request.url.path)
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, content=read_fixture_bytes("agc_sitemap.xml"))
        return httpx.Response(500)

    patch_client(agc, handler)
    # keep retry backoff instant so the test is quick
    monkeypatch.setattr(agc, "fetch_article", _fast(agc.fetch_article))
    monkeypatch.setattr(agc, "MAX_CONSECUTIVE_FAILURES", 2)
    with pytest.raises(zeeker.Skip) as exc:
        agc.fetch_data(None)
    assert exc.value.kind == "blocked"
    article_calls = [p for p in patch_calls if p.startswith("/newsroom/") and p != "/newsroom/"]
    assert len(article_calls) == 2 * agc.MAX_RETRIES  # 2 failing articles x 3 attempts each
