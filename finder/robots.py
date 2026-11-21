"""robots.txt checks shared by every fetcher in the app.

One cached parser per host (a run touches the same sites again and again), a
hard timeout on the robots.txt download (urllib's own reader has none and can
hang a whole run), and one rule everywhere: a page robots.txt disallows is not
fetched at all -- not by the free fetch, and not by Firecrawl either.
"""
import threading
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

USER_AGENT = "Mozilla/5.0 (compatible; WaresportPartnershipFinder/0.1)"
ROBOTS_TIMEOUT = 8
CACHE_TTL_SECONDS = 6 * 60 * 60

_lock = threading.Lock()
_cache: dict[str, tuple[float, RobotFileParser | None]] = {}


def _load(robots_url: str) -> RobotFileParser | None:
    """A parser for the site's robots.txt, or None when there is no usable file
    (missing, unreachable, server error): no rules means everything is allowed."""
    try:
        resp = requests.get(robots_url, headers={"User-Agent": USER_AGENT}, timeout=ROBOTS_TIMEOUT)
    except requests.RequestException:
        return None
    if resp.status_code in (401, 403):
        # The standard reading: an access-controlled robots.txt means "disallow all".
        parser = RobotFileParser()
        parser.disallow_all = True
        return parser
    if resp.status_code >= 400:
        return None
    parser = RobotFileParser()
    parser.parse(resp.text.splitlines())
    return parser


def allowed(url: str) -> bool:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return False
    key = f"{parsed.scheme}://{parsed.netloc}".lower()
    now = time.monotonic()
    with _lock:
        cached = _cache.get(key)
    if cached is None or now - cached[0] > CACHE_TTL_SECONDS:
        parser = _load(f"{key}/robots.txt")
        with _lock:
            _cache[key] = (now, parser)
    else:
        parser = cached[1]
    return True if parser is None else parser.can_fetch(USER_AGENT, url)


def clear_cache() -> None:
    with _lock:
        _cache.clear()
