"""Regression tests for the 2026-11-21 data-quality fixes.

Covers:
  * mom_news: current-template DivCode extraction (span/<br> bodies inside the
    page-wide ASP.NET form), legacy-template fallback, the empty-extraction
    guard, and www/non-www normalised dedup
  * mlaw_news: empty feed <title> falling back to the page <h1>, and skipping
    the entry when even that fails
"""

from __future__ import annotations

from typing import Mapping

import httpx
import pytest
import tenacity
from conftest import load_module, read_fixture_bytes

mom = load_module("mom_news")
mlaw = load_module("mlaw_news")

DIVCODE_URL = "https://mom.gov.sg/newsroom/press-releases/2026/1111-former-site-manager"
LEGACY_URL = "https://mom.gov.sg/newsroom/press-releases/2026/1030-labour-market"
SITEMAP_URL = "https://www.mom.gov.sg/newsroom.xml"


def _serve(routes: Mapping[str, str | tuple[int, bytes]]):
    """Exact-URL MockTransport router; unmatched URLs 404."""

    def handler(request: httpx.Request) -> httpx.Response:
        key = str(request.url).rstrip("/")
        if key in routes:
            target = routes[key]
            if isinstance(target, tuple):
                return httpx.Response(target[0], content=target[1])
            return httpx.Response(200, content=read_fixture_bytes(target))
        return httpx.Response(404, content=b"not found")

    return handler


# --- mom extraction ----------------------------------------------------------


def test_mom_divcode_template_extraction(mock_client) -> None:
    art = mom.fetch_article(DIVCODE_URL, mock_client(_serve({DIVCODE_URL: "mom_divcode_page.html"})))
    assert art["title"] == (
        "Former Site Manager Convicted and Imprisoned for Demanding Kickbacks from Migrant Workers"
    )
    assert art["published_date"] == "2026-11-11"
    ct = art["content_text"]
    assert ct.startswith("Koh Kim Sia")
    assert "2 Koh faced a total of 24 charges" in ct
    assert "Advisory" in ct
    # chrome must not leak into the stored text
    assert "Share this page" not in ct
    assert "11 November 2026" not in ct
    assert "Foreign manpower" not in ct
    assert "mainform" not in ct


def test_mom_legacy_template_still_works(mock_client) -> None:
    art = mom.fetch_article(LEGACY_URL, mock_client(_serve({LEGACY_URL: "mom_legacy_page.html"})))
    assert art["title"] == "Labour Market Report 3Q 2026"
    assert "Total employment grew" in art["content_text"]
    assert "Unemployment rates remained low" in art["content_text"]
    assert "\n\n" in art["content_text"]


def test_mom_empty_extraction_raises_not_blank(mock_client) -> None:
    """A page with no extractable body must fail the fetch, not store ''."""
    blank = "<html><body><form><p>short</p></form></body></html>"
    with pytest.raises((tenacity.RetryError, ValueError)):
        mom.fetch_article(LEGACY_URL, mock_client(_serve({LEGACY_URL: (200, blank.encode())})))


# --- mom dedup normalisation ---------------------------------------------------


def test_mom_normalize_url() -> None:
    assert mom._normalize_url("https://www.mom.gov.sg/a/") == mom._normalize_url("https://mom.gov.sg/a")


def test_mom_dedup_ignores_www_variants(mock_client) -> None:
    """A sitemap flip between www and bare-host forms must not re-import."""
    existing = {"https://www.mom.gov.sg/newsroom/press-releases/2026/0117-dts-grant"}
    urls = mom.discover_urls_from_sitemap(
        mock_client(_serve({SITEMAP_URL: "mom_sitemap.xml"})), existing
    )
    assert urls == ["https://mom.gov.sg/newsroom/press-releases/2026/0202-new-release"]


# --- mlaw empty-title handling --------------------------------------------------


def test_mlaw_empty_title_falls_back_to_page_h1(mock_client) -> None:
    routes = {
        "https://www.mlaw.gov.sg/feed.xml": "mlaw_feed_emptytitle.xml",
        "https://www.mlaw.gov.sg/entry-with-h1-fallback": "mlaw_page_h1.html",
        # entry-with-dead-link deliberately left unrouted -> 404
    }
    items = mlaw.discover_news_from_feed(
        mock_client(_serve(routes)), set()
    )
    by_url = {i["source_url"]: i for i in items}
    assert by_url["https://www.mlaw.gov.sg/normal-feed-entry/"]["title"] == "Normal Feed Entry Title"
    assert by_url["https://www.mlaw.gov.sg/entry-with-h1-fallback/"]["title"] == "Page Title From H1"
    # untitled entry whose page 404s is skipped, never imported blank
    assert "https://www.mlaw.gov.sg/entry-with-dead-link/" not in by_url
