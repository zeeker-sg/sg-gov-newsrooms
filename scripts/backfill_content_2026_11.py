#!/usr/bin/env python3
"""
One-off content backfill for sg-gov-newsrooms.db (November 2026, PR #13).

Resources are insert-only, so rows stored by the old extractors keep their bad
content_text until fixed in place. This script fixes two known problems:

1. judiciary_news rows with empty content_text. These pages put their body in
   bare <div>s, which the old extractor didn't read, so the row was stored
   empty and the summary was generated from the title alone. Deleting the rows
   would NOT get them re-fetched: judiciary discovery stops paging once a whole
   page is already in the DB. This re-fetches each page with the fixed
   extractor, writes content_text, and regenerates the summary.

2. mlaw_news rows whose content_text contains the "<date> Posted in <category>"
   byline. The feed only carries ~10 posts, so they can't be re-fetched;
   byline paragraphs are dropped in place (no network, no LLM).

Dry run by default; pass --apply to write. Run it on the host between daily
builds (the build starts by syncing the DB from S3), then deploy:

    cd <project dir>
    set -a; source ~/.config/zeeker/.env; set +a      # LLM_* for summaries
    uv run zeeker build --sync-from-s3 mlaw_news       # or otherwise fetch the latest DB
    uv run python scripts/backfill_content_2026_11.py            # dry run
    uv run python scripts/backfill_content_2026_11.py --apply
    uv run zeeker deploy
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

import httpx

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "resources"))

import judiciary_news  # noqa: E402
import mlaw_news  # noqa: E402

DEFAULT_DB = PROJECT_DIR / "sg-gov-newsrooms.db"
USER_AGENT = "ZeekerBot/1.0 (+https://data.zeeker.sg; sg-gov-newsrooms research bot)"


def clean_mlaw_content(text: str) -> str:
    """Drop byline/footer paragraphs, exactly as the fixed extractor would."""
    paras = text.split("\n\n")
    return "\n\n".join(p for p in paras if not mlaw_news._is_metadata_line(p.strip()))


def backfill_mlaw(conn: sqlite3.Connection, apply: bool) -> int:
    rows = conn.execute(
        "SELECT id, content_text FROM mlaw_news WHERE content_text LIKE '%Posted in%'"
    ).fetchall()
    changed = 0
    for row_id, text in rows:
        cleaned = clean_mlaw_content(text)
        if cleaned == text:
            continue
        changed += 1
        if apply:
            conn.execute("UPDATE mlaw_news SET content_text = ? WHERE id = ?", (cleaned, row_id))
    print(f"mlaw_news: {len(rows)} rows mention 'Posted in', {changed} cleaned")
    return changed


def backfill_judiciary(conn: sqlite3.Connection, apply: bool) -> tuple[int, int]:
    rows = conn.execute(
        "SELECT id, source_url, title FROM judiciary_news "
        "WHERE content_text IS NULL OR content_text = '' ORDER BY published_date"
    ).fetchall()
    print(f"judiciary_news: {len(rows)} rows with empty content_text")
    fixed = summarised = 0
    with httpx.Client(
        timeout=judiciary_news.REQUEST_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        for i, (row_id, url, title) in enumerate(rows):
            if i:
                judiciary_news.polite_sleep()
            try:
                text = judiciary_news.fetch_article_content(url, client)
            except Exception as e:  # noqa: BLE001 - report and move on
                print(f"  FAILED {url}: {e}")
                continue
            if not text:
                print(f"  STILL EMPTY {url}")
                continue
            fixed += 1
            print(f"  {len(text):>5} chars  {title[:70]}")
            if not apply:
                continue
            summary = asyncio.run(judiciary_news.get_summary(text, title))
            if summary:
                summarised += 1
                conn.execute(
                    "UPDATE judiciary_news SET content_text = ?, summary = ? WHERE id = ?",
                    (text, summary, row_id),
                )
            else:
                # Keep the old summary rather than blank it; flag for a re-run.
                print("    summary failed; content written, old summary kept")
                conn.execute(
                    "UPDATE judiciary_news SET content_text = ? WHERE id = ?", (text, row_id)
                )
            conn.commit()
    return fixed, summarised


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()

    if not args.db.exists():
        sys.exit(f"DB not found: {args.db}")
    print(f"DB: {args.db}  ({'APPLY' if args.apply else 'dry run'})\n")

    conn = sqlite3.connect(str(args.db), timeout=30)
    try:
        backfill_mlaw(conn, args.apply)
        conn.commit()
        fixed, summarised = backfill_judiciary(conn, args.apply)
    finally:
        conn.close()

    if args.apply:
        print(f"\njudiciary_news: {fixed} rows re-extracted, {summarised} re-summarised")
        print("Next: uv run zeeker deploy")
    else:
        print("\nDry run only. Re-run with --apply to write.")


if __name__ == "__main__":
    main()
