"""Thin wrapper around the OpenAI API.

Loads OPENAI_API_KEY from the local .env file (never hardcoded, never
printed). Used for the tasks Firecrawl + regex/heuristics can't do reliably:
  1. Competitor discovery -- "who competes with Waresport" is a knowledge/
     reasoning question, not a scraping one.
  2. Picking the right result out of a list of web-search hits (a URL that
     merely contains a name isn't necessarily that company's real site).
  3. Structured extraction of partner names from arbitrary, differently-laid-
     out competitor pages (cards, logo widgets, or plain prose -- one regex
     can't handle all three, an LLM reading the page can).
  4. Best-effort contact-email extraction from scraped page text.

Model: gpt-4o-mini by default -- cheap and accurate enough for extraction/
classification tasks like these (not creative writing), keeping this a
pay-per-use tool rather than a big line item.

Every function here degrades gracefully (returns [] / "" / None) on any
failure -- a missing key, a network error, a malformed response -- so a
pipeline run doesn't hard-crash if OpenAI is unavailable, and callers can
fall back to the free/heuristic path instead.
"""
import json
import os
import re
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

MODEL = "gpt-4o-mini"
MAX_INPUT_CHARS = 8000  # keeps token usage (and cost) bounded per call

_client = None
_disabled_reason = None


def _get_client():
    global _client, _disabled_reason
    if _client is not None or _disabled_reason is not None:
        return _client

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        _disabled_reason = "OPENAI_API_KEY not set. Add it to .env (OPENAI_API_KEY=sk-...) to enable LLM-assisted discovery/extraction."
        print(f"[openai] {_disabled_reason}")
        return None
    try:
        from openai import OpenAI
    except ImportError:
        _disabled_reason = "The 'openai' package isn't installed. Run: pip install -r requirements.txt"
        print(f"[openai] {_disabled_reason}")
        return None

    _client = OpenAI(api_key=api_key)
    return _client


def available() -> bool:
    return _get_client() is not None


def _json_call(system: str, user: str, max_chars: int | None = None) -> dict | None:
    client = _get_client()
    if client is None:
        return None
    try:
        # response_format=json_object requires the literal word "json" to
        # appear somewhere in the messages. Putting it in the system prompt
        # (never truncated) guarantees that regardless of how long/truncated
        # the user content is -- truncating a combined user string can
        # otherwise cut off a trailing "respond as JSON" instruction and
        # trigger a 400 from the API.
        system_with_json = system.rstrip() + "\n\nAlways respond with a single valid JSON object."
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system_with_json},
                {"role": "user", "content": user[:max_chars or MAX_INPUT_CHARS]},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        print(f"[openai] call failed: {e}")
        return None


def discover_competitors(product_description: str, known_names: list[str], count: int = 12) -> list[str]:
    """Asks the model directly for real, named competitors -- much more
    reliable than mining listicle pages with regex, since this is a knowledge
    question, not a text-extraction one."""
    system = (
        "You are a precise market research analyst. Only name real, "
        "currently operating software companies. Never invent names, never "
        "include generic category labels, never include the company being "
        "compared against."
    )
    user = (
        f"{product_description}\n\n"
        f"Already-known competitors: {', '.join(known_names) if known_names else '(none yet)'}\n\n"
        f"List up to {count} additional REAL competitor companies in this space "
        "that are not already in the known list. "
        'Respond as JSON: {"competitors": ["Name1", "Name2", ...]}'
    )
    result = _json_call(system, user)
    if not result:
        return []
    names = result.get("competitors", [])
    return [n.strip() for n in names if isinstance(n, str) and n.strip()]


