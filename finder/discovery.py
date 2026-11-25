"""Dynamic competitor + integration discovery, powered by DuckDuckGo (search),
Firecrawl (page scraping, via page_fetch.py) and OpenAI (knowledge questions,
judgment, and structured extraction).

Division of labor:
  - DuckDuckGo: searching the web (via search.py -- free, with Firecrawl's
    search as a fallback only when DuckDuckGo comes back empty).
  - Firecrawl: scraping a specific URL when the free requests fetch in
    page_fetch.py can't (JS-rendered/blocked pages).
  - OpenAI: "who competes with X" (a knowledge question no scrape can answer),
    "which of these search results is the real company" (judgment), and
    "pull the partner names out of this messy page" (structured extraction
    that has to generalize across totally different page layouts -- cards,
    logo widgets, plain prose -- which no single regex can).

Scope, on purpose (see README): this does ONE hop of "competitors of
competitors" -- it does not recurse indefinitely. Everything here is
best-effort (an LLM can still be wrong, a search result can still be
irrelevant), so results should be reviewed, same as the heuristics in
enrich.py.
"""
from urllib.parse import urlparse

from . import page_fetch
from .llm_select import llm_client as openai_client
from .models import Partner
from .scrape_select import scrape_client as firecrawl_client
from .search import web_search

_LLM_PROVIDER_LABEL = "Gemini" if openai_client.__name__.endswith("gemini_client") else "OpenAI"
_SCRAPE_PROVIDER_LABEL = "Crawl4AI" if firecrawl_client.__name__.endswith("crawl4ai_client") else "Firecrawl"

WARESPORT_DESCRIPTION = (
    "Waresport (waresport.com) is an all-in-one sports club management software "
    "platform for youth/multi-sport club directors (roughly 80-800 athletes): "
    "registrations, payments, scheduling, tryouts, parent communications, plus "
    "facility and court booking for clubs that also run their own venues."
)

# Domains that show up constantly in search results but are never a
# competitor's or partner's own site -- review aggregators, social platforms,
# reference sites. Used to sanity-check an LLM's website pick and as a last-
# resort filter when the LLM itself is unavailable.
NON_COMPANY_DOMAINS = {
    "g2.com", "capterra.com", "getapp.com", "softwareadvice.com", "trustpilot.com",
    "wikipedia.org", "youtube.com", "youtu.be", "linkedin.com", "facebook.com",
    "instagram.com", "twitter.com", "x.com", "reddit.com", "crunchbase.com",
    "producthunt.com", "medium.com", "quora.com", "forbes.com", "businessofapps.com",
    "waresport.com",
}


