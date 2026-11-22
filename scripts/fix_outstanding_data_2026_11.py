#!/usr/bin/env python3
"""One-off outstanding-data repair for sg-gov-newsrooms.db (2026-11-21 survey).

Fixes:
  A. mom_news dedup: delete www-variant twin rows. The April backfill stored
     www.mom.gov.sg URLs; on 2026-06-15 the sitemap flipped to non-www and the
     exact-string source_url dedup let every article re-import under a new URL.
     The live sitemap is 100% non-www, so the NON-www rows are kept — deleting
     those instead would let all dups return on the next build.
  B. pdpc_news dedup: delete one row of the exact (id, source_url) race dup
     ('DNC Registry Figures', inserted twice 6s apart on 2026-04-29) and the
     two /press-room/-scheme twins of items that already exist under the
     current /media-events/ scheme (current discovery only reads /media-events/*).
  C. mom_news content: backfill content_text for 5 rows stored empty. MOM's
     newer template puts the article body in #pagecontent_*_documentcontent_*_DivCode
     as span/<br>-separated text; the old extractor only reads p/li/td and its
     body fallback form.decompose() wipes the whole ASP.NET page form. Also:
     row bfd691688628 ('0727-mast-calls-for-greater-vigilance') — MOM re-dated
     the live URL to 0728; remap source_url, fix the '-' title from the page h1,
     take published_date from the page meta, regenerate the title-only summary.
     All 5 rows get their summaries regenerated (they were generated from the
     title alone while content_text was empty).
  D. mlaw_news: remap 3 dead feed URLs to live canonical URLs found in the
     sitemap; backfill the 2 empty titles from page h1; backfill content_text
     for the 2 rows whose feed entries carried no body (CALC keynote, CLAF
     closing address) and regenerate their title-only summaries.
  E. Empty summaries (LLM outage ~2026-06-04): judiciary 1, ccs 1, agc 3
     (the agc rows are migration-banner stubs — the live pages carry no real
     text either, so the existing content is summarized as-is).

Row ids are NOT recomputed when source_url is remapped: id stays the stable
opaque key it was created with (remap is a link repair, not a new article).

FTS tables stay in sync automatically via the ai/ad/au triggers.

Dry run by default; pass --apply to write. Run between daily builds (the
build appends; it does not overwrite these repairs), then `uv run zeeker deploy`.

    set -a; source .env; set +a      # LLM_* for summary generation
    uv run python scripts/fix_outstanding_data_2026_11.py            # dry run
    uv run python scripts/fix_outstanding_data_2026_11.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "resources"))

import agc_news  # noqa: E402
import ccs_news  # noqa: E402
import judiciary_news  # noqa: E402
import mlaw_news  # noqa: E402
import mom_news  # noqa: E402

DB_PATH = PROJECT_DIR / "sg-gov-newsrooms.db"
USER_AGENT = "ZeekerBot/1.0 (+https://data.zeeker.sg; sg-gov-newsrooms research bot)"
BANNER_RE = re.compile(r"Government agencies communicate via\.gov\.sg", re.I)

# --- C: mom rows with empty content_text (survey 2026-11-21) ---------------
# url_for fetch; bfd691688628's stored URL is dead (MOM re-dated it to 0728).
MOM_CONTENT_ROWS = [
    {"id": "16100ea76989", "fetch_url": "https://mom.gov.sg/newsroom/press-releases/2026/0309-launch-of-clfs"},
    {"id": "19218595cb85", "fetch_url": "https://mom.gov.sg/newsroom/press-releases/2026/0819-change-in-nwc-chairmanship"},
    {"id": "42c70c448bea", "fetch_url": "https://mom.gov.sg/newsroom/press-releases/2026/1102-mom-investigating-companies-involved-in-airport-operations-for-suspected-work-pass-offences"},
    {"id": "00f0dda6522b", "fetch_url": "https://mom.gov.sg/newsroom/press-releases/2026/1111-former-site-manager-convicted-and-imprisoned-for-demanding-kickbacks-from-migrant-workers"},
    {"id": "bfd691688628", "fetch_url": "https://mom.gov.sg/newsroom/press-releases/2026/0728-mast-calls-for-greater-vigilance",
     "remap_url": "https://mom.gov.sg/newsroom/press-releases/2026/0728-mast-calls-for-greater-vigilance", "fix_title": True, "fix_date": True},
]

# --- D: mlaw rows ------------------------------------------------------------
MLAW_FIX_ROWS = [
    {"id": "d7b96cf57dcb", "new_url": "https://www.mlaw.gov.sg/continued-designation-of-cbs-as-the-operator-of-the-moneylenders-credit-bureau/",
     "fix_title": True},
    {"id": "81393120e605", "new_url": "https://www.mlaw.gov.sg/minister-edwin-tong-delivers-statement-on-emerging-technologies-and-maritime-security/",
     "fix_title": True},
    {"id": "da90e322d151", "new_url": None, "backfill_content": True},
    {"id": "aeee62b96200", "new_url": "https://www.mlaw.gov.sg/closing-address-by-minister-edwin-tong-sc-at-chief-legal-advisors-forum-2026/",
     "fix_title": False, "backfill_content": True},
]

# --- E: rows with empty summary (content present) ---------------------------
EMPTY_SUMMARY_ROWS = {
    "judiciary_news": ["18c22eb7360f"],
    "ccs_news": ["a06180140c64"],
    "agc_news": ["c5244ea73033", "fc30b8b83074", "7bd6693b445d"],
}


def polite_sleep():
    time.sleep(1.5)


def norm_url(url: str) -> str:
    return url.replace("://www.", "://").rstrip("/").lower()


# =============================================================================
# EXTRACTION
# =============================================================================


def fetch_mom_content(url: str, client: httpx.Client) -> tuple[str, str, str | None]:
    """Extract (title, content, meta_published_date) from a mom.gov.sg page.

    The body lives in #pagecontent_*_documentcontent_*_DivCode as span/<br>
    text (NOT p/li/td). 'documentcontentdescription' divs are empty siblings —
    never select those.
    """
    r = client.get(url)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "lxml")

    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""
    meta = soup.find("meta", attrs={"name": "published_date"})
    meta_pd = (meta.get("content") or "").strip() or None if meta else None

    divs = soup.find_all("div", id=re.compile(r"documentcontent_\d+_DivCode$"))
    if not divs:
        return title, "", meta_pd
    best = max(divs, key=lambda d: len(d.get_text(strip=True)))

    # drop title / category link / bare-date chrome from the container
    for el in best.find_all("h1"):
        el.decompose()
    for el in best.find_all("a", id=re.compile("RptCategory")):
        el.decompose()
    for el in best.find_all(["span", "p", "div"]):
        if re.fullmatch(r"\d{1,2}\s+[A-Z][a-z]+\s+\d{4}", el.get_text(strip=True) or ""):
            el.decompose()
    # <br> acts as the paragraph separator in this template
    for br in best.find_all("br"):
        br.replace_with("\n\n")

    text = best.get_text(" ", strip=True)
    text = re.sub(r" *\n\n *", "\n\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    for marker in ("Share this page",):
        i = text.find(marker)
        if i != -1:
            text = text[:i]
    text = re.sub(r"\s+\n", "\n", text).strip()
    return title, text, meta_pd


def fetch_mlaw_content(url: str, client: httpx.Client) -> tuple[str, str]:
    """Extract (title, content) from an mlaw.gov.sg detail page.

    mlaw_news.fetch_article mis-extracts these pages (it grabs the gov.sg
    anti-scam banner), so this uses the largest content block instead, then
    drops byline/metadata lines the same way the fixed feed extractor does.
    """
    r = client.get(url)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "lxml")
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


# =============================================================================
# DEDUP PLANNING
# =============================================================================


def plan_mom_drops(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    groups: dict[str, list[tuple[str, str]]] = {}
    for rid, url in conn.execute("SELECT id, source_url FROM mom_news"):
        groups.setdefault(norm_url(url), []).append((rid, url))
    drops = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        keep = next((m for m in members if "://www." not in m[1]), members[0])
        drops.extend(m for m in members if m[0] != keep[0] or m[1] != keep[1])
    return drops


def plan_pdpc_drops(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    """Return (id, source_url, keep_created_at, reason) tuples to delete.

    The delete must keep the earliest created_at row: an exact (id, source_url)
    duplicate pair shares BOTH key columns, so deleting by id+url alone would
    remove both copies of the article.
    """
    drops: list[tuple[str, str, str, str]] = []
    # 1. exact (id, source_url) duplicates -> keep earliest created_at
    groups: dict[tuple[str, str], list[str]] = {}
    for rid, url, created in conn.execute("SELECT id, source_url, created_at FROM pdpc_news"):
        groups.setdefault((rid, url), []).append(created)
    for (rid, url), createds in groups.items():
        if len(createds) > 1:
            keep = min(createds)
            drops.append((rid, url, keep, f"exact dup, keep created_at={keep}"))
    # 2. /press-room/-scheme twins of /media-events/ rows (same date+title)
    rows = list(conn.execute("SELECT id, source_url, published_date, title FROM pdpc_news"))
    seen: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for rid, url, pd_, title in rows:
        seen.setdefault((pd_, (title or "").strip().lower()), []).append((rid, url))
    for key, members in seen.items():
        if len(members) < 2:
            continue
        pr = [m for m in members if "/press-room/" in m[1]]
        me = [m for m in members if "/media-events/" in m[1]]
        if len(pr) == 1 and len(me) >= 1:
            drops.append((pr[0][0], pr[0][1], "", f"press-room twin of {me[0][0]} ({key[0]})"))
    return drops


# =============================================================================
# MAIN
# =============================================================================


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


async def summarise(module, text: str, title: str) -> str:
    return await module.get_summary(text, title)


def run_summary(module, text: str, title: str) -> str:
    try:
        return asyncio.run(summarise(module, text, title))
    except Exception as e:  # noqa: BLE001 - report, keep old summary
        print(f"      summary FAILED ({type(e).__name__}: {e}) — old summary kept")
        return ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()

    if args.apply:
        import os
        if not os.environ.get("LLM_BASE_URL"):
            sys.exit("ERROR: LLM_BASE_URL not set (source .env first) — summaries would be blanked.")

    print(f"DB: {DB_PATH}  ({'APPLY' if args.apply else 'dry run'})\n")
    conn = get_db()

    if args.apply:
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        backup = Path.home() / "archive" / "ops-2026" / f"sg-gov-newsrooms.db.pre-fix-{stamp}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DB_PATH, backup)
        print(f"backup: {backup}\n")

    # ---------------- A + B: dedup ----------------
    mom_drops = plan_mom_drops(conn)
    pdpc_drops = plan_pdpc_drops(conn)
    print(f"A. mom_news: {len(mom_drops)} www-variant twin rows to delete")
    for rid, url in mom_drops:
        print(f"   DROP {rid} {url}")
    print(f"\nB. pdpc_news: {len(pdpc_drops)} duplicate rows to delete")
    for rid, url, keep, why in pdpc_drops:
        print(f"   DROP {rid} {url}  ({why})")

    if args.apply:
        for rid, url in mom_drops:
            cur = conn.execute("DELETE FROM mom_news WHERE id=? AND source_url=?", (rid, url))
            assert cur.rowcount == 1, f"mom delete matched {cur.rowcount} rows for {rid}"
        for rid, url, keep, _ in pdpc_drops:
            if keep:
                cur = conn.execute(
                    "DELETE FROM pdpc_news WHERE id=? AND source_url=? AND created_at != ?",
                    (rid, url, keep),
                )
            else:
                cur = conn.execute("DELETE FROM pdpc_news WHERE id=? AND source_url=?", (rid, url))
            assert cur.rowcount == 1, f"pdpc delete matched {cur.rowcount} rows for {rid}"
        conn.commit()
        print("   deleted + committed")

    # ---------------- C: mom content ----------------
    print("\nC. mom_news empty-content rows:")
    with httpx.Client(timeout=30.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        for spec in MOM_CONTENT_ROWS:
            row = conn.execute(
                "SELECT title, published_date, LENGTH(content_text), LENGTH(summary) FROM mom_news WHERE id=?",
                (spec["id"],),
            ).fetchone()
            if row is None:
                print(f"   {spec['id']}: NOT FOUND (skipped)")
                continue
            old_title, old_pd, old_ct, old_sum = row
            try:
                title, content, meta_pd = fetch_mom_content(spec["fetch_url"], client)
            except Exception as e:  # noqa: BLE001
                print(f"   {spec['id']}: FETCH FAILED {type(e).__name__}: {e}")
                continue
            date_note = ""
            if spec.get("fix_date") and meta_pd and meta_pd != old_pd:
                date_note = f" [date {old_pd} -> {meta_pd}]"
            print(f"   {spec['id']}: ct {old_ct} -> {len(content)} chars, "
                  f"title {old_title[:30]!r} -> {title[:40]!r}{date_note}")
            print(f"      head: {content[:110]!r}")
            if args.apply and content:
                conn.execute(
                    "UPDATE mom_news SET content_text=?, title=?, source_url=COALESCE(?, source_url), "
                    "published_date=COALESCE(?, published_date) WHERE id=?",
                    (content, title if spec.get("fix_title") else old_title,
                     spec.get("remap_url"), meta_pd if spec.get("fix_date") else None, spec["id"]),
                )
                conn.commit()
                new_sum = run_summary(mom_news, content, title)
                if new_sum:
                    conn.execute("UPDATE mom_news SET summary=? WHERE id=?", (new_sum, spec["id"]))
                    conn.commit()
                    print(f"      summary regenerated ({len(new_sum)} chars)")
            polite_sleep()

        # ---------------- D: mlaw ----------------
        print("\nD. mlaw_news fixes:")
        for spec in MLAW_FIX_ROWS:
            row = conn.execute(
                "SELECT source_url, title, LENGTH(content_text), LENGTH(summary), published_date FROM mlaw_news WHERE id=?",
                (spec["id"],),
            ).fetchone()
            if row is None:
                print(f"   {spec['id']}: NOT FOUND (skipped)")
                continue
            old_url, old_title, old_ct, old_sum, old_pd = row
            target = spec.get("new_url") or old_url
            try:
                title, content = fetch_mlaw_content(target, client)
            except Exception as e:  # noqa: BLE001
                print(f"   {spec['id']}: FETCH FAILED {type(e).__name__}: {e}")
                continue
            notes = []
            if spec.get("new_url") and spec["new_url"] != old_url:
                notes.append(f"url remap {old_url[-45:]} -> {spec['new_url'][-45:]}")
            if spec.get("fix_title") and title and title != old_title:
                notes.append(f"title {old_title[:25]!r} -> {title[:35]!r}")
            if spec.get("backfill_content"):
                notes.append(f"ct {old_ct} -> {len(content)}")
            print(f"   {spec['id']}: {'; '.join(notes) or 'no change needed'}")
            if spec.get("backfill_content"):
                print(f"      head: {content[:110]!r}")
            if args.apply:
                if spec.get("new_url"):
                    conn.execute("UPDATE mlaw_news SET source_url=? WHERE id=?", (spec["new_url"], spec["id"]))
                if spec.get("fix_title") and title:
                    conn.execute("UPDATE mlaw_news SET title=? WHERE id=?", (title, spec["id"]))
                if spec.get("backfill_content") and content:
                    conn.execute("UPDATE mlaw_news SET content_text=? WHERE id=?", (content, spec["id"]))
                conn.commit()
                if spec.get("backfill_content") and content:
                    new_sum = run_summary(mlaw_news, content, title or old_title)
                    if new_sum:
                        conn.execute("UPDATE mlaw_news SET summary=? WHERE id=?", (new_sum, spec["id"]))
                        conn.commit()
                        print(f"      summary regenerated ({len(new_sum)} chars)")
            polite_sleep()

    # ---------------- E: empty summaries ----------------
    print("\nE. empty-summary rows (content present):")
    modules = {"judiciary_news": judiciary_news, "ccs_news": ccs_news, "agc_news": agc_news}
    for table, ids in EMPTY_SUMMARY_ROWS.items():
        mod = modules[table]
        for rid in ids:
            title, content = conn.execute(
                f"SELECT title, content_text FROM {table} WHERE id=?", (rid,)
            ).fetchone()
            if content is None:
                print(f"   {table}/{rid}: no content — skipped")
                continue
            print(f"   {table}/{rid}: summarising {len(content)} chars ({title[:45]!r})")
            if args.apply:
                new_sum = run_summary(mod, content, title)
                if new_sum:
                    conn.execute(f"UPDATE {table} SET summary=? WHERE id=?", (new_sum, rid))
                    conn.commit()
                    print(f"      summary written ({len(new_sum)} chars)")

    # ---------------- verification ----------------
    print("\n== VERIFICATION ==")
    for t in ["mlaw_news", "judiciary_news", "agc_news", "ipos_news", "ccs_news", "acra_news", "mom_news", "pdpc_news"]:
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        fts = conn.execute(f"SELECT COUNT(*) FROM {t}_fts").fetchone()[0]
        empty_ct = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE content_text IS NULL OR content_text=''").fetchone()[0]
        empty_sum = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE summary IS NULL OR summary=''").fetchone()[0]
        # remaining normalized dups
        dups = 0
        groups: dict[str, int] = {}
        for (url,) in conn.execute(f"SELECT source_url FROM {t}"):
            groups[norm_url(url)] = groups.get(norm_url(url), 0) + 1
        dups = sum(v - 1 for v in groups.values() if v > 1)
        flag = "OK" if fts == n and dups == 0 else "*** CHECK ***"
        print(f"  {t}: rows={n} fts={fts} empty_ct={empty_ct} empty_sum={empty_sum} norm_dups={dups} {flag}")
    if not args.apply:
        print("\nDry run only. Re-run with --apply to write.")
    else:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        print("\nApplied. Next: uv run zeeker deploy")
    conn.close()


if __name__ == "__main__":
    main()
