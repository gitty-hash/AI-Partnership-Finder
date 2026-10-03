"""Picks which scraping backend module backs "firecrawl_client" calls
throughout page_fetch.py/discovery.py/enrich.py/pipeline.py, based on the
SCRAPE_PROVIDER env var.

Set SCRAPE_PROVIDER=crawl4ai in .env (or `set SCRAPE_PROVIDER=crawl4ai`
before running) to run the whole pipeline on Crawl4AI (free, local,
Playwright-based) instead of Firecrawl for a side-by-side comparison --
crawl4ai_client.py mirrors firecrawl_client.py's scrape()/search()/
available() names/signatures, so no other code needs to change. Defaults to
Firecrawl.

Note: Firecrawl's rate-limiting/caching/retry machinery in firecrawl_client.py
only applies when Firecrawl is the active provider -- Crawl4AI has no paid
API to rate-limit against (it's your own local browser), so it doesn't need
that layer.
"""
import os

from . import crawl4ai_client, firecrawl_client

scrape_client = crawl4ai_client if os.environ.get("SCRAPE_PROVIDER", "").strip().lower() == "crawl4ai" else firecrawl_client
