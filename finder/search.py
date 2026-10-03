"""Unified web search for the whole app: DuckDuckGo first (free, no credits),
Firecrawl's search only as a fallback when DuckDuckGo comes back empty.

DuckDuckGo has no official API and occasionally returns nothing under rapid
back-to-back queries, so a fallback keeps a flaky lookup from silently turning
into "no result" -- while still keeping Firecrawl credits for the work only it
does well (scraping pages, including JS-rendered ones).

Returns [{"title", "url", "description"}, ...] -- the same shape
firecrawl_client.search() always returned, so callers don't care which
backend answered.
"""
from . import ddg_client
from .scrape_select import scrape_client as firecrawl_client

_cache: dict[tuple[str, int], list[dict]] = {}


def web_search(query: str, limit: int = 5) -> list[dict]:
    key = (query, limit)
    if key in _cache:
        return _cache[key]

    results: list[dict] = []
    if ddg_client.available():
        results = [
            {"title": r["title"], "url": r["url"], "description": r["snippet"]}
            for r in ddg_client.search(query, max_results=limit)
        ]

    if not results and firecrawl_client.available():
        results = firecrawl_client.search(query, limit=limit)

    if results:
        _cache[key] = results
    return results
