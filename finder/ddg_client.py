"""Free, keyless web search via DuckDuckGo (the `ddgs` package).

Replaces the Google Search grounding path: Gemini's `google_search` tool
needs a paid/billed Google Cloud project even for light use, which kept
blocking on org policy, service-account binding, and prepay-credit walls.
DuckDuckGo needs no API key, no account, and no billing at all, and still
gives live, current search results -- including dedicated "X alternatives"
listicle pages (SaaSHub, SourceForge, G2, etc.) that are exactly the kind of
up-to-date source the LLM's own training data won't have.

This module only fetches raw search results; search.py layers a Firecrawl
fallback on top, and the LLM (openai_client) judges/extracts from them --
same search-then-extract split used everywhere else in this app.

DuckDuckGo throttles rapid back-to-back queries (it answers "No results
found"), so calls are paced and a failed query is retried once before giving
up. Degrades gracefully (returns []) if the package isn't installed or the
search keeps failing.
"""
import time

_MIN_INTERVAL_SECONDS = 1.0
_RETRY_DELAY_SECONDS = 2.5
_last_call_at = 0.0


def available() -> bool:
    try:
        import ddgs  # noqa: F401
        return True
    except ImportError:
        return False


def search(query: str, max_results: int = 8) -> list[dict]:
    """Returns [{"title", "url", "snippet"}, ...]. Never raises -- any
    failure (network, rate limit, parsing) just yields an empty list so
    callers can carry on without this source."""
    global _last_call_at
    try:
        from ddgs import DDGS
    except ImportError:
        print("[ddg] the 'ddgs' package isn't installed. Run: pip install ddgs")
        return []

    raw = None
    for attempt in range(2):
        wait = _MIN_INTERVAL_SECONDS - (time.monotonic() - _last_call_at)
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()
        try:
            raw = DDGS().text(query, max_results=max_results)
            break
        except Exception as e:
            if attempt == 1:
                print(f"[ddg] search failed for {query!r}: {e}")
                return []
            time.sleep(_RETRY_DELAY_SECONDS)

    return [
        {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
        for r in raw or []
        if r.get("href")
    ]
