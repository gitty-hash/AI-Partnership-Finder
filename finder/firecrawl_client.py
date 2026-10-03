"""Firecrawl client: centralized rate limiting, caching, and retry.

This is the ONLY place in the app that talks to Firecrawl. Every caller
(page_fetch.py's JS-rendering fallback, in turn used by discovery.py and
enrich.py) goes through the same shared throttle/semaphore/cache here, so
independent parts of the app can't stack up requests faster than the
configured rate -- there is exactly one rate limiter for the whole process,
not one per caller.

Architecture (per-request):
    cache check -> concurrency semaphore -> shared rate-limit throttle
    -> Firecrawl API -> success: cache + return
                      -> 429: Retry-After (if given) or exponential backoff
                         + jitter, retry through the same gate, up to
                         FIRECRAWL_MAX_RETRIES times -> log and give up

Upstream of this file, discovery.py/page_fetch.py already do the "don't call
Firecrawl unless needed" work: common URL paths are tried directly first, and
a free `requests` fetch is tried before Firecrawl's scrape (see
page_fetch.py) -- Firecrawl's scrape is reserved for pages that actually need
JS rendering or hit anti-bot measures. Firecrawl's own search() IS the
primary search backend (there's no other search API in this app). This
file's job is to make each call itself robust and coordinated, not to change
when a call happens.

Configuration (env vars, all optional -- defaults are conservative for a
free-tier Firecrawl account):
    FIRECRAWL_REQUESTS_PER_MINUTE  (default 8)
    FIRECRAWL_MAX_CONCURRENCY      (default 1 -- this app is single-threaded
                                     today; the semaphore is a no-op safety
                                     net if that ever changes)
    FIRECRAWL_MAX_RETRIES          (default 5)
    FIRECRAWL_CACHE_TTL            (seconds, default 86400 = 24h)
"""
import json
import logging
import os
import random
import re
import threading
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass  # python-dotenv not installed -- fall back to real env vars only

logger = logging.getLogger("finder.firecrawl")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("[firecrawl] %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

# ---- Configuration -----------------------------------------------------
REQUESTS_PER_MINUTE = float(os.environ.get("FIRECRAWL_REQUESTS_PER_MINUTE", 8))
MAX_CONCURRENCY = max(1, int(os.environ.get("FIRECRAWL_MAX_CONCURRENCY", 1)))
MAX_RETRIES = int(os.environ.get("FIRECRAWL_MAX_RETRIES", 5))
CACHE_TTL_SECONDS = float(os.environ.get("FIRECRAWL_CACHE_TTL", 86400))

MIN_INTERVAL_SECONDS = 60.0 / REQUESTS_PER_MINUTE
CACHE_PATH = Path(__file__).resolve().parent.parent / ".cache" / "firecrawl_cache.json"

_client = None
_disabled_reason = None

# ---- Centralized rate limiting / concurrency (module-level = shared across
# every caller in the process, synchronous or not) -----------------------
_rate_lock = threading.Lock()
_last_call_at = 0.0
_concurrency_sem = threading.Semaphore(MAX_CONCURRENCY)

# ---- Simple TTL cache, keyed by request, backed by a JSON file so it
# survives across runs -----------------------------------------------------
_cache_lock = threading.Lock()
_cache: dict[str, dict] = {}
_cache_loaded = False


def _load_cache() -> None:
    global _cache, _cache_loaded
    if _cache_loaded:
        return
    _cache_loaded = True
    try:
        if CACHE_PATH.exists():
            _cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        _cache = {}


def _save_cache() -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(_cache), encoding="utf-8")
    except Exception:
        pass  # the cache is a pure optimization -- never let it break a run


def _cache_get(key: str):
    _load_cache()
    with _cache_lock:
        entry = _cache.get(key)
    if not entry or (time.time() - entry["cached_at"] > CACHE_TTL_SECONDS):
        return None
    return entry["data"]


def _cache_set(key: str, data) -> None:
    _load_cache()
    with _cache_lock:
        _cache[key] = {"data": data, "cached_at": time.time()}
        _save_cache()


# ---- Client init ---------------------------------------------------------
def _get_client():
    global _client, _disabled_reason
    if _client is not None or _disabled_reason is not None:
        return _client

    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        _disabled_reason = (
            "FIRECRAWL_API_KEY not set. Add it to a .env file in the project root "
            "(FIRECRAWL_API_KEY=fc-...) to enable the JS-rendering fallback fetch."
        )
        logger.info(_disabled_reason)
        return None

    try:
        from firecrawl import Firecrawl
    except ImportError:
        _disabled_reason = "The 'firecrawl' package isn't installed. Run: pip install -r requirements.txt"
        logger.info(_disabled_reason)
        return None

    _client = Firecrawl(api_key=api_key)  # never log api_key
    return _client