def _root_domain(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def _looks_like_company_domain(url: str) -> bool:
    domain = _root_domain(url)
    return bool(domain) and domain not in NON_COMPANY_DOMAINS


def _base_domain(url: str) -> str:
    """Registrable-domain heuristic: leagueapps.com from svnjbspl.leagueapps.com.
    Naive (doesn't know about multi-part TLDs like .co.uk) but good enough for
    the company names this app deals with."""
    domain = _root_domain(url)
    parts = domain.split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 else domain


def resolve_company_website(name: str, context: str = "", query_hint: str = "") -> tuple[str, str]:
    """Find a company's own official site: a web search (DuckDuckGo first)
    supplies candidate results, OpenAI picks which one (if any) is actually that company --
    catching same-named-but-unrelated traps (e.g. a generic "Vanta" showing up
    for a "Vanta Sports" query) that a plain top-result heuristic can't.
    Falls back to the plain heuristic if OpenAI isn't configured.

    `query_hint` (short, e.g. a category like "college recruiting") goes into
    the SEARCH QUERY itself -- for a name that collides with unrelated real
    organizations (a generic short acronym like "NCSA" also being a
    government cybersecurity agency and a university research center), the
    plain "<name> official website" query gets dominated by whichever
    collision is more globally prominent, and no amount of picker judgment
    over irrelevant results fixes that -- the query has to be disambiguated
    up front. `context` (a longer sentence) is used only for the picker's
    judgment between the resulting candidates.
    """
    query = f"{name} {query_hint} official website".strip() if query_hint else f"{name} official website"
    results = web_search(query)
    if not results:
        return "", ""

    if openai_client.available():
        url = openai_client.pick_best_website(name, context, results)
        if url and _looks_like_company_domain(url):
            return url, "llm_picked"
        return _base_domain_fallback(results)

    for r in results:
        if _looks_like_company_domain(r["url"]):
            return r["url"], "guessed"
    return "", ""


def _base_domain_fallback(results: list[dict]) -> tuple[str, str]:
    """When no single result is the picker's answer (common when every hit
    is a customer/product subdomain -- e.g. searching a platform's name
    surfaces its individual customers' portals like
    svnjbspl.leagueapps.com/usabaseball.leagueapps.com/... rather than
    leagueapps.com itself), a base domain repeated across several results is
    itself strong evidence of the real company, even though that root URL
    never literally appeared as a hit."""
    counts: dict[str, int] = {}
    for r in results:
        base = _base_domain(r["url"])
        if base and base not in NON_COMPANY_DOMAINS:
            counts[base] = counts.get(base, 0) + 1
    if not counts:
        return "", ""
    best_base, count = max(counts.items(), key=lambda kv: kv[1])
    if count >= 3:
        return f"https://{best_base}", "guessed_from_subdomain_pattern"
    return "", ""


def _resolve_all(names: list[str], existing: list[dict]) -> list[dict]:
    seen_domains = {_root_domain(c["website"]) for c in existing if c["website"]}
    out = list(existing)
    for name in names:
        if any(c["name"].lower() == name.lower() for c in out):
            continue
        # No query_hint on the first attempt -- for a distinctive name
        # (TeamSnap, LeagueApps, ...) adding a generic category hint like
        # "sports club management software" was found to actively hurt
        # results (pulling in app-store links, review aggregators, and
        # unrelated pages ahead of the company's own homepage). But some
        # competitor names ARE generic/collision-prone on their own (Active
        # Network, ClubManager, Huddle -- a well-known unrelated
        # collaboration tool also called Huddle), and for those the plain
        # query finds nothing. So: try plain first, and only if that comes
        # back empty, retry once WITH the hint as a disambiguator.
        website, confidence = resolve_company_website(name, WARESPORT_DESCRIPTION)
        if not website:
            website, confidence = resolve_company_website(
                name, WARESPORT_DESCRIPTION, query_hint="youth sports club management software"
            )
        domain = _root_domain(website) if website else ""
        if domain and domain in seen_domains:
            continue
        if domain:
            seen_domains.add(domain)
        out.append({"name": name, "website": website, "website_confidence": confidence})
    return out


DDG_DISCOVERY_QUERY = "Waresport alternatives competitors"

# Listicle/review-aggregator sites reliably publish an actual named list on
# the page itself (unlike their own search-snippet text, which is often thin
# or auto-generated junk) -- these are the domains worth fetching in full.
# g2.com deliberately excluded: for a smaller/niche product it has no real
# data and silently serves an auto-generated filler page (verified directly
# -- it listed event/PR agencies as "alternatives" to a sports club
# management tool), which is worse than finding nothing.
_LISTICLE_DOMAINS = ("trustradius.com", "saashub.com", "sourceforge.net", "capterra.com", "slashdot.org")


def discover_competitors(seed_names: list[str], count: int = 25) -> list[dict]:
    """Returns a deduped list of {"name", "website", "website_confidence"}:
    the given seeds plus whatever OpenAI names from its own knowledge, plus
    whatever a free DuckDuckGo search of "X alternatives" listicle pages
    turns up (fetched + extracted by OpenAI) -- each resolved to a real
    website via Firecrawl+OpenAI.

    `count` asks for a few more than the target (default 25, to reliably
    clear "at least 20") since a handful of names typically fail website
    resolution or turn out to be duplicates of an already-known domain.

    The DuckDuckGo pass is purely additive and needs no API key/billing --
    it finds live "alternatives" listicle pages (G2, SaaSHub, Capterra, ...),
    fetches the actual page content (not just the search snippet, which is
    often too thin to contain the real list), and extracts real competitor
    names from it, so it can surface newer/smaller competitors that
    discover_competitors() alone (LLM knowledge only) would miss.
    """
    if not openai_client.available():
        print("  [skip] OpenAI isn't configured -- competitor discovery needs OPENAI_API_KEY in .env")
        return _resolve_all(seed_names, [])

    llm_names = openai_client.discover_competitors(WARESPORT_DESCRIPTION, seed_names, count=count)
    all_names = list(dict.fromkeys(seed_names + llm_names))

    search_results = web_search(DDG_DISCOVERY_QUERY, limit=8)
    listicle_urls = [r["url"] for r in search_results if any(d in r["url"] for d in _LISTICLE_DOMAINS)]
    for url in listicle_urls[:2]:
        fetched = page_fetch.fetch(url)
        if not fetched or not fetched.get("markdown"):
            continue
        searched_names = openai_client.extract_competitor_names_from_page_text(
            WARESPORT_DESCRIPTION, all_names, fetched["markdown"], count=count
        )
        for n in searched_names:
            if n not in all_names:
                all_names.append(n)

    return _resolve_all(all_names, [])


def discover_competitors_of_competitors(competitors: list[dict], max_new_per_competitor: int = 5) -> list[dict]:
    """One hop out: ask OpenAI for each known competitor's own competitors,
    fold any new ones in. Does not recurse further."""
    if not openai_client.available():
        print("  [skip] OpenAI isn't configured -- can't expand to competitors-of-competitors")
        return competitors

    known_names = [c["name"] for c in competitors]
    new_names: list[str] = []
    for c in competitors:
        found = openai_client.discover_competitors(
            f"{c['name']} is a company in the sports club/facility management software space.",
            known_names + new_names,
            count=max_new_per_competitor,
        )
        for n in found:
            if n not in new_names:
                new_names.append(n)

    return _resolve_all(new_names, competitors)


def scrape_integrations_page(
    competitor_name: str,
    competitor_website: str,
    page_url: str,
    skip_names: set[str] | None = None,
) -> list[Partner]:
    """Reads a competitor's integrations/partners page (page_fetch: free
    fetch, Firecrawl fallback for JS-rendered pages) and extracts partner
    companies from it (OpenAI structured extraction) -- generalizes across
    card grids, logo widgets, and plain-prose mentions, which no single regex
    heuristic can. Each extracted partner then gets its own website resolved
    the same way a discovered competitor does.

    `skip_names` (lowercased) are partners already collected elsewhere in the
    run -- they're dropped before the website lookup, which is the expensive
    part, so a partner shared by several competitors is only resolved once.
    """
    skip_names = skip_names or set()
    fetched = page_fetch.fetch(page_url)
    if not fetched or not fetched["markdown"]:
        return []
    if _looks_like_error_page(fetched["markdown"]):
        print(f"    [skip] {page_url} is a 404/error page, not real content")
        return []

    if not openai_client.available():
        print("    [skip] OpenAI isn't configured -- can't extract structured partner data from this page")
        return []

    extracted = openai_client.extract_partners(competitor_name, fetched["markdown"])

    partners = []
    for item in extracted:
        name = item.get("name", "").strip()
        if not name or name.lower() in skip_names:
            continue
        skip_names = skip_names | {name.lower()}
        category = item.get("category", "")
        description = item.get("description", "")
        # Category/description are the disambiguating signal for a name that
        # maps to multiple unrelated real companies (e.g. "NCSA" is both a
        # college-recruiting platform AND the University of Illinois's
        # supercomputing center) -- without them the picker has nothing to
        # tell them apart.
        context = f"A {category or 'partner/integration'} of {competitor_name}. {description}".strip()
        website, confidence = resolve_company_website(name, context, query_hint=category)
        partners.append(
            Partner(
                competitor=competitor_name,
                competitor_website=competitor_website,
                name=name,
                category=item.get("category", ""),
                description=item.get("description", ""),
                source_url=page_url,
                website=website,
                website_confidence=confidence,
                fit="niche_vertical",
                notes=f"Discovered via {_SCRAPE_PROVIDER_LABEL} fetch + {_LLM_PROVIDER_LABEL} extraction -- verify before outreach.",
            )
        )
    return partners


# Most SaaS products that have a dedicated integrations directory put it at
# one of these predictable paths. Trying these directly first is free (no
# search API call at all) and, when it hits, more reliable than search
# ranking -- e.g. courtreserve.com/integrations/ is the real directory page,
# but doesn't rank in the top 10 for a "site:courtreserve.com integrations"
# search at all.
COMMON_INTEGRATIONS_PATHS = [
    "/integrations", "/integrations/", "/partners", "/partners/",
    "/marketplace", "/app-directory", "/apps", "/ecosystem",
]

# A 404/error page routinely has enough boilerplate text (nav, footer, "page
# not found" copy) to clear a plain length check, which was silently
# accepting guessed paths that don't actually exist as if they were real
# integrations pages (confirmed: stacksports.com, gotsport.com, eventbrite.com,
# and activenetwork.com's /integrations guesses were ALL 404 pages that
# passed the old len()>200 check). Check for this explicitly instead.
_ERROR_PAGE_MARKERS = (
    "404", "page not found", "page couldn't be found", "could not be found",
    "doesn't exist", "does not exist", "we can't find", "we can not find",
    # Broader "was/is/were/are not found" catches phrasing variants like
    # eventbrite.com's "the page or event you are looking for was not found"
    # that a fixed exact phrase ("page you are looking for") would miss.
    "was not found", "is not found", "were not found", "are not found",
)


def _looks_like_error_page(text: str) -> bool:
    low = text[:1000].lower()  # the "not found" message is always near the top
    return any(marker in low for marker in _ERROR_PAGE_MARKERS)


MAX_PAGES_PER_COMPETITOR = 3

# Phrases that mark a same-domain page as announcing a partner/integration even
# when its URL path doesn't say so (a "Stack Sports partners with X" blog post).
# Deliberately specific -- a bare "partner" would also match M&A announcements
# and "become a partner" recruiting pages.
_PARTNERSHIP_PHRASES = (
    "partners with", "partnership with", "partnered with", "integrates with",
    "integration with", "now integrates", "official partner",
)


def _path_looks_like_integrations_page(url: str) -> bool:
    path = urlparse(url).path.lower()
    return "integrat" in path or "partner" in path


def _announces_partnership(result: dict) -> bool:
    text = f"{result.get('title', '')} {result.get('description', '')}".lower()
    return any(phrase in text for phrase in _PARTNERSHIP_PHRASES)


def _domain_plausibly_matches_name(url: str, name: str) -> bool:
    """Without a known competitor domain, an unrestricted search for
    '<name> integrations' can rank a totally unrelated company's page --
    e.g. "ClubManager" matching roompricegenie.com, "Jersey Watch"
    matching cloudbeds.com, because both happen to mention
    integrations/partners and rank for generic terms. Require the
    company name to actually show up in the domain before trusting it."""
    import re as _re

    def norm(s: str) -> str:
        return _re.sub(r"[^a-z0-9]", "", s.lower())

    domain_base = norm(_base_domain(url).split(".")[0])
    name_norm = norm(name)
    if not name_norm or not domain_base:
        return False
    return name_norm in domain_base or domain_base in name_norm


def _same_page(a: str, b: str) -> bool:
    return a.rstrip("/").lower() == b.rstrip("/").lower()


def find_integrations_pages(competitor_name: str, competitor_website: str) -> list[str]:
    """Find up to MAX_PAGES_PER_COMPETITOR pages on the competitor's own site
    that name its partners/integrations -- the dedicated directory page if
    there is one, plus any partnership announcements (blog/press posts) that
    turn up in a search.

    Tries common URL conventions on the competitor's own site first (free --
    page_fetch doesn't escalate a plain 404 to Firecrawl -- and often more
    reliable than search ranking for this specific kind of page), then a
    domain-restricted web search (DuckDuckGo first, Firecrawl search as
    fallback).

    If we already know the competitor's website, every search result MUST be
    on that same domain -- otherwise a same-named-but-unrelated company (e.g.
    a generic "Vanta" appearing for a "Vanta Sports" competitor) can get
    silently substituted in. Only when we have no known domain at all do we
    fall back to an unrestricted search.
    """
    pages: list[str] = []

    def _add(url: str) -> None:
        if len(pages) < MAX_PAGES_PER_COMPETITOR and not any(_same_page(url, p) for p in pages):
            pages.append(url)

    if competitor_website:
        base = competitor_website.rstrip("/")
        for path in COMMON_INTEGRATIONS_PATHS:
            candidate = base + path
            fetched = page_fetch.fetch(candidate)
            if fetched and len(fetched["markdown"]) > 200 and not _looks_like_error_page(fetched["markdown"]):
                _add(candidate)
                break  # /integrations and /integrations/ are the same page -- one hit is enough

        domain = _root_domain(competitor_website)
        queries = [
            f"site:{domain} integrations OR partners",
            f'site:{domain} "partners with" OR "partnership with" OR "integrates with"',
        ]
        for query in queries:
            if len(pages) >= MAX_PAGES_PER_COMPETITOR:
                break
            results = [r for r in web_search(query, limit=10) if _root_domain(r["url"]) == domain]
            # Pages whose URL path itself says integrations/partners first, then
            # same-domain posts that explicitly announce a partnership.
            for r in results:
                if _path_looks_like_integrations_page(r["url"]):
                    _add(r["url"])
            for r in results:
                if _announces_partnership(r):
                    _add(r["url"])
        return pages

    results = web_search(f"{competitor_name} integrations partners page", limit=10)
    for r in results:
        if (
            _looks_like_company_domain(r["url"])
            and _path_looks_like_integrations_page(r["url"])
            and _domain_plausibly_matches_name(r["url"], competitor_name)
        ):
            _add(r["url"])
    return pages


def describe_competitor(name: str, website: str) -> str:
    """One-or-two-sentence description of what a competitor does, grounded in
    its own homepage text when the page can be fetched (free fetch first,
    Firecrawl for JS-rendered homepages), otherwise from the model's own
    knowledge. Returns "" if OpenAI isn't configured or doesn't know it."""
    if not openai_client.available():
        return ""
    page_text = ""
    if website:
        fetched = page_fetch.fetch(website)
        if fetched and fetched["markdown"] and not _looks_like_error_page(fetched["markdown"]):
            page_text = fetched["markdown"]
    return openai_client.describe_company(name, website, page_text)
