"""Shared Isomer helpers (resources/_isomer.py): RSC payload parsing + listing fetch."""

from __future__ import annotations

import httpx
import pytest
from conftest import load_module, read_fixture

isomer = load_module("_isomer")

# Older payload shape: flat category string directly after date.
LEGACY_PAYLOAD = (
    '<script>self.__next_f.push([1,"{\\"items\\":['
    '{\\"id\\":\\"/news/a-story\\",\\"date\\":\\"$D2026-03-05T00:00:00.000Z\\",'
    '\\"category\\":\\"Media releases\\",\\"title\\":\\"A story\\"},'
    '{\\"id\\":\\"/other/skip-me\\",\\"date\\":\\"$D2026-03-06T00:00:00.000Z\\",'
    '\\"category\\":\\"x\\",\\"title\\":\\"Other\\"}'
    ']}"])</script>'
)


def test_normalize_url_strips_trailing_slash() -> None:
    assert isomer.normalize_url("https://x.gov.sg/a/") == "https://x.gov.sg/a"
    assert isomer.normalize_url("https://x.gov.sg/a") == "https://x.gov.sg/a"


def test_listing_items_legacy_shape_and_prefix() -> None:
    items = isomer.parse_isomer_listing_items(LEGACY_PAYLOAD, "/news/")
    assert items == [
        {
            "path": "/news/a-story",
            "date": "2026-03-05",
            "category": "Media releases",
            "title": "A story",
        }
    ]


def test_listing_dates_from_real_ipos_payload() -> None:
    dates = isomer.parse_isomer_listing_dates(read_fixture("ipos_listing.html"))
    assert dates == {
        "/news/news-collection/singapores-strong-innovation-foundations-recognised-globally": "2026-09-29",
        "/news/news-collection/new-global-network-to-advance-intangible-asset-valuation-practices": "2026-08-27",
        "/news/news-collection/is-it-ip-as-usual--singapore-ip-week-tackles-the-big-questions-facing-ip-in-the-age-of-ai-": "2026-08-26",
        "/news/news-collection/sg-patents-fast-and-sg-trade-marks-fast-to-resume-acceptance-of-new-requests": "2026-08-14",
    }


def test_listing_dates_from_real_agc_payload() -> None:
    dates = isomer.parse_isomer_listing_dates(read_fixture("agc_listing.html"))
    assert len(dates) == 4
    assert (
        dates[
            "/newsroom/16th-china-asean-prosecutors-general-conference-2026-speech-by-attorney-general--mr-lucien-wong-s-c-"
        ]
        == "2026-10-22"
    )
    assert all(p.startswith("/newsroom/") for p in dates)
    assert all(len(d) == 10 and d.startswith("2026-") for d in dates.values())


def test_listing_items_from_real_ipos_payload() -> None:
    # Live payloads put 'plaintextTags' between 'date' and 'title' and carry the
    # category in a tag group; the parser must not depend on field order.
    items = isomer.parse_isomer_listing_items(
        read_fixture("ipos_listing.html"), "/news/news-collection/"
    )
    assert len(items) == 4
    assert items[0] == {
        "path": "/news/news-collection/singapores-strong-innovation-foundations-recognised-globally",
        "date": "2026-09-29",
        "category": "Media releases",
        "title": "Singapore’s strong innovation foundations recognised globally  ",
    }


def test_listing_items_from_real_agc_payload() -> None:
    items = isomer.parse_isomer_listing_items(read_fixture("agc_listing.html"), "/newsroom/")
    assert len(items) == 4
    first = items[0]
    assert first["path"].startswith("/newsroom/16th-china-asean-prosecutors-general-conference")
    assert first["date"] == "2026-10-22"
    assert first["category"] == "Speeches"
    assert first["title"].startswith("16th China-ASEAN Prosecutors General Conference 2026")


@pytest.mark.parametrize(
    "fixture,prefix",
    [("ipos_listing.html", "/news/news-collection/"), ("agc_listing.html", "/newsroom/")],
)
def test_listing_items_agree_with_listing_dates(fixture: str, prefix: str) -> None:
    html = read_fixture(fixture)
    items = isomer.parse_isomer_listing_items(html, prefix)
    dates = {p: d for p, d in isomer.parse_isomer_listing_dates(html).items() if p.startswith(prefix)}
    assert {i["path"]: i["date"] for i in items} == dates


def test_listing_items_category_from_tag_group_and_dedup() -> None:
    # ACRA-style item: empty plaintextTags, several tag groups, only "Category" counts.
    item = (
        '{\\"id\\":\\"/n/a\\",\\"date\\":\\"$D2026-11-06T00:00:00.000Z\\",'
        '\\"plaintextTags\\":[],\\"title\\":\\"A\\",\\"tags\\":['
        '{\\"id\\":\\"x\\",\\"category\\":\\"Audience\\",\\"selected\\":[\\"Public\\"]},'
        '{\\"id\\":\\"y\\",\\"category\\":\\"Category\\",\\"selected\\":[\\"Press release\\"]}]}'
    )
    html = '<script>self.__next_f.push([1,"{\\"items\\":[%s,%s]}"])</script>' % (item, item)
    assert isomer.parse_isomer_listing_items(html, "/n/") == [
        {"path": "/n/a", "date": "2026-11-06", "category": "Press release", "title": "A"}
    ]


def test_fetch_listing_dates_builds_absolute_urls_and_filters_prefix(mock_client) -> None:
    client = mock_client(lambda r: httpx.Response(200, text=read_fixture("agc_listing.html")))
    result = isomer.fetch_isomer_listing_dates(
        client, "https://www.agc.gov.sg/newsroom/", "/newsroom/"
    )
    assert len(result) == 4
    assert all(u.startswith("https://www.agc.gov.sg/newsroom/") for u in result)
    other = isomer.fetch_isomer_listing_dates(
        client, "https://www.agc.gov.sg/newsroom/", "/nothing/"
    )
    assert other == {}


def test_fetch_listing_dates_swallows_http_errors(mock_client) -> None:
    client = mock_client(lambda r: httpx.Response(503))
    assert isomer.fetch_isomer_listing_dates(client, "https://x.gov.sg/n/", "/n/") == {}


def test_fetch_listing_dates_swallows_transport_errors(mock_client) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    client = mock_client(boom)
    assert isomer.fetch_isomer_listing_dates(client, "https://x.gov.sg/n/", "/n/") == {}
