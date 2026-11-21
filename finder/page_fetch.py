"""Fetches a page's readable text + links, preferring the free path.

Tries a plain `requests` GET first -- free, fast, no rate limit, and works for
the large majority of server-rendered pages. Only falls back to Firecrawl's
scrape (which handles JS-rendered pages and basic anti-bot measures) when the
free fetch fails outright or comes back suspiciously thin (a strong signal the
page needs JS to render). Keeping Firecrawl call volume low this way, combined
with firecrawl_client.py's rate limiting/caching/retry, keeps this within
Firecrawl's free-tier limits.

robots.txt is respected on BOTH paths: a page the site disallows is not fetched
for free and is not handed to Firecrawl either (finder/robots.py).
"""
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from . import robots
from .scrape_select import scrape_client as firecrawl_client

HEADERS = {"User-Agent": robots.USER_AGENT}
TIMEOUT = 12
MIN_USEFUL_CHARS = 300  # below this, assume the page needs JS and fall back to Firecrawl


def _free_fetch(url: str) -> dict | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException:
        return None
    if resp.status_code in (404, 410):
        # Definitive "this page doesn't exist" -- unlike a 403/timeout/thin
        # page, rendering it with Firecrawl can't help, so don't escalate.
        return {"markdown": "", "links": [], "not_found": True}
    if resp.status_code >= 400:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
    links = [urljoin(url, a["href"]) for a in soup.find_all("a", href=True) if a["href"].startswith(("http", "/"))]
    return {"markdown": text, "links": links}


def fetch(url: str) -> dict | None:
    """Returns {"markdown": str, "links": list[str]}: the free path if it got
    enough content, Firecrawl as fallback for JS-rendered/blocked pages,
    otherwise whatever the free path got (even if thin) beats nothing.
    None when the page doesn't exist or robots.txt disallows it."""
    if not robots.allowed(url):
        return None
    free = _free_fetch(url)
    if free and free.get("not_found"):
        return None
    if free and len(free["markdown"]) >= MIN_USEFUL_CHARS:
        return free

    if firecrawl_client.available():
        scraped = firecrawl_client.scrape(url)
        if scraped and scraped["markdown"]:
            return scraped

    return free
