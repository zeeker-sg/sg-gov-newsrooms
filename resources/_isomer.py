"""Shared helpers for Isomer CMS (Next.js/RSC) government sites."""

import json
import re
from typing import Any, Dict, List
from urllib.parse import urljoin

import click
import httpx


def normalize_url(url: str) -> str:
    # Isomer RSC payload paths have historically flipped between with/without
    # trailing slash; canonicalize for dedup so a format change can't create
    # duplicate rows for the same article.
    return url.rstrip("/")


def parse_isomer_listing_dates(html: str) -> Dict[str, str]:
    """Extract {path: ISO date} from Isomer RSC payload in listing page HTML."""
    dates: Dict[str, str] = {}
    text = html.replace('\\"', '"')
    for m in re.finditer(
        r'"id":"([^"]+)","date":"\$D(\d{4}-\d{2}-\d{2})T[^"]*"',
        text,
    ):
        dates[m.group(1)] = m.group(2)
    return dates


# Next.js streams the RSC payload as JS string literals inside
# ``self.__next_f.push([1,"..."])`` calls; each literal is valid JSON.
_RSC_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
_ITEM_START_RE = re.compile(r'\{"id":"')
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _decode_rsc_payload(html: str) -> str:
    """Return the decoded RSC payload text from a Next.js page.

    Falls back to the input unchanged when it has no ``__next_f`` pushes
    (e.g. it is already a bare payload).
    """
    chunks = []
    for literal in _RSC_PUSH_RE.findall(html):
        try:
            chunks.append(json.loads(literal))
        except ValueError:
            continue
    return "".join(chunks) if chunks else html


def _item_category(obj: Dict[str, Any]) -> str:
    """Category of a listing item.

    Current Isomer: the ``tags`` group named "Category" (``plaintextTags`` is
    empty on some sites, e.g. ACRA). Multiple selections are joined with ", ".
    Older payloads carried a flat ``category`` string.
    """
    for tag in obj.get("tags") or []:
        if isinstance(tag, dict) and tag.get("category") == "Category":
            selected = [s for s in tag.get("selected") or [] if isinstance(s, str)]
            if selected:
                return ", ".join(selected)
    category = obj.get("category")
    return category if isinstance(category, str) else ""


def parse_isomer_listing_items(html: str, path_prefix: str) -> List[Dict[str, str]]:
    """Extract article items from Isomer RSC payload.

    Parses the payload structurally: decodes the RSC string chunks, then
    JSON-decodes every object that starts with ``{"id":...`` and keeps those
    that look like listing items (an ``id`` path, a ``$D``-prefixed date and a
    title). Field order inside the object no longer matters, so additions such
    as ``plaintextTags`` don't break discovery.

    Returns list of dicts with: path, date, title, category (deduplicated by
    path, in page order).
    """
    text = _decode_rsc_payload(html)
    decoder = json.JSONDecoder()
    items: Dict[str, Dict[str, str]] = {}
    for m in _ITEM_START_RE.finditer(text):
        try:
            obj, _ = decoder.raw_decode(text, m.start())
        except ValueError:
            continue
        path = obj.get("id")
        raw_date = obj.get("date")
        title = obj.get("title")
        if not (
            isinstance(path, str)
            and isinstance(raw_date, str)
            and raw_date.startswith("$D")
            and isinstance(title, str)
        ):
            continue
        iso_date = raw_date[2:12]
        if not _ISO_DATE_RE.fullmatch(iso_date):
            continue
        if not path.startswith(path_prefix) or path in items:
            continue
        items[path] = {
            "path": path,
            "date": iso_date,
            "category": _item_category(obj),
            "title": title,
        }
    return list(items.values())


def fetch_isomer_listing_dates(
    client: httpx.Client,
    listing_url: str,
    path_prefix: str,
) -> Dict[str, str]:
    """Fetch an Isomer listing page and extract publication dates.

    Returns dict mapping full URLs to ISO date strings (YYYY-MM-DD).
    """
    try:
        response = client.get(listing_url)
        response.raise_for_status()
    except httpx.HTTPError as e:
        click.echo(f"Failed to fetch Isomer listing dates from {listing_url}: {e}", err=True)
        return {}

    path_dates = parse_isomer_listing_dates(response.text)

    result: Dict[str, str] = {}
    for path, date_str in path_dates.items():
        if path.startswith(path_prefix):
            full_url = urljoin(listing_url, path)
            result[full_url] = date_str

    click.echo(f"Isomer listing dates: {len(result)} articles with dates from {listing_url}")
    return result
