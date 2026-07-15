"""
Personal Data Protection Commission Singapore press room resource.

Cadence: Daily (Tier 1)
Source: https://www.pdpc.gov.sg/media-events  (formerly /news-and-events/press-room)
Strategy: Sitemap-driven discovery -- fetch /sitemap.xml, take all /media-events/*
  URLs, filter against existing rows, fetch each detail page, parse title/date/
  category from the rendered HTML and the article body from the Next.js RSC
  streaming payload.

Site migrated April 2026 from GovTech CWP (ASP.NET, JSON API + CSRF) to GovTech
Optical (Next.js App Router on S3+CloudFront, SSG). The old API and CSRF flow
are gone -- there's no live JSON listing endpoint -- and CloudFront returns 403
to data-centre source IPs, so the build runs through the host's tailscale
sidecar (TAILSCALE_PROXY) to exit via houfu's Mac.

Detail pages have title/date/category in the rendered HTML, but the article
body is empty in SSR HTML -- it lives in the streaming RSC payload as a text
row referenced by `"content":"$<id>"`.

Licensing: Government of Singapore, all rights reserved. Content stored but NOT
shown by default in Datasette UI; see zeeker.toml for column config.
"""

import asyncio
import hashlib
import json
import os
import random
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

import click
import httpx
from bs4 import BeautifulSoup
from openai import AsyncOpenAI
from sqlite_utils.db import Table
from tenacity import retry, stop_after_attempt, wait_exponential

try:
    from ._token_usage import _log_token_usage
except ImportError:
    from pathlib import Path as _P
    import sys as _sys
    _sys.path.insert(0, str(_P(__file__).resolve().parent))
    from _token_usage import _log_token_usage

# =============================================================================
# CONFIGURATION
# =============================================================================

BASE_URL = "https://www.pdpc.gov.sg"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"
MEDIA_EVENTS_PATH = "/media-events/"

# Only keep articles published from this date onwards
START_DATE = date(2026, 1, 1)

# Scraping rate limits (Tier 1 -- daily incremental, be polite)
REQUEST_DELAY_BASE = 1.5
REQUEST_DELAY_JITTER = 0.5
REQUEST_TIMEOUT = 30.0
MAX_CONSECUTIVE_FAILURES = 5
MAX_RETRIES = 3

# Mac's residential IP rate-limit awareness: keep a single connection in flight
# per fetch round. Concurrent loads through the SOCKS5 sidecar all egress via
# Mac, so politeness here also protects the upstream Tailscale exit.
HTTP_LIMITS = httpx.Limits(max_connections=2, max_keepalive_connections=2)

