"""Drop-in replacement for firecrawl_client.py's scrape()/search() -- same
function names/signatures/return shapes -- backed by Crawl4AI instead of
Firecrawl.

Two backends, auto-selected:
  - Hosted (api.crawl4ai.com), used when CRAWL4AI_API_KEY is set in .env.
    Cheap (credit-based, ~$0.0002/page, more for hard/bot-protected sites),
    includes proxy handling so it gets through Cloudflare-style JS
    challenges that defeat a plain local browser, and has a real Google-
    backed /search endpoint -- so this one backend can replace BOTH of
    Firecrawl's search() and scrape() jobs.
  - Local (no key needed), a free headless Chromium browser via Playwright.
    Works on most sites but fails on Cloudflare-protected ones (verified
    directly: SourceForge/Capterra's Waresport-alternatives pages blocked
    it with "Blocked by anti-bot protection: Cloudflare JS challenge").
    Search isn't native to Crawl4AI locally, so it's backed by the already-
    integrated free DuckDuckGo client instead.

Built for a one-off comparison run, selected via the SCRAPE_PROVIDER env var
(see scrape_select.py) -- not a permanent default.
"""
import asyncio
import os
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from . import ddg_client

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

HOSTED_BASE_URL = "https://api.crawl4ai.com"
TIMEOUT = 30


def _api_key() -> str | None:
    return os.environ.get("CRAWL4AI_API_KEY")


def available() -> bool:
    if _api_key():
        return True
    try:
        import crawl4ai  # noqa: F401
        return True
    except ImportError:
        return False


# ---- Hosted (api.crawl4ai.com) --------------------------------------------
def _hosted_scrape(url: str) -> dict | None:
    try:
        resp = requests.post(
            f"{HOSTED_BASE_URL}/scrape",
            headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"},
            json={"url": url},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[crawl4ai] hosted scrape failed for {url}: {e}")
        return None

    if not data.get("ok", True) and not data.get("markdown"):
        return None

    links = []
    html = data.get("html", "")
    if html:
        soup = BeautifulSoup(html, "html.parser")
        links = [a["href"] for a in soup.find_all("a", href=True) if a["href"].startswith(("http", "/"))]

    return {"markdown": data.get("markdown", "") or "", "links": links}


def _hosted_search(query: str, limit: int) -> list[dict]:
    try:
        resp = requests.get(
            f"{HOSTED_BASE_URL}/search",
            headers={"Authorization": f"Bearer {_api_key()}"},
            params={"q": query},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[crawl4ai] hosted search failed for {query!r}: {e}")
        return []

    results = data.get("results", [])[:limit]
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "description": r.get("snippet", "")} for r in results if r.get("url")]


# ---- Local (free, no key, Playwright-based) --------------------------------
async def _local_scrape_async(url: str) -> dict | None:
    from crawl4ai import AsyncWebCrawler

    async with AsyncWebCrawler() as crawler:
        result = await crawler.arun(url=url)
        if not result or not result.success:
            return None
        markdown = str(result.markdown) if result.markdown else ""
        links = []
        link_data = result.links or {}
        for group in ("internal", "external"):
            for link in link_data.get(group, []):
                href = link.get("href") if isinstance(link, dict) else None
                if href:
                    links.append(href)
        return {"markdown": markdown, "links": links}


def _local_scrape(url: str) -> dict | None:
    try:
        return asyncio.run(_local_scrape_async(url))
    except Exception as e:
        print(f"[crawl4ai] local scrape failed for {url}: {e}")
        return None


# ---- Public API (matches firecrawl_client.py exactly) ----------------------
def scrape(url: str) -> dict | None:
    if _api_key():
        return _hosted_scrape(url)
    return _local_scrape(url)


def search(query: str, limit: int = 5) -> list[dict]:
    if _api_key():
        return _hosted_search(query, limit)
    # No native local search in Crawl4AI -- fall back to free DuckDuckGo.
    results = ddg_client.search(query, max_results=limit)
    return [{"title": r["title"], "url": r["url"], "description": r["snippet"]} for r in results]