def extract_competitor_names_from_page_text(product_description: str, known_names: list[str], page_text: str, count: int = 15) -> list[str]:
    """Pulls real competitor company names out of a fetched "X alternatives"
    listicle/comparison page (G2, SaaSHub, Capterra, etc.) -- the
    search-then-extract counterpart to discover_competitors(), but grounded
    in actual live page content instead of the model's training data alone.

    Deliberately NOT fed just search-result snippets: a listicle's own
    search-engine snippet is often thin or auto-generated junk that doesn't
    contain the real list, while the full page does.
    """
    if not page_text:
        return []
    # Same truncation rationale as extract_partners(): take the content
    # nearest the top, where a listicle's actual list of names lives,
    # rather than letting later page chrome (nav, other articles, footer)
    # dilute the model's attention.
    truncated = page_text[:4000]
    system = (
        "You extract REAL, currently operating software company names from a web page listing "
        "alternatives/competitors to a given product. Only name companies that actually appear in "
        "the page text -- never invent one, never include the product being compared against, "
        "never include generic category labels, and never include the site hosting the listicle "
        "itself (e.g. G2, Capterra, SaaSHub, SourceForge, TrustRadius, Slashdot are not "
        "competitors, they are the review/listing site)."
    )
    user = (
        f"{product_description}\n\n"
        f"Already-known competitors: {', '.join(known_names) if known_names else '(none yet)'}\n\n"
        f"Page content:\n{truncated}\n\n"
        f"List up to {count} REAL competitor company names that appear in this page and are not "
        "already in the known list. "
        'Respond as JSON: {"competitors": ["Name1", "Name2", ...]}'
    )
    result = _json_call(system, user)
    if not result:
        return []
    names = result.get("competitors", [])
    return [n.strip() for n in names if isinstance(n, str) and n.strip()]


def pick_best_website(company_name: str, context: str, search_results: list[dict]) -> str:
    """Given a company name and a handful of web-search hits, asks the model
    which one (if any) is that company's real official homepage. Returns ""
    if none look right, rather than guessing."""
    if not search_results:
        return ""
    listing = "\n".join(f"{i+1}. {r['url']} -- {r.get('title', '')} -- {r.get('description', '')[:150]}" for i, r in enumerate(search_results))
    system = "You verify company identities from search results. Be skeptical: a similarly-named but unrelated company is a common trap."
    user = (
        f"Company: {company_name}\n"
        f"Context: {context}\n\n"
        f"Search results:\n{listing}\n\n"
        "Which result (if any) is this company's own official website? "
        'Respond as JSON: {"url": "..." or null, "confident": true/false}'
    )
    result = _json_call(system, user)
    if not result or not result.get("confident"):
        return ""
    return result.get("url") or ""


def extract_partners(competitor_name: str, page_markdown: str) -> list[dict]:
    """Structured extraction of partner/integration companies from a scraped
    page's markdown, regardless of whether the source page uses cards,
    headings, or plain prose -- the one thing a regex heuristic can't
    generalize across.

    Covers both senses of "partner" the way people actually use the word:
    technology integrations (a payments processor, an access-control system)
    AND business/commercial partnerships (a perks program, a sponsorship, a
    co-marketing deal, a discount/benefits partner) -- not just API-level
    integrations. Still excludes the competitor's own customers/clients,
    internal engineering/infra vendors, banks/investors chosen behind the
    scenes, and people.
    """
    system = (
        "You extract THIRD-PARTY PARTNER COMPANIES from a software company's partners/integrations "
        "page, blog post, or press release -- covering BOTH technology integrations (a payments "
        "processor, an access-control system, an email marketing tool) AND business/commercial "
        "partnerships (a member perks/discount program, a sponsorship, a co-marketing partner, an "
        "equipment/apparel partner) -- any named company the page explicitly calls a partner, "
        "sponsor, or integration.\n\n"
        "Do NOT include, under any circumstances:\n"
        "- The page owner's own customers/clients (sports clubs, teams, leagues) -- even if shown "
        "prominently as logos or testimonials.\n"
        "- The page owner's own internal engineering/infrastructure vendors (hosting, error "
        "monitoring, analytics, dev tools) unless the page is explicitly an end-user-facing "
        "partners/integrations page, not a jobs page, engineering blog, or status page.\n"
        "- Banks, payment processors chosen behind the scenes, investors, or acquirers -- unless "
        "the page explicitly frames them as a partner or a selectable integration option.\n"
        "- Navigation items, generic section headings, or people's names.\n\n"
        "The page can be a dedicated directory, a blog post, a press release, or a feature "
        "announcement -- the PAGE TYPE doesn't matter. What matters is whether the text explicitly "
        "names a specific third-party company as a partner, sponsor, or integration (e.g. "
        "\"integrates with Stripe\", \"in partnership with Acme Corp\", \"now supports Zapier\", "
        "\"partners with USA Elite to offer member discounts\"). If nothing in the text explicitly "
        "names a qualifying partner, return an empty list rather than guessing."
    )
    # Truncate the CONTENT itself, not just the assembled prompt in _json_call
    # (which truncates at MAX_INPUT_CHARS=8000). A long page's later content --
    # nav links, other blog post teasers, embedded video chrome -- was found
    # to dilute/confuse the model into missing a partner named clearly near
    # the top, even though the same prompt correctly finds it when given only
    # the first ~3000 chars. The actual announcement is always near the top
    # of a real partners/integrations page or article.
    truncated_content = page_markdown[:4000]
    user = (
        f"This is scraped content from {competitor_name}'s integrations/partners page:\n\n"
        f"{truncated_content}\n\n"
        'Respond as JSON: {"partners": [{"name": "...", "category": "...", "description": "..."}]}. '
        "category and description should be short and drawn only from the text. Empty list if none qualify."
    )
    result = _json_call(system, user)
    if not result:
        return []
    partners = result.get("partners", [])
    return [p for p in partners if isinstance(p, dict) and p.get("name")]