# Browser-shaped UA -- CloudFront also fingerprints the agent string
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.4 Safari/605.1.15"
)

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
As an expert in Singapore data protection law and privacy regulation, provide concise
summaries of PDPC press releases, enforcement decisions, speeches, and advisories for
legal practitioners and privacy professionals. Highlight key PDPA enforcement outcomes,
financial penalties, regulatory guidance, policy developments, or advisory updates.
Write 1 narrative paragraph, no longer than 100 words. Focus on what happened, who is
affected, and why it matters for data protection compliance in Singapore.
"""

# =============================================================================
# HELPERS
# =============================================================================


def make_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:12]


def polite_sleep():
    delay = REQUEST_DELAY_BASE + random.uniform(-REQUEST_DELAY_JITTER, REQUEST_DELAY_JITTER)
    time.sleep(max(0.5, delay))


def slugify_category(type_str: str) -> str:
    """Slugify category to slug (e.g., 'Press Room' -> 'press-room')."""
    return re.sub(r"[^a-z0-9]+", "-", type_str.strip().lower()).strip("-")


def parse_date_string(date_str: str) -> Optional[str]:
    """Parse 'DD Mon YYYY' or 'DD Month YYYY' to ISO date string."""
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
                return datetime.strptime(
                    f"{m.group(1)} {m.group(2)} {m.group(3)}", fmt
                ).date().isoformat()
            except ValueError:
                continue
    return None


# =============================================================================
# SITEMAP DISCOVERY
# =============================================================================


# A trailing year in the slug (e.g. ".../on-9-april-2026", ".../webinar-2022")
# is a strong hint at the article year. Used to skip obvious pre-START_DATE
# entries without fetching them. Slugs without a year still get fetched and
# date-filtered properly via the on-page date.
_SLUG_YEAR_RE = re.compile(r"-(20\d{2})(?:[-/]|$)")


def _max_year_in_slug(url: str) -> Optional[int]:
    years = [int(m.group(1)) for m in _SLUG_YEAR_RE.finditer(url)]
    return max(years) if years else None


@retry(stop=stop_after_attempt(MAX_RETRIES), wait=wait_exponential(multiplier=2, min=1, max=10))
def fetch_sitemap_urls(client: httpx.Client) -> List[str]:
    """Fetch the site sitemap and return all /media-events/<slug> URLs."""
    click.echo(f"Fetching sitemap: {SITEMAP_URL}")
    resp = client.get(SITEMAP_URL)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls: List[str] = []
    for u in root.findall("s:url", ns):
        loc = u.findtext("s:loc", namespaces=ns) or ""
        # /media-events/<slug> only -- skip the listing root /media-events
        if MEDIA_EVENTS_PATH in loc and not loc.rstrip("/").endswith("/media-events"):
            urls.append(loc)
    click.echo(f"Sitemap returned {len(urls)} /media-events/* URLs")
    return urls


# =============================================================================
# DETAIL PAGE PARSING
# =============================================================================


# Pattern for a single self.__next_f.push([N, "<escaped string>"]) call.
# Captures the escaped string -- caller json-decodes it to the streamed text.
_NEXT_F_PUSH_RE = re.compile(
    r'self\.__next_f\.push\(\[\s*\d+\s*,\s*"((?:[^"\\]|\\.)*)"\s*\]\)'
)


def _decode_rsc_stream(html: str) -> str:
    """Concatenate all self.__next_f.push string payloads.

    Next.js App Router emits the RSC stream as a series of these calls. The
    string args are JSON-escaped fragments; concat in document order to get
    the full stream that the client would have received.
    """
    parts: List[str] = []
    for esc in _NEXT_F_PUSH_RE.findall(html):
        try:
            parts.append(json.loads('"' + esc + '"'))
        except json.JSONDecodeError:
            continue
    return "".join(parts)


# Streamed text row marker: "<rowId>:T<hexLen>,<content>" terminated by the
# next row marker (one or more digits followed by a colon at line start) or
# end of stream. Used to fish the article body out of the RSC payload.
_RSC_TEXT_ROW_RE_TEMPLATE = (
    r"(?:^|\n){row}:T[0-9a-f]+,(.*?)(?=\n\d+:[\[\"{{T]|\Z)"
)


def _extract_article_body_html(rsc: str) -> str:
    """Extract the article body HTML from the streamed RSC payload.

    Optical detail pages render `<div className="rte"><RscRef content="$N"/></div>`
    where row N is a streamed text chunk holding the body HTML. We find the
    first non-cookie content reference and pull its row.
    """
    for ref_id in re.findall(r'"content":"\$([\w]+)"', rsc):
        row_re = re.compile(
            _RSC_TEXT_ROW_RE_TEMPLATE.format(row=re.escape(ref_id)),
            re.DOTALL,
        )
        m = row_re.search(rsc)
        if not m:
            continue
        body = m.group(1).strip()
        # Skip the cookie-banner content that also lives in a content ref
        if "cookie" in body.lower() and len(body) < 500:
            continue
        return body
    return ""


def _html_body_to_text(body_html: str) -> str:
    """Convert the article body HTML to plain text (paragraph-separated)."""
    if not body_html:
        return ""
    soup = BeautifulSoup(body_html, "lxml")
    parts: List[str] = []
    for el in soup.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td"]):
        text = el.get_text(strip=True)
        if len(text) > 15:
            parts.append(text)
    return "\n\n".join(parts)


def parse_detail_page(html: str, url: str) -> Dict[str, Any]:
    """Parse a /media-events/[slug] page.

    Returns a dict with title, date_text, published_date, category_name,
    category_slug, content_html, content_text. Missing fields default to ''
    or None so callers can decide how to handle.
    """
    soup = BeautifulSoup(html, "lxml")

    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""

    # Two layouts on the new site:
    #   - News-shaped pages (Announcements, Press Room, Advisories, Reports):
    #     <span class="page-banner__date">Published on 14 Apr 2026</span>
    #   - Event pages (Events / Webinars):
    #     <div class="page-banner__event-date">
    #       <span>icon</span><span>08 Apr 2021</span>
    #     </div>
    date_text = ""
    pub_date: Optional[str] = None
    date_text_source: Any = soup.find("span", class_="page-banner__date")
    if date_text_source is None:
        date_text_source = soup.find("div", class_="page-banner__event-date")
    if date_text_source is not None:
        full = date_text_source.get_text(" ", strip=True)
        m = re.search(r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})", full)
        if m:
            date_text = m.group(1)
            pub_date = parse_date_string(date_text)

    # <div class="page-banner__category"><span>Announcements</span></div>
    category_name = ""
    cat_div = soup.find("div", class_="page-banner__category")
    if cat_div:
        span = cat_div.find("span")
        if span:
            category_name = span.get_text(strip=True)
    # Fallback: schema.org JSON-LD type
    if not category_name:
        for s in soup.find_all("script", type="application/ld+json"):
            try:
                d = json.loads(s.string or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            ctx = d.get("@context", "") or ""
            if "schema.gov.sg" in ctx:
                category_name = (d.get("type") or "").strip()
                if category_name:
                    break

    rsc = _decode_rsc_stream(html)
    body_html = _extract_article_body_html(rsc)
    body_text = _html_body_to_text(body_html)

    return {
        "url": url,
        "title": title,
        "date_text": date_text,
        "published_date": pub_date,
        "category_name": category_name,
        "category_slug": slugify_category(category_name) if category_name else "",
        "content_html": body_html,
        "content_text": body_text,
    }


@retry(stop=stop_after_attempt(MAX_RETRIES), wait=wait_exponential(multiplier=2, min=1, max=10))
def fetch_detail_page(url: str, client: httpx.Client) -> str:
    """Fetch the raw HTML of a /media-events/[slug] page."""
    resp = client.get(url)
    resp.raise_for_status()
    return resp.text


# =============================================================================
# AI SUMMARY
# =============================================================================


async def get_summary(text: str, title: str) -> str:
    base_url = os.environ.get("LLM_BASE_URL", "")
    api_key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "")

    if not base_url:
        click.echo("  LLM_BASE_URL not set -- skipping summary", err=True)
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
                    {"role": "user", "content": f"Summarise this PDPC item:\n\n{content_snippet}"},
                ],
            )
            try:
                _log_token_usage(
                    agent="sg-gov-newsrooms-zeeker",
                    endpoint=base_url,
                    model=model,
                    prompt_tokens=getattr(response.usage, "prompt_tokens", None),
                    completion_tokens=getattr(response.usage, "completion_tokens", None),
                    call_type="pdpc_summary",
                )
            except Exception:
                pass
            return (response.choices[0].message.content or "").strip()
        except Exception as e:
            click.echo(f"  Summary failed: {e}", err=True)
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
    """Fetch new PDPC media-events articles via sitemap discovery.

    Incremental: skips URLs already in DB and articles published before
    START_DATE. The 2 legacy CWP rows (/news-and-events/press-room/...) won't
    match any new sitemap URL and will be left as historical orphans.
    """
    existing_urls: set = set()
    if existing_table:
        existing_urls = {row["source_url"] for row in existing_table.rows}
        click.echo(f"Existing records: {len(existing_urls)}")

    # PDPC's CDN does IP-based routing that 403s data-centre IPs. When the
    # host SOCKS5 sidecar is up, TAILSCALE_PROXY is set in the build env and
    # routes this fetch via houfu's Mac. When unset (offline/local-dev),
    # httpx behaves as before -- and PDPC will return 403, signalling the
    # sidecar is down.
    proxy = os.environ.get("TAILSCALE_PROXY") or None
    if proxy:
        click.echo(f"Routing PDPC fetches via {proxy}")

    results: List[Dict[str, Any]] = []
    consecutive_failures = 0

    with httpx.Client(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        proxy=proxy,
        headers={"User-Agent": USER_AGENT},
        limits=HTTP_LIMITS,
    ) as client:
        try:
            sitemap_urls = fetch_sitemap_urls(client)
        except Exception as e:
            click.echo(f"Failed to fetch sitemap: {e}", err=True)
            return []

        candidate_urls = [u for u in sitemap_urls if u not in existing_urls]
        # Cheap pre-filter: skip URLs whose slug carries a year before
        # START_DATE.year. Slugs without a year fall through to the per-page
        # date check.
        pre_filtered: List[str] = []
        skipped_by_slug = 0
        for u in candidate_urls:
            year = _max_year_in_slug(u)
            if year is not None and year < START_DATE.year:
                skipped_by_slug += 1
                continue
            pre_filtered.append(u)
        click.echo(
            f"Sitemap candidates: {len(candidate_urls)} new (of {len(sitemap_urls)}); "
            f"slug-year filter skipped {skipped_by_slug}; will fetch {len(pre_filtered)}"
        )

        if not pre_filtered:
            click.echo("No new items to process.")
            return []
        new_urls = pre_filtered

        for i, url in enumerate(new_urls, 1):
            click.echo(f"[{i}/{len(new_urls)}] {url}")
            polite_sleep()

            try:
                html = fetch_detail_page(url, client)
                consecutive_failures = 0
            except Exception as e:
                click.echo(f"  Failed to fetch: {e}", err=True)
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    click.echo(
                        "Circuit breaker triggered -- too many consecutive failures.",
                        err=True,
                    )
                    break
                continue

            parsed = parse_detail_page(html, url)
            pub_date = parsed["published_date"]

            if pub_date:
                try:
                    if date.fromisoformat(pub_date) < START_DATE:
                        click.echo(f"  Skipping (before {START_DATE}): {pub_date}")
                        continue
                except ValueError:
                    pass
            else:
                click.echo("  Warning: no published date parsed, including anyway")

            if not parsed["title"]:
                click.echo("  Skipping: no <h1> title found")
                continue

            content_text = parsed["content_text"]
            if not content_text:
                click.echo(
                    "  Warning: empty body (RSC content stream not found); "
                    "saving with title-only context"
                )

            result = {
                "id": make_id(url),
                "source_url": url,
                "category": parsed["category_slug"] or "other",
                "title": parsed["title"],
                "published_date": pub_date,
                "content_text": content_text,
                "summary": "",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            results.append(result)
            click.echo(
                f"  -> {parsed['title'][:60]} "
                f"({pub_date}, {parsed['category_slug']}, {len(content_text)} chars)"
            )

    if not results:
        click.echo("No articles scraped.")
        return []

    click.echo(f"\nGenerating summaries for {len(results)} articles...")
    results = asyncio.run(generate_summaries(results))

    summaries_ok = sum(1 for r in results if r.get("summary"))
    click.echo(f"\nDone: {len(results)} new articles, {summaries_ok} with summaries.")
    return results


def transform_data(raw_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return raw_data
