"""
Accounting and Corporate Regulatory Authority (ACRA) Singapore newsroom resource.

Cadence: Daily (Tier 1)
Source: https://www.acra.gov.sg/news-events/news-announcements/
Strategy: Incremental — sitemap.xml discovery, filtered to /news-events/news-announcements/* URLs
  not already in DB. lastmod dates in the sitemap may not reflect publication dates, so we scrape
  each article page for its date and filter by published_date >= START_DATE post-scrape.

Licensing: © Government of Singapore, all rights reserved.
Content stored but NOT shown by default in Datasette UI. See zeeker.toml for column config.
"""

import asyncio
import hashlib
import os
import random
import re
import time
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree

import click
import httpx
from bs4 import BeautifulSoup
from openai import AsyncOpenAI
from sqlite_utils.db import Table
from tenacity import retry, stop_after_attempt, wait_exponential
from zeeker import Skip

# Sibling imports — zeeker >= 0.9.0 puts resources/ on sys.path during module
# load, so no sys.path shim is needed.
from _isomer import fetch_isomer_listing_dates
from _token_usage import _log_token_usage

# =============================================================================
# CONFIGURATION
# =============================================================================

RESOURCE_NAME = "acra_news"


def _echo(message: str, err: bool = False) -> None:
    """click.echo with the resource name prefixed (kept after leading whitespace)."""
    stripped = message.lstrip(" \n")
    leading = message[: len(message) - len(stripped)]
    click.echo(f"{leading}{RESOURCE_NAME}: {stripped}", err=err)


BASE_URL = "https://www.acra.gov.sg"
SITEMAP_URL = "https://www.acra.gov.sg/sitemap.xml"
LISTING_URL = "https://www.acra.gov.sg/news-events/news-announcements/"
NEWS_PREFIX = "/news-events/news-announcements/"

# Only keep articles published from this date onwards
START_DATE = date(2026, 1, 1)

# Scraping rate limits (Tier 1 — daily incremental, be polite)
REQUEST_DELAY_BASE = 1.5
REQUEST_DELAY_JITTER = 0.5
REQUEST_TIMEOUT = 30.0
MAX_CONSECUTIVE_FAILURES = 5
MAX_RETRIES = 3

# LLM concurrency
_LLM_SEMAPHORES = {}


def _get_llm_semaphore() -> asyncio.Semaphore:
    try:
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
    except RuntimeError:
        loop_id = 0
    if loop_id not in _LLM_SEMAPHORES:
        _LLM_SEMAPHORES[loop_id] = asyncio.Semaphore(3)
    return _LLM_SEMAPHORES[loop_id]


# =============================================================================
# SYSTEM PROMPT
# =============================================================================

SUMMARY_SYSTEM_PROMPT = """
As an expert in Singapore corporate regulation and business compliance, provide
concise summaries of ACRA news announcements, press releases, and speeches for
business owners, compliance professionals, and corporate service providers.
Highlight key regulatory changes, enforcement actions, filing requirements,
corporate governance updates, or industry developments. Write 1 narrative paragraph,
no longer than 100 words. Focus on what changed, who is affected, and why it matters
for businesses and corporate entities in Singapore.
"""

# =============================================================================
# HELPERS
# =============================================================================


def make_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:12]


def polite_sleep():
    delay = REQUEST_DELAY_BASE + random.uniform(-REQUEST_DELAY_JITTER, REQUEST_DELAY_JITTER)
    time.sleep(max(0.5, delay))


def infer_category(url: str, title: str) -> str:
    """
    Infer article category from URL slug and title.

    ACRA publishes: announcements, press releases, speeches, and ACRAConnect newsletters.
    """
    slug = url.lower()
    title_lower = title.lower()

    if "press-release" in slug or "press release" in title_lower:
        return "press-release"
    if any(x in slug or x in title_lower for x in ["speech", "address", "remarks"]):
        return "speech"
    if "newsletter" in slug or "acraconnect" in title_lower or "newsletter" in title_lower:
        return "newsletter"
    if "announcement" in slug or "announcement" in title_lower:
        return "announcement"
    return "other"


def parse_date_string(date_str: str) -> Optional[str]:
    """Parse 'DD Month YYYY' or 'DD Mon YYYY' to ISO date string."""
    date_str = date_str.strip()
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(date_str, fmt).date().isoformat()
        except ValueError:
            continue
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(20\d{2})", date_str)
    if m:
        for fmt in ("%d %B %Y", "%d %b %Y"):
            try:
                return (
                    datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", fmt)
                    .date()
                    .isoformat()
                )
            except ValueError:
                continue
    return None


