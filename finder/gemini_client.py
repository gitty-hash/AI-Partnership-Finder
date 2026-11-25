"""Drop-in Gemini equivalent of openai_client.py -- same public function
names/signatures, so discovery.py/enrich.py/pipeline.py can call whichever
provider is selected via the LLM_PROVIDER env var without any other code
changes. Built for a one-off comparison run (Gemini vs OpenAI on the same
pipeline), not as a permanent default.

Loads GOOGLE_API_KEY from .env. Model: gemini-3.8-flash (gemini-2.5-flash is
no longer available to new users as of this writing).

Every function degrades gracefully (returns [] / "" / None) on any failure,
same contract as openai_client, so a pipeline run doesn't hard-crash if
Gemini is unavailable or a call fails.
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

MODEL = "gemini-3.8-flash"
MAX_INPUT_CHARS = 8000

_client = None
_disabled_reason = None


def _get_client():
    global _client, _disabled_reason
    if _client is not None or _disabled_reason is not None:
        return _client

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        _disabled_reason = "GOOGLE_API_KEY not set. Add it to .env to enable Gemini-assisted discovery/extraction."
        print(f"[gemini] {_disabled_reason}")
        return None
    try:
        from google import genai
    except ImportError:
        _disabled_reason = "The 'google-genai' package isn't installed. Run: pip install google-genai"
        print(f"[gemini] {_disabled_reason}")
        return None

    _client = genai.Client(api_key=api_key)
    return _client


def available() -> bool:
    return _get_client() is not None


def _json_call(system: str, user: str, max_chars: int | None = None) -> dict | None:
    client = _get_client()
    if client is None:
        return None
    try:
        from google.genai import types
        resp = client.models.generate_content(
            model=MODEL,
            contents=f"{system}\n\n{user[:max_chars or MAX_INPUT_CHARS]}",
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0),
        )
        return json.loads(resp.text)
    except Exception as e:
        print(f"[gemini] call failed: {e}")
        return None


def discover_competitors(product_description: str, known_names: list[str], count: int = 12) -> list[str]:
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


def pick_best_website(company_name: str, context: str, search_results: list[dict]) -> str:
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


def extract_competitor_names_from_page_text(product_description: str, known_names: list[str], page_text: str, count: int = 15) -> list[str]:
    if not page_text:
        return []
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


def _extract_json_from_free_text(text: str) -> dict | None:
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
    """Best-effort: tries Gemini's own google_search grounding tool. That
    tool needs a billed Google Cloud project -- if billing/credits aren't
    available this just fails and returns None, same graceful-degradation
    contract as every other tier in enrich.py's escalation chain."""
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
        from google.genai import types
        resp = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]),
        )
        result = _extract_json_from_free_text(resp.text)
    except Exception as e:
        print(f"[gemini] web_search email lookup failed for {company_name}: {e}")
        return {"email": None, "contact_page": None}
    if not result:
        return {"email": None, "contact_page": None}
    return {"email": result.get("email"), "contact_page": result.get("contact_page")}
