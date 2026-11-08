"""scripts/backfill_content_2026_11.py: in-place fixes for rows stored by the old extractors."""

from __future__ import annotations

import importlib.util
import sqlite3

import httpx
import pytest
import tenacity
from conftest import PROJECT_ROOT, read_fixture_bytes

_spec = importlib.util.spec_from_file_location(
    "backfill_content_2026_11", PROJECT_ROOT / "scripts" / "backfill_content_2026_11.py"
)
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("CREATE TABLE mlaw_news (id TEXT PRIMARY KEY, content_text TEXT)")
    conn.execute(
        "CREATE TABLE judiciary_news (id TEXT PRIMARY KEY, source_url TEXT, title TEXT, "
        "published_date TEXT, content_text TEXT, summary TEXT)"
    )
    conn.executemany(
        "INSERT INTO mlaw_news VALUES (?, ?)",
        [
            ("a", "4 November 2026Posted inSpeeches\n\n1. First paragraph of the speech."),
            ("b", "No byline here, but it was posted in the gazette."),
        ],
    )
    conn.executemany(
        "INSERT INTO judiciary_news VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("e", "https://www.judiciary.gov.sg/n/empty", "Advisory", "2026-08-05", "", "title-only"),
            ("f", "https://www.judiciary.gov.sg/n/full", "Speech", "2026-08-06", "Body", "good"),
        ],
    )
    conn.commit()
    yield conn
    conn.close()


def test_clean_mlaw_content_drops_byline_only() -> None:
    text = "4 November 2026Posted inSpeeches\n\n1. Body.\n\nLast updated on 4 November 2026"
    assert backfill.clean_mlaw_content(text) == "1. Body."
    assert backfill.clean_mlaw_content("1. Body.") == "1. Body."


def test_backfill_mlaw_dry_run_then_apply(db) -> None:
    assert backfill.backfill_mlaw(db, apply=False) == 1
    assert "Posted in" in db.execute("SELECT content_text FROM mlaw_news WHERE id='a'").fetchone()[0]
    assert backfill.backfill_mlaw(db, apply=True) == 1
    rows = dict(db.execute("SELECT id, content_text FROM mlaw_news").fetchall())
    assert rows == {
        "a": "1. First paragraph of the speech.",
        "b": "No byline here, but it was posted in the gazette.",
    }


def _patch_network(monkeypatch, summary: str) -> list[str]:
    seen: list[str] = []
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=read_fixture_bytes("judiciary_article_divs.html"))

    monkeypatch.setattr(
        backfill.httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), headers=kw.get("headers")),
    )
    monkeypatch.setattr(backfill.judiciary_news, "polite_sleep", lambda: None)
    monkeypatch.setattr(
        backfill.judiciary_news,
        "fetch_article_content",
        backfill.judiciary_news.fetch_article_content.retry_with(wait=tenacity.wait_none()),
    )

    async def fake_summary(text: str, title: str) -> str:
        assert text  # summarised from the body, not the title
        return summary

    monkeypatch.setattr(backfill.judiciary_news, "get_summary", fake_summary)
    return seen


def test_backfill_judiciary_dry_run_writes_nothing(db, monkeypatch) -> None:
    seen = _patch_network(monkeypatch, "new summary")
    assert backfill.backfill_judiciary(db, apply=False) == (1, 0)
    assert seen == ["https://www.judiciary.gov.sg/n/empty"]  # only the empty row is fetched
    assert db.execute("SELECT content_text FROM judiciary_news WHERE id='e'").fetchone()[0] == ""


def test_backfill_judiciary_apply_updates_content_and_summary(db, monkeypatch) -> None:
    _patch_network(monkeypatch, "new summary")
    assert backfill.backfill_judiciary(db, apply=True) == (1, 1)
    content, summary = db.execute(
        "SELECT content_text, summary FROM judiciary_news WHERE id='e'"
    ).fetchone()
    assert content.startswith("1.") and "Criminal Mentions Court" in content
    assert summary == "new summary"
    assert db.execute("SELECT content_text, summary FROM judiciary_news WHERE id='f'").fetchone() == (
        "Body",
        "good",
    )


def test_backfill_judiciary_keeps_old_summary_when_llm_fails(db, monkeypatch) -> None:
    _patch_network(monkeypatch, "")
    assert backfill.backfill_judiciary(db, apply=True) == (1, 0)
    content, summary = db.execute(
        "SELECT content_text, summary FROM judiciary_news WHERE id='e'"
    ).fetchone()
    assert content and summary == "title-only"