# =============================================================================
# SITEMAP DISCOVERY
# =============================================================================


def discover_urls_from_sitemap(client: httpx.Client, existing_urls: set) -> List[str]:
    """
    Fetch sitemap.xml and return news announcement URLs not already in the database.

    ACRA's sitemap contains ~480 news-announcement URLs. We filter to the
    /news-events/news-announcements/ prefix, excluding the index page itself
    and any URL already in the DB.

    Raises on fetch/parse failure so fetch_data can emit an ABORTED status line.
    """
    _echo(f"Fetching sitemap: {SITEMAP_URL}")
    response = client.get(SITEMAP_URL)
    response.raise_for_status()

    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ElementTree.fromstring(response.content)

    all_urls = [
        url_el.findtext("sm:loc", namespaces=ns) or "" for url_el in root.findall("sm:url", ns)
    ]

    news_urls = [
        url
        for url in all_urls
        if NEWS_PREFIX in url
        and url.rstrip("/") != f"{BASE_URL}{NEWS_PREFIX}".rstrip("/")
        and url not in existing_urls
    ]

    _echo(
        f"Sitemap: {len(all_urls)} total URLs, "
        f"{len(news_urls)} new news announcement URLs to scrape."
    )
    return news_urls


# =============================================================================
# ARTICLE SCRAPING
# =============================================================================


@retry(stop=stop_after_attempt(MAX_RETRIES), wait=wait_exponential(multiplier=2, min=1, max=10))
def fetch_article(url: str, client: httpx.Client) -> Dict[str, Any]:
    """
    Fetch and extract content from an acra.gov.sg news announcement page.

    The site is built on Isomer (Next.js). Key extraction notes:
    - Title: <h1> element
    - Date: footer text pattern "last updated DD Month YYYY"; also searched in full page text
    - Content: <main> element, stripping nav/footer/aside noise
    """
    response = client.get(url)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "lxml")

    # Title
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
    if not title:
        title_tag = soup.find("title")
        if title_tag:
            title = title_tag.get_text(strip=True).split("|")[0].strip()

    # Date — look for "last updated DD Month YYYY" pattern first (Isomer footer)
    published_date = None
    page_text = soup.get_text(" ", strip=True)

    last_updated = re.search(
        r"last updated\s+(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|"
        r"September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|"
        r"Jul|Aug|Sep|Oct|Nov|Dec)\s+20\d{2})",
        page_text,
        re.I,
    )
    if last_updated:
        published_date = parse_date_string(last_updated.group(1))

    # Fallback: any date pattern in the page
    if not published_date:
        m = re.search(
            r"\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|"
            r"September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|"
            r"Jul|Aug|Sep|Oct|Nov|Dec)\s+20\d{2})\b",
            page_text,
        )
        if m:
            published_date = parse_date_string(m.group(1))

    # Content — extract from <main>
    content_text = ""
    main = soup.find("main")
    container = main or soup.find("body")
    if container:
        for unwanted in container.find_all(
            ["nav", "header", "footer", "script", "style", "aside", "form"]
        ):
            unwanted.decompose()
        parts = [
            el.get_text(strip=True)
            for el in container.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td"])
            if len(el.get_text(strip=True)) > 15
        ]
        content_text = "\n\n".join(parts)

    return {
        "title": title,
        "published_date": published_date,
        "content_text": content_text,
    }


# =============================================================================
# AI SUMMARY
# =============================================================================


async def get_summary(text: str, title: str) -> str:
    base_url = os.environ.get("LLM_BASE_URL", "")
    api_key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "")

    if not base_url:
        _echo("  LLM_BASE_URL not set — skipping summary", err=True)
        return ""

    client = AsyncOpenAI(
        base_url=base_url,
        api_key=api_key or "not-needed",
        max_retries=2,
        timeout=120.0,
    )
    content_snippet = text[:4000] if text else title

    async with _get_llm_semaphore():
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": f"Summarise this ACRA item:\n\n{content_snippet}"},
                ],
            )
            try:
                _log_token_usage(
                    agent="sg-gov-newsrooms-zeeker",
                    endpoint=base_url,
                    model=model,
                    prompt_tokens=getattr(response.usage, "prompt_tokens", None),
                    completion_tokens=getattr(response.usage, "completion_tokens", None),
                    call_type="acra_summary",
                )
            except Exception:
                pass
            return (response.choices[0].message.content or "").strip()
        except Exception as e:
            _echo(f"  Summary failed: {e}", err=True)
            return ""


