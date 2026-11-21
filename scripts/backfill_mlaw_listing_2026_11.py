#!/usr/bin/env python3
"""One-off mlaw_news coverage backfill via the /news/ listing page (2026-11-21).

mlaw_news discovery reads the Atom feed (https://www.mlaw.gov.sg/feed.xml),
which only carries ~10 recent posts. Items that fell off the feed between
daily builds are never re-discovered, so the table starts at 2026-03-27 and
has further holes. The /news/ listing page server-renders every item with a
year-filtered id (id="2026"), so this walks the listing once and imports
every 2026 item the DB is missing.

Rows are built exactly the way the resource builds them from the feed:
id = sha256(source_url)[:12], category mapped onto NEWS_CATEGORIES,
content extracted from the detail page, AI summary via the module's
get_summary. Items dated before 2026-01-01 are skipped (resource START_DATE).

Dry run by default; --apply to write. Run between daily builds, then:
uv run zeeker deploy.

    set -a; source .env; set +a
    export LLM_BASE_URL=http://localhost:11434/v1   # host-resolvable override
    uv run python scripts/backfill_mlaw_listing_2026_11.py [--apply]
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "resources"))
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import mlaw_news  # noqa: E402
from fix_outstanding_data_2026_11 import BANNER_RE, norm_url  # noqa: E402

DB_PATH = PROJECT_DIR / "sg-gov-newsrooms.db"
LISTING_URL = "https://www.mlaw.gov.sg/news/"
USER_AGENT = "ZeekerBot/1.0 (+https://data.zeeker.sg; sg-gov-newsrooms research bot)"
START_DATE = date(2026, 1, 1)

# listing label (lowercased, hyphenated) -> NEWS_CATEGORIES member
LABEL_TO_CATEGORY = {
    "press-releases": "press-releases",
    "speeches": "speeches",
    "parliamentary-speech": "parliamentary-speeches",
    "parliamentary-speeches": "parliamentary-speeches",
    "announcements": "announcements",
    "visits": "visits",
    "replies": "replies",
}

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_listing(html: str) -> list[dict]:
    year_spans = [(m.group(1), m.start()) for m in re.finditer(r'id="(20\d\d)"', html)]
    cards = []
    for i, (year, start) in enumerate(year_spans):
        end = year_spans[i + 1][1] if i + 1 < len(year_spans) else len(html)
        section = html[start:end]
        for cm in re.finditer(r'<a href="(/[a-z0-9-]+/)" class="is-media-card">(.*?)</a>', section, re.S):
            url, inner = cm.group(1), cm.group(2)
            tm = re.search(r"<b>(.*?)</b>", inner, re.S)
            catm = re.search(r'<small class="has-text-white padding--bottom">(.*?)</small>', inner)
            label = (catm.group(1).strip().lower().replace(" ", "-") if catm else "")
            cards.append({
                "year": year,
                "url": f"https://www.mlaw.gov.sg{url}",
                "listing_title": re.sub(r"\s+", " ", tm.group(1)).strip() if tm else "",
                "category": LABEL_TO_CATEGORY.get(label, "other"),
            })
    return cards


def extract_mlaw_from_soup(soup: BeautifulSoup) -> tuple[str, str]:
    """Soup-based variant of fix_outstanding_data_2026_11.fetch_mlaw_content."""
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""
    for unwanted in soup.find_all(["script", "style", "nav", "header", "footer", "aside", "form"]):
        unwanted.decompose()
    best = ""
    for sec in soup.find_all(["section", "article", "div"]):
        paras = []
        for p in sec.find_all(["p", "li", "h2", "h3", "blockquote"]):
            t = p.get_text(" ", strip=True)
            if len(t) > 20 and not BANNER_RE.search(t):
                paras.append(t)
        joined = "\n\n".join(paras)
        if len(joined) > len(best):
            best = joined
    paras = [p for p in best.split("\n\n") if not mlaw_news._is_metadata_line(p.strip())]
    return title, "\n\n".join(paras).strip()


def extract_published_date(soup: BeautifulSoup, raw_html: str) -> date | None:
    """Prefer og:article:published_time meta; fall back to the 'DD Month YYYY
    Posted in <Category>' byline in the tag-stripped page text."""
    for sel in ('meta[property="article:published_time"]', 'meta[name="published_date"]',
                'meta[property="og:article:published_time"]'):
        el = soup.select_one(sel)
        if el and el.get("content"):
            try:
                return datetime.fromisoformat(el["content"]).date()
            except ValueError:
                pass
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw_html))
    m = re.search(r"(\d{1,2}) ([A-Za-z]+) (20\d\d) Posted in", text)
    if m and m.group(2).lower() in MONTHS:
        try:
            return date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
        except ValueError:
            pass
    # hero date badge (e.g. '29 MAR 2026') precedes the content; the footer's
    # 'Last updated on' comes later, so the FIRST date match is the badge.
    m = re.search(r"(\d{1,2}) ([A-Za-z]{3,9}) (20\d\d)", text)
    if m and m.group(2).lower() in MONTHS:
        try:
            return date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
        except ValueError:
            pass
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()

    if args.apply:
        import os
        if not os.environ.get("LLM_BASE_URL"):
            sys.exit("ERROR: LLM_BASE_URL not set (source .env first).")

    print(f"DB: {DB_PATH}  ({'APPLY' if args.apply else 'dry run'})\n")
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA busy_timeout=10000")

    existing = {norm_url(u) for (u,) in conn.execute("SELECT source_url FROM mlaw_news")}
    print(f"existing mlaw_news rows: {len(existing)}")

    with httpx.Client(timeout=60.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        r = client.get(LISTING_URL)
        r.raise_for_status()
        cards = parse_listing(r.text)
        missing = [c for c in cards if c["year"] == "2026" and norm_url(c["url"]) not in existing]
        # de-dup within the listing itself
        seen: set[str] = set()
        unique_missing = []
        for c in missing:
            if norm_url(c["url"]) in seen:
                continue
            seen.add(norm_url(c["url"]))
            unique_missing.append(c)
        print(f"listing cards: {len(cards)} (2026: {sum(1 for c in cards if c['year'] == '2026')})")
        print(f"2026 items missing from DB: {len(unique_missing)}\n")

        imported = skipped = 0
        for c in unique_missing:
            try:
                r2 = client.get(c["url"])
                r2.raise_for_status()
                soup = BeautifulSoup(r2.content, "lxml")
                title, content = extract_mlaw_from_soup(soup)
            except Exception as e:  # noqa: BLE001
                print(f"  FAILED {c['url']}: {type(e).__name__}: {e}")
                skipped += 1
                continue
            pd_ = extract_published_date(soup, r2.text)
            title = title or c["listing_title"]
            if pd_ is None:
                print(f"  SKIP (no date)  {title[:60]!r}")
                skipped += 1
                time.sleep(1.5)
                continue
            if pd_ < START_DATE:
                print(f"  SKIP (before {START_DATE}: {pd_})  {title[:55]!r}")
                skipped += 1
                time.sleep(1.5)
                continue
            print(f"  {pd_} [{c['category']:22}] ct={len(content):6}  {title[:58]!r}")
            if not content:
                print("     WARNING: empty content extracted")
            if args.apply and content:
                rid = mlaw_news.make_id(c["url"])
                try:
                    summary = asyncio.run(mlaw_news.get_summary(content, title))
                except Exception as e:  # noqa: BLE001
                    print(f"     summary FAILED ({type(e).__name__}: {e}) — importing with empty summary")
                    summary = ""
                conn.execute(
                    "INSERT INTO mlaw_news (id, title, published_date, category, source_url, "
                    "summary, content_text, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (rid, title, pd_.isoformat(), c["category"], c["url"], summary or "",
                     content, datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
                imported += 1
                print(f"     INSERTED id={rid} summary={len(summary)} chars")
            time.sleep(1.5)

    print(f"\n{'imported' if args.apply else 'would import'}: {imported}, skipped: {skipped}")
    n = conn.execute("SELECT COUNT(*) FROM mlaw_news").fetchone()[0]
    fts = conn.execute("SELECT COUNT(*) FROM mlaw_news_fts").fetchone()[0]
    mn = conn.execute("SELECT MIN(published_date) FROM mlaw_news").fetchone()[0]
    print(f"mlaw_news: rows={n} fts={fts} earliest={mn}")
    if not args.apply:
        print("\nDry run only. Re-run with --apply to write.")
    else:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        print("\nApplied. Next: uv run zeeker deploy")
    conn.close()


if __name__ == "__main__":
    main()
