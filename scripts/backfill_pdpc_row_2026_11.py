#!/usr/bin/env python3
"""Backfill pdpc_news/593fe23cfbba empty content_text (2026-11-21).

'Singapore Data Festival 2026' (2026-07-24) was imported with empty
content_text: its detail fetch failed during the Tailscale exit-node outage
and the row kept the API/SSR fallback. The proxy is back, so re-fetch via
the module's own fetch_detail_page + parse_detail_page (RSC body extraction)
and regenerate the summary from real content.

    set -a; source .env; source ~/.config/zeeker/.env; set +a
    export LLM_BASE_URL=http://localhost:11434/v1   # host-resolvable override
    uv run python scripts/backfill_pdpc_row_2026_11.py [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from pathlib import Path

import httpx

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "resources"))

import pdpc_news  # noqa: E402

DB_PATH = PROJECT_DIR / "sg-gov-newsrooms.db"
ROW_ID = "593fe23cfbba"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()

    proxy = os.environ.get("TAILSCALE_PROXY")
    if not proxy:
        sys.exit("ERROR: TAILSCALE_PROXY not set (source ~/.config/zeeker/.env first).")

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA busy_timeout=10000")
    title, url, old_ct, old_sum = conn.execute(
        "SELECT title, source_url, content_text, summary FROM pdpc_news WHERE id=?", (ROW_ID,)
    ).fetchone()
    print(f"row {ROW_ID}: {title!r} ct={len(old_ct)} sum={len(old_sum)}")
    print(f"  url: {url}")

    with httpx.Client(
        timeout=pdpc_news.REQUEST_TIMEOUT,
        follow_redirects=True,
        proxy=proxy,
        headers={"User-Agent": pdpc_news.USER_AGENT},
        limits=pdpc_news.HTTP_LIMITS,
    ) as client:
        html = pdpc_news.fetch_detail_page(url, client)
    parsed = pdpc_news.parse_detail_page(html, url)
    content = parsed["content_text"]
    print(f"  re-parsed: title={parsed['title']!r} date={parsed['published_date']} ct={len(content)}")
    print(f"  head: {content[:120]!r}")
    if not content:
        print("  STILL EMPTY — page carries no RSC body; aborting without changes.")
        return
    if not args.apply:
        print("\nDry run only. Re-run with --apply to write.")
        return

    conn.execute("UPDATE pdpc_news SET content_text=? WHERE id=?", (content, ROW_ID))
    conn.commit()
    new_sum = asyncio.run(pdpc_news.get_summary(content, parsed["title"] or title))
    if new_sum:
        conn.execute("UPDATE pdpc_news SET summary=? WHERE id=?", (new_sum, ROW_ID))
        conn.commit()
        print(f"  summary regenerated: {len(old_sum)} -> {len(new_sum)} chars")
    else:
        print("  summary FAILED — old summary kept")
    fts = conn.execute("SELECT COUNT(*) FROM pdpc_news_fts").fetchone()[0]
    n = conn.execute("SELECT COUNT(*) FROM pdpc_news").fetchone()[0]
    ect = conn.execute("SELECT COUNT(*) FROM pdpc_news WHERE content_text='' OR content_text IS NULL").fetchone()[0]
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    print(f"pdpc_news: rows={n} fts={fts} empty_ct={ect}")
    print("Applied. Next: uv run zeeker deploy")


if __name__ == "__main__":
    main()