async def generate_summaries(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    tasks = [get_summary(item.get("content_text", ""), item.get("title", "")) for item in items]
    summaries = await asyncio.gather(*tasks, return_exceptions=True)
    for item, summary in zip(items, summaries):
        item["summary"] = "" if isinstance(summary, Exception) else summary
    return items


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================


def fetch_data(existing_table: Optional[Table]) -> List[Dict[str, Any]]:
    """
    Fetch new ACRA news announcement articles via sitemap discovery.

    Incremental: skips URLs already in DB and articles before START_DATE.
    """
    existing_urls: set = set()
    if existing_table:
        existing_urls = {row["source_url"] for row in existing_table.rows}
        _echo(f"Existing records: {len(existing_urls)}")

    results: List[Dict[str, Any]] = []
    consecutive_failures = 0
    skipped = 0
    failed = 0
    abort_reason: Optional[str] = None

    with httpx.Client(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        headers={
            "User-Agent": "ZeekerBot/1.0 (+https://data.zeeker.sg; sg-gov-newsrooms research bot)"
        },
        limits=httpx.Limits(max_connections=5, max_keepalive_connections=3),
    ) as client:
        try:
            new_urls = discover_urls_from_sitemap(client, existing_urls)
        except Exception as e:
            _echo(
                f"ABORTED (discovery failed: {type(e).__name__}: {e}) — 0 new, 0 failed",
                err=True,
            )
            raise Skip(f"discovery failed: {type(e).__name__}: {e}", kind="blocked")

        # Fetch publication dates from Isomer listing page RSC payload
        listing_dates = fetch_isomer_listing_dates(
            client, LISTING_URL, "/news-events/news-announcements/"
        )

        if not new_urls:
            _echo("No new URLs to process.")
            _echo("done — 0 new, 0 skipped, 0 failed")
            return []

        _echo(f"\nScraping {len(new_urls)} articles...")
        for i, url in enumerate(new_urls, 1):
            _echo(f"[{i}/{len(new_urls)}] {url}")
            polite_sleep()

            try:
                article = fetch_article(url, client)
                consecutive_failures = 0
            except Exception as e:
                _echo(f"  Failed: {e}", err=True)
                failed += 1
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    abort_reason = f"circuit breaker: {type(e).__name__}: {e}"
                    _echo(
                        f"circuit breaker tripped — {consecutive_failures} consecutive failures "
                        f"(last: {type(e).__name__}: {e})",
                        err=True,
                    )
                    break
                continue

            # Filter by date — prefer Isomer listing date over page scrape
            pub_date = listing_dates.get(url) or article.get("published_date")
            if pub_date:
                try:
                    if date.fromisoformat(pub_date) < START_DATE:
                        _echo(f"  Skipping (before {START_DATE}): {pub_date}")
                        skipped += 1
                        continue
                except ValueError:
                    pass
            elif pub_date is None:
                _echo("  Warning: no date found, including anyway", err=True)

            title = article.get("title", "")
            category = infer_category(url, title)

            result = {
                "id": make_id(url),
                "source_url": url,
                "category": category,
                "title": title,
                "published_date": pub_date,
                "content_text": article.get("content_text", ""),
                "summary": "",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            results.append(result)
            _echo(
                f"  → {title[:60]} "
                f"({pub_date}, {category}, {len(article.get('content_text',''))} chars)"
            )

    if results:
        _echo(f"\nGenerating summaries for {len(results)} articles...")
        results = asyncio.run(generate_summaries(results))
        summaries_ok = sum(1 for r in results if r.get("summary"))
        _echo(f"{summaries_ok} of {len(results)} summaries generated.")

    # Surface per-run counters on zeeker's status line / --json output
    global __zeeker_report__
    report: Dict[str, int] = {}
    if skipped:
        report["skipped"] = skipped
    if failed:
        report["failed"] = failed
    if report:
        __zeeker_report__ = report

    if abort_reason:
        _echo(
            f"ABORTED ({abort_reason}) — {len(results)} new, {failed} failed",
            err=True,
        )
        if not results:
            raise Skip(abort_reason, kind="blocked")
    else:
        _echo(f"done — {len(results)} new, {skipped} skipped, {failed} failed")
    return results


def transform_data(raw_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return raw_data