def describe_company(company_name: str, website: str, page_text: str) -> str:
    """One or two plain sentences on what a company does and who it serves,
    grounded in its own homepage text when we have it. Returns "" if the
    model doesn't actually know the company -- better blank than invented."""
    system = (
        "You write short, factual descriptions of what a company does and who it serves. "
        "Use the provided homepage text when given; otherwise rely only on what you reliably know. "
        "If you don't actually know the company, return an empty description instead of guessing. "
        "No marketing fluff."
    )
    user = (
        f"Company: {company_name} ({website or 'website unknown'})\n\n"
        f"Homepage text:\n{page_text[:3000] if page_text else '(not available)'}\n\n"
        'Respond as JSON: {"description": "..."}. At most 35 words.'
    )
    result = _json_call(system, user)
    if not result:
        return ""
    description = result.get("description", "")
    return description.strip() if isinstance(description, str) else ""


def extract_contact_email(company_name: str, website: str, page_text: str) -> dict:
    """Best-effort extraction of a public contact email from scraped page
    text. Returns {"email": str|None, "contact_page_hint": str|None}."""
    system = "You find public business contact emails from scraped webpage text. Never invent an email that isn't present in the text."
    user = (
        f"Company: {company_name} ({website})\n\n"
        f"Scraped page text:\n{page_text}\n\n"
        'Respond as JSON: {"email": "..." or null, "contact_page_hint": "..." or null}. '
        "Only return an email that literally appears in the text."
    )
    result = _json_call(system, user)
    if not result:
        return {"email": None, "contact_page_hint": None}
    return {"email": result.get("email"), "contact_page_hint": result.get("contact_page_hint")}


def _extract_json_from_free_text(text: str) -> dict | None:
    """The web_search tool can't be combined with strict JSON mode (see
    find_contact_email_via_web_search), so the model sometimes wraps its
    JSON in ```json fences or a sentence of preamble -- pull the JSON object
    out robustly instead of assuming the whole response is clean JSON."""
    if not text:
        return None
    text = text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def find_contact_email_via_web_search(company_name: str, website: str) -> dict:
    """Last-resort email lookup using OpenAI's own hosted web_search tool --
    a genuinely independent attempt from the Firecrawl-sourced page text the
    other tiers use, since it searches and reads pages itself.
    Returns {"email": str|None, "contact_page": str|None}. Only ever returns
    an email the model actually found on a real page, never invented."""
    client = _get_client()
    if client is None:
        return {"email": None, "contact_page": None}
    prompt = (
        f"Company: {company_name} ({website})\n\n"
        "Search the web (their contact/about page is a good bet) for a public contact email for "
        "this company. Never invent one -- only report an email you actually find on a real page.\n\n"
        'Respond with ONLY a JSON object: {"email": "..." or null, "contact_page": "..." or null}.'
    )
    try:
        resp = client.responses.create(model=MODEL, tools=[{"type": "web_search"}], input=prompt)
        result = _extract_json_from_free_text(resp.output_text)
    except Exception as e:
        print(f"[openai] web_search email lookup failed for {company_name}: {e}")
        return {"email": None, "contact_page": None}
    if not result:
        return {"email": None, "contact_page": None}
    return {"email": result.get("email"), "contact_page": result.get("contact_page")}
