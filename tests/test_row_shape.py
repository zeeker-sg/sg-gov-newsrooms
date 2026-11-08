"""fetch_data() end to end on fixtures: row shape vs the zeeker.toml schema contract."""

from __future__ import annotations

import hashlib
import json
import tomllib
from datetime import date, datetime

import httpx
import pytest
import sqlite_utils
import tenacity
from conftest import PROJECT_ROOT, load_module, read_fixture, read_fixture_bytes

TOML = tomllib.loads((PROJECT_ROOT / "zeeker.toml").read_text("utf-8"))["resource"]

ARTICLE_FIXTURE = {
    "agc_news": "agc_article.html",
    "ipos_news": "ipos_article.html",
}


def _ipos_listing_in_expected_format() -> str:
    """Listing in the payload shape parse_isomer_listing_items matches (see test_isomer)."""
    items = ",".join(
        '{\\"id\\":\\"/news/news-collection/story-%d\\",\\"date\\":\\"$D2026-0%d-10T00:00:00.000Z\\",'
        '\\"category\\":\\"Media releases\\",\\"title\\":\\"Story %d\\"}' % (n, n, n)
        for n in (1, 2)
    )
    items += (
        ',{\\"id\\":\\"/news/news-collection/old-story\\",\\"date\\":\\"$D2025-12-31T00:00:00.000Z\\",'
        '\\"category\\":\\"Media releases\\",\\"title\\":\\"Old\\"}'
    )
    return f'<script>self.__next_f.push([1,"{{\\"items\\":[{items}]}}"])</script>'


def _router(name: str):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if name == "mlaw_news":
            return httpx.Response(200, content=read_fixture_bytes("mlaw_feed.xml"))
        if name == "judiciary_news":
            if request.method == "POST":
                page = json.loads(request.content)["model"]["CurrentPage"]
                year = json.loads(request.content)["model"]["SelectedYear"]
                if page == 0 and year == "2026":
                    return httpx.Response(200, content=read_fixture_bytes("judiciary_list.json"))
                return httpx.Response(200, json={"listPartialView": ""})
            return httpx.Response(200, content=read_fixture_bytes("judiciary_article.html"))
        if name == "agc_news":
            if path == "/sitemap.xml":
                return httpx.Response(200, content=read_fixture_bytes("agc_sitemap.xml"))
            if path == "/newsroom/":
                return httpx.Response(200, text=read_fixture("agc_listing.html"))
            return httpx.Response(200, content=read_fixture_bytes("agc_article.html"))
        if name == "ipos_news":
            if path == "/news/news-collection/":
                return httpx.Response(200, text=_ipos_listing_in_expected_format())
            return httpx.Response(200, content=read_fixture_bytes("ipos_article.html"))
        raise AssertionError(name)

    return handler


@pytest.mark.parametrize("name", ["mlaw_news", "judiciary_news", "agc_news", "ipos_news"])
def test_rows_match_zeeker_toml_schema(patch_client, name: str) -> None:
    mod = load_module(name)
    patch_client(mod, _router(name))

    rows = mod.fetch_data(None)
    assert rows, f"{name} produced no rows from fixtures"

    expected_columns = set(TOML[name]["columns"])
    seen_ids = set()
    for row in rows:
        assert set(row) == expected_columns
        for key, value in row.items():
            if key == "published_date":
                assert value is None or isinstance(value, str)
            else:
                assert isinstance(value, str), f"{name}.{key} is {type(value).__name__}"
        assert row["source_url"].startswith("https://")
        assert row["id"] == hashlib.sha256(row["source_url"].encode()).hexdigest()[:12]
        assert row["title"]
        assert row["summary"] == ""  # LLM_BASE_URL unset in tests
        if row["published_date"]:
            assert date.fromisoformat(row["published_date"]) >= mod.START_DATE
        assert datetime.fromisoformat(row["created_at"]).tzinfo is not None
        seen_ids.add(row["id"])
    assert len(seen_ids) == len(rows)  # primary key unique


def test_ipos_start_date_filter_drops_old_items(patch_client) -> None:
    mod = load_module("ipos_news")
    patch_client(mod, _router("ipos_news"))
    rows = mod.fetch_data(None)
    assert len(rows) == 2
    assert not any("old-story" in r["source_url"] for r in rows)


def test_judiciary_row_values(patch_client) -> None:
    mod = load_module("judiciary_news")
    patch_client(mod, _router("judiciary_news"))
    rows = mod.fetch_data(None)
    assert len(rows) == 3
    first = rows[0]
    assert first["content_type"] == "Speech"
    assert first["courts"] == "Supreme Court|Singapore International Commercial Court"
    assert first["published_date"] == "2026-11-06"
    assert first["content_text"].startswith("POLARIS 2026")


def test_incremental_run_skips_rows_already_in_table(patch_client) -> None:
    """existing_table is zeeker's sqlite-utils Table: seen URLs are not re-emitted."""
    mod = load_module("mlaw_news")
    patch_client(mod, _router("mlaw_news"))
    first = mod.fetch_data(None)

    db = sqlite_utils.Database(memory=True)
    db["mlaw_news"].insert_all(first[:1], pk="id")
    rows = mod.fetch_data(db["mlaw_news"])
    assert len(rows) == len(first) - 1
    assert first[0]["source_url"] not in {r["source_url"] for r in rows}


@pytest.mark.parametrize("name", ["mlaw_news", "judiciary_news", "agc_news"])
def test_rows_insert_into_sqlite_with_zeeker_columns(patch_client, name: str) -> None:
    mod = load_module(name)
    patch_client(mod, _router(name))
    rows = mod.transform_data(mod.fetch_data(None))

    db = sqlite_utils.Database(memory=True)
    db[name].insert_all(rows, pk="id")
    assert db[name].count == len(rows)
    assert {c.name for c in db[name].columns} == set(TOML[name]["columns"])
    db[name].enable_fts(["title", "summary"])  # fts_fields configured in zeeker.toml


@pytest.mark.parametrize("name", ["mlaw_news", "judiciary_news", "agc_news", "ipos_news"])
def test_zeeker_toml_sections_cover_resource(name: str) -> None:
    cfg = TOML[name]
    assert cfg["label_column"] in cfg["columns"]
    assert cfg["sort_desc"] in cfg["columns"]
    assert all(f in cfg["columns"] for f in cfg["fts_fields"])
    assert all(f in cfg["columns"] for f in cfg["facets"])


def test_judiciary_skips_item_when_detail_fetch_fails(patch_client, monkeypatch) -> None:
    # A failed detail fetch must not produce an empty-content row: discovery skips
    # URLs already in the table, so a stored empty row would never be retried.
    mod = load_module("judiciary_news")
    # Same retry policy, no real backoff sleeping.
    monkeypatch.setattr(
        mod,
        "fetch_article_content",
        mod.fetch_article_content.retry_with(wait=tenacity.wait_none()),
    )
    base = _router("judiciary_news")
    all_urls = [r["source_url"] for r in mod_rows(patch_client, mod, base)]
    broken = all_urls[0]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and str(request.url).rstrip("/") == broken.rstrip("/"):
            return httpx.Response(404)
        return base(request)

    rows = mod_rows(patch_client, mod, handler)
    assert [r["source_url"] for r in rows] == all_urls[1:]
    assert all(r["content_text"] for r in rows)
    assert mod.__zeeker_report__ == {"failed": 1}


def mod_rows(patch_client, mod, handler):
    patch_client(mod, handler)
    return mod.fetch_data(None)