def available() -> bool:
    return _get_client() is not None


# ---- Rate limit + retry machinery ----------------------------------------
def _throttle() -> None:
    """The single shared pacing gate every Firecrawl call passes through,
    regardless of which function or module called it -- this is what makes
    the rate limit centralized instead of per-caller."""
    global _last_call_at
    with _rate_lock:
        wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _last_call_at)
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()


def _parse_retry_after(error_message: str) -> float | None:
    m = re.search(r"retry after (\d+)", error_message, re.IGNORECASE)
    return float(m.group(1)) + 1 if m else None


def _is_rate_limit_error(error_message: str) -> bool:
    low = error_message.lower()
    return "rate limit" in low or "429" in low


def _call_with_retry(fn, *, what: str):
    """Runs fn() under the concurrency semaphore and shared throttle. On a
    429, honors a Retry-After hint if Firecrawl gave one, otherwise backs off
    exponentially (2s, 4s, 8s, 16s, ...) with jitter so concurrent retries
    don't all land at once. Gives up after FIRECRAWL_MAX_RETRIES."""
    for attempt in range(MAX_RETRIES + 1):
        with _concurrency_sem:
            _throttle()
            try:
                result = fn()
                if attempt > 0:
                    logger.info(f"Firecrawl request succeeded ({what}) after {attempt} retr{'y' if attempt == 1 else 'ies'}")
                return result
            except Exception as e:
                msg = str(e)
                if not _is_rate_limit_error(msg):
                    raise
                if attempt >= MAX_RETRIES:
                    logger.warning(f"Maximum Firecrawl retries reached for {what} -- giving up")
                    raise
                retry_after = _parse_retry_after(msg)
                wait = retry_after if retry_after is not None else (2 ** (attempt + 1))
                wait += random.uniform(0, wait * 0.25)  # jitter
                logger.info(f"Firecrawl rate limit reached on {what}; retrying in {wait:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
    return None  # unreachable -- loop always returns or raises


# ---- Public API -----------------------------------------------------------
def search(query: str, limit: int = 5) -> list[dict]:
    """Web search. Returns a list of {title, url, description}, best-effort.

    Empty list on any failure (missing key, network error, retries
    exhausted) rather than raising -- callers should treat search as
    optional, not a hard dependency. This is the app's only search backend
    (discovery.py and enrich.py both call this directly).
    """
    cache_key = f"search::{query}::{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.info(f"Using cached Firecrawl result for search '{query}'")
        return cached

    client = _get_client()
    if client is None:
        return []
    logger.info(f"Firecrawl request queued: search '{query}'")
    try:
        result = _call_with_retry(lambda: client.search(query, limit=limit), what=f"search '{query}'")
    except Exception as e:
        logger.warning(f"Firecrawl search failed for '{query}': {e}")
        return []

    web_results = getattr(result, "web", None)
    if web_results is None and isinstance(result, dict):
        web_results = result.get("web", [])
    out = []
    for r in web_results or []:
        if isinstance(r, dict):
            out.append({"title": r.get("title", ""), "url": r.get("url", ""), "description": r.get("description", "")})
        else:
            out.append({
                "title": getattr(r, "title", ""),
                "url": getattr(r, "url", ""),
                "description": getattr(r, "description", ""),
            })
    out = [r for r in out if r["url"]]
    _cache_set(cache_key, out)
    return out


def scrape(url: str) -> dict | None:
    """Scrape a single URL. Returns {"markdown": str, "links": list[str]} or
    None on failure (missing key, network error, retries exhausted)."""
    cache_key = f"scrape::{url}"
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.info(f"Using cached Firecrawl result for {url}")
        return cached

    client = _get_client()
    if client is None:
        return None
    logger.info(f"Firecrawl request queued: scrape {url}")
    try:
        result = _call_with_retry(lambda: client.scrape(url, formats=["markdown", "links"]), what=f"scrape {url}")
    except Exception as e:
        logger.warning(f"Firecrawl scrape failed for {url}: {e}")
        return None

    markdown = getattr(result, "markdown", None)
    links = getattr(result, "links", None)
    if markdown is None and isinstance(result, dict):
        markdown = result.get("markdown", "")
        links = result.get("links", [])
    data = {"markdown": markdown or "", "links": links or []}
    _cache_set(cache_key, data)
    return data
