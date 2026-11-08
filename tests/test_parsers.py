"""Parser tests against saved real pages in tests/fixtures/ (bs4 + lxml + stdlib XML)."""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET

import httpx
import pytest
from conftest import ALL_NEWS_MODULES, load_module, read_fixture_bytes

agc = load_module("agc_news")
ipos = load_module("ipos_news")
judiciary = load_module("judiciary_news")
mlaw = load_module("mlaw_news")


def _page(fixture: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=read_fixture_bytes(fixture))

    return handler


def test_agc_article_parsing(mock_client) -> None:
    art = agc.fetch_article(
        "https://www.agc.gov.sg/newsroom/x/", mock_client(_page("agc_article.html"))
    )
    assert art["title"].startswith("16th China-ASEAN Prosecutors General Conference 2026")
    assert art["published_date"] == "2026-10-22"
    assert len(art["content_text"]) > 5000
    # nav/footer/form chrome is stripped from the extracted body
    assert "\n\n" in art["content_text"]


def test_ipos_article_parsing(mock_client) -> None:
    art = ipos.fetch_article(
        "https://www.ipos.gov.sg/news/x/", mock_client(_page("ipos_article.html"))
    )
    assert art["title"] == "Singapore’s strong innovation foundations recognised globally"
    assert art["published_date"] == "2026-09-29"
    assert len(art["content_text"]) > 2000


def test_judiciary_article_parsing(mock_client) -> None:
    text = judiciary.fetch_article_content(
        "https://www.judiciary.gov.sg/x", mock_client(_page("judiciary_article.html"))
    )
    assert text.startswith("POLARIS 2026 Appreciation keynote")
    assert "The Honourable Justice Goh Yihan" in text
    assert len(text) > 5000


def test_judiciary_listing_parsing(mock_client) -> None:
    client = mock_client(
        lambda r: httpx.Response(200, content=read_fixture_bytes("judiciary_list.json"))
    )
    items = judiciary.fetch_listing_page(client, 0, "2026")
    assert [i["published_date"] for i in items] == ["2026-11-06", "2026-11-04", "2026-11-04"]
    assert items[0]["content_type"] == "Speech"
    assert items[0]["courts"] == "Supreme Court|Singapore International Commercial Court"
    assert items[2]["content_type"] == "Media Release"
    assert items[1]["title"] == "Justice Goh Yihan: Keynote address at POLARIS 2026"
    assert all(i["source_url"].startswith("https://www.judiciary.gov.sg/") for i in items)


def test_judiciary_listing_request_shape(mock_client) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"listPartialView": ""})

    assert judiciary.fetch_listing_page(mock_client(handler), 2, "2026") == []
    req = seen[0]
    assert req.method == "POST"
    body = json.loads(req.content)["model"]
    assert body["CurrentPage"] == 2 and body["SelectedYear"] == "2026"
    assert req.headers["content-type"].startswith("application/json")


def _feed_entries() -> list[tuple[str, str]]:
    ns = "{http://www.w3.org/2005/Atom}"
    root = ET.fromstring(read_fixture_bytes("mlaw_feed.xml"))
    return [
        (e.findtext(f"{ns}title") or "", e.find(f"{ns}content").text or "")
        for e in root.findall(f"{ns}entry")
    ]


def test_mlaw_feed_html_extraction() -> None:
    entries = _feed_entries()
    assert len(entries) == 3
    categories = [mlaw.extract_category_from_content(html) for _, html in entries]
    assert categories == ["parliamentary-speeches", "other", "speeches"]
    for _, html in entries:
        text = mlaw.extract_content_from_feed_html(html)
        assert len(text) > 1000


def test_mlaw_discovery_from_feed(mock_client) -> None:
    client = mock_client(lambda r: httpx.Response(200, content=read_fixture_bytes("mlaw_feed.xml")))
    items = mlaw.discover_news_from_feed(client, set())
    assert len(items) == 3
    assert {i["published_date"] for i in items} == {"2026-11-04"}
    assert all(i["source_url"].startswith("https://www.mlaw.gov.sg/") for i in items)
    known = {items[0]["source_url"]}
    assert len(mlaw.discover_news_from_feed(client, known)) == 2
    # The "<date> Posted in <category>" byline is metadata, not article text.
    for item in items:
        assert "Posted in" not in item["content_text"]
        assert item["content_text"]


def test_mlaw_metadata_line_filter() -> None:
    assert mlaw._is_metadata_line("4 November 2026Posted inSpeeches")
    assert mlaw._is_metadata_line("4 November 2026 Posted in Press Releases")
    assert mlaw._is_metadata_line("Posted in Speeches")
    assert mlaw._is_metadata_line("Last updated on 3 November 2026")
    assert not mlaw._is_metadata_line("The Ministry has posted information in its annual report.")


def test_mlaw_fetch_article_page(mock_client) -> None:
    # Not on the live fetch_data path (discovery uses the feed) but still shipped code.
    art = mlaw.fetch_article("https://www.mlaw.gov.sg/x/", mock_client(_page("mlaw_article.html")))
    assert art["title"].startswith("Speech by Senior Parliamentary Secretary")
    assert art["published_date"] == "2026-11-04"
    assert "Posted in" not in art["content_text"]


def test_agc_sitemap_discovery(mock_client) -> None:
    client = mock_client(
        lambda r: httpx.Response(200, content=read_fixture_bytes("agc_sitemap.xml"))
    )
    urls = agc.discover_urls_from_sitemap(client, set())
    assert len(urls) == 4  # index page and non-newsroom urls excluded
    assert all("/newsroom/" in u and not u.endswith("/newsroom/") for u in urls)
    assert agc.discover_urls_from_sitemap(client, {urls[0]}) == urls[1:]


@pytest.mark.parametrize(
    "url,title,expected",
    [
        ("https://x/newsroom/agc-media-statement-foo/", "t", "media-statement"),
        ("https://x/newsroom/agc-press-release-foo/", "t", "press-release"),
        ("https://x/newsroom/foo/", "Speech by AG", "speech"),
        ("https://x/newsroom/foo/", "Reappointment of Judge", "appointment"),
        ("https://x/newsroom/foo-pleads-guilty/", "t", "prosecution"),
        ("https://x/newsroom/foo/", "Something else", "other"),
    ],
)
def test_agc_infer_category(url: str, title: str, expected: str) -> None:
    assert agc.infer_category(url, title) == expected


@pytest.mark.parametrize("name", ALL_NEWS_MODULES)
def test_make_id_is_sha256_prefix(name: str) -> None:
    mod = load_module(name)
    url = "https://www.example.gov.sg/news/a"
    assert mod.make_id(url) == hashlib.sha256(url.encode()).hexdigest()[:12]


@pytest.mark.parametrize("name", ALL_NEWS_MODULES)
def test_parse_date_string(name: str) -> None:
    mod = load_module(name)
    assert mod.parse_date_string("8 April 2026") == "2026-04-08"
    assert mod.parse_date_string("08 Apr 2026") == "2026-04-08"
    if name != "judiciary_news":  # judiciary only parses the bare listing format
        assert mod.parse_date_string("Published on 3 March 2026.") == "2026-03-03"
    assert mod.parse_date_string("not a date") is None
