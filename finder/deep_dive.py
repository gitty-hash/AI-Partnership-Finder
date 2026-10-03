"""Deep-dive drafting: research a partner brand, analyze how it relates to
Waresport, then write (and fact-check) a genuine business pitch.

Pipeline for one partner:
  1. gather_evidence  Fetch the brand's homepage and a couple of its own key pages
                      (news/press/about/solutions), plus the top web-search results
                      for recent news. Every piece keeps its URL.
  2. research         The LLM builds a fact sheet from ONLY that evidence; each fact
                      carries the number of the source it came from.
  3. analyze          Where the brand's product fits a club's workflow inside
                      Waresport, the value for clubs and for the brand, and the
                      single strongest reason to partner -- or viable=false.
  4. write            A 150-200 word email from the fact sheet + analysis.
  5. verify           A second pass lists any claim in the email that neither the
                      evidence nor the Waresport facts support. Unsupported claims
                      send the draft back for one rewrite; if it still fails, the
                      partner is NOT drafted (and so never sent).

Everything cited is traceable: the drafts CSV records the research summary, the
analysis and the source URLs next to each email so a human can check them.
"""
import json
import re
from urllib.parse import urlparse

from . import page_fetch
from .llm_select import llm_client
from .models import registrable_domain
from .search import web_search

MAX_EVIDENCE = 6
PAGE_CHARS = 3000
LONG_INPUT = 24000  # evidence is longer than the default per-call input cap

# Checked against waresport.com. Re-check it if the product changes: the email
# writer is only allowed to state what is in this block.
WARESPORT_FACTS = """Waresport (www.waresport.com) is an all-in-one sports club management platform for clubs, leagues, academies and facilities.
- Scheduling and leagues: practices, games and tournaments; a live team calendar synced to parents, with rainout updates.
- Facility and court booking: availability calendars, public booking pages, memberships and drop-in passes, a pro shop point of sale.
- Registrations: online registration forms, tryout registration and check-in, waivers.
- Billing and payments: recurring payment plans by ACH or card, automatic dues collection, payment ledgers (payments run through Stripe).
- Member communication: AI-powered communication, plus targeted SMS and email messaging by team.
- Integrations listed on its site: Stripe, Discord, Google Meet, AWS, QuickBooks Online, DUPR.
- Its site states "200+ clubs" across "30+ sports" (volleyball, soccer, cricket, basketball, pickleball, baseball, softball, tennis, lacrosse and more).
Do not state any other numbers, customers, awards, pricing or integrations."""

_PAGE_HINTS = ("news", "press", "about", "solutions", "products", "platform")
_SKIP_DOMAINS = {
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
    "glassdoor.com", "indeed.com", "zoominfo.com", "reddit.com",
}


# ---- 1. Evidence ------------------------------------------------------------
def gather_evidence(name: str, website: str) -> list[dict]:
    """[{"url", "title", "text"}, ...] -- the brand's own pages first, then news."""
    evidence: list[dict] = []
    seen: set[str] = set()

    def add(url: str, title: str, text: str) -> None:
        key = url.rstrip("/").lower()
        text = (text or "").strip()
        if not text or key in seen or len(evidence) >= MAX_EVIDENCE:
            return
        seen.add(key)
        evidence.append({"url": url, "title": title, "text": text[:PAGE_CHARS]})

    if website:
        home = page_fetch.fetch(website)
        if home and home["markdown"]:
            add(website, "Homepage", home["markdown"])
            own = registrable_domain(website)
            links = [
                link for link in home.get("links", []) or []
                if isinstance(link, str) and link.startswith("http") and registrable_domain(link) == own
            ]
            for hint in _PAGE_HINTS:
                if len(evidence) >= 4:
                    break
                for link in links:
                    path = urlparse(link).path.lower().strip("/")
                    if path and hint in path:
                        page = page_fetch.fetch(link)
                        if page and page["markdown"]:
                            add(link, f"{hint} page", page["markdown"])
                            break

    results = web_search(f"{name} news announcement partnership launch expansion", limit=6)
    fetched_from_search = 0
    for r in results:
        url = r.get("url", "")
        if not url or registrable_domain(url) in _SKIP_DOMAINS:
            continue
        if fetched_from_search < 2:
            page = page_fetch.fetch(url)
            text = page["markdown"] if page and page["markdown"] else f"{r.get('title', '')}. {r.get('description', '')}"
            fetched_from_search += 1
        else:
            text = f"{r.get('title', '')}. {r.get('description', '')}"
        add(url, r.get("title", "search result"), text)
    return evidence


# ---- 2. Research ------------------------------------------------------------
_RESEARCH_SYSTEM = (
    "You are a careful business research analyst. Build a fact sheet about one company using ONLY "
    "the numbered evidence provided. Rules: every fact must be stated or clearly implied by the "
    "evidence and must carry the number of the evidence item it came from; if the evidence doesn't "
    "say something, leave it out -- never fill gaps from memory or guess; prefer concrete facts "
    "(products, customers, launches, partnerships, expansions) over marketing language; ignore "
    "evidence that is clearly about a different company with a similar name."
)


def research(name: str, website: str, category: str, evidence: list[dict]) -> dict | None:
    blocks = "\n\n".join(f"[{i}] {e['url']}\n{e['text']}" for i, e in enumerate(evidence, 1))
    user = (
        f"Company: {name} ({website})\nCategory hint: {category or 'unknown'}\n\nEVIDENCE:\n{blocks}\n\n"
        'Respond as JSON: {"what_they_do": "...", "business_model": "how they make money, or empty", '
        '"customers": "who they sell to, or empty", "serves_sports_clubs": "yes" | "no" | "unclear", '
        '"recent_developments": [{"fact": "...", "source": 1}], "key_facts": [{"fact": "...", "source": 1}]}'
    )
    raw = llm_client._json_call(_RESEARCH_SYSTEM, user, max_chars=LONG_INPUT)
    if not raw or not raw.get("what_they_do"):
        return None

    def with_urls(items) -> list[dict]:
        out = []
        for item in items or []:
            if not isinstance(item, dict) or not item.get("fact"):
                continue
            idx = item.get("source")
            if isinstance(idx, int) and 1 <= idx <= len(evidence):
                out.append({"fact": str(item["fact"]).strip(), "url": evidence[idx - 1]["url"]})
        return out

    return {
        "what_they_do": str(raw.get("what_they_do", "")).strip(),
        "business_model": str(raw.get("business_model", "")).strip(),
        "customers": str(raw.get("customers", "")).strip(),
        "serves_sports_clubs": str(raw.get("serves_sports_clubs", "unclear")).strip(),
        "recent_developments": with_urls(raw.get("recent_developments")),
        "key_facts": with_urls(raw.get("key_facts")),
    }


def _fact_sheet_text(facts: dict) -> str:
    lines = [
        f"What they do: {facts['what_they_do']}",
        f"Business model: {facts['business_model'] or 'not stated'}",
        f"Customers: {facts['customers'] or 'not stated'}",
        f"Serves sports clubs: {facts['serves_sports_clubs']}",
    ]
    lines += [f"Recent: {f['fact']} ({f['url']})" for f in facts["recent_developments"]]
    lines += [f"Fact: {f['fact']} ({f['url']})" for f in facts["key_facts"]]
    return "\n".join(lines)


# ---- 3. Analysis ------------------------------------------------------------
_ANALYSIS_SYSTEM = (
    "You are a partnerships strategist for Waresport. Using ONLY the Waresport facts and the "
    "researched fact sheet, work out how the target brand relates to Waresport. Be concrete and "
    "honest: answer viable=false only when you cannot find any sensible partnership angle at all; a "
    "modest but real angle counts as viable. Do not invent a fit. Never assume Waresport features beyond the facts "
    "given. Choose ONE strongest reason; do not list several."
)


def analyze(name: str, facts: dict) -> dict | None:
    user = (
        f"WARESPORT FACTS:\n{WARESPORT_FACTS}\n\nTARGET BRAND: {name}\nFACT SHEET:\n{_fact_sheet_text(facts)}\n\n"
        'Respond as JSON: {"viable": true or false, "viability_reason": "one sentence", '
        '"workflow_fit": "where their product naturally fits into a club\'s workflow inside Waresport", '
        '"value_for_clubs": "concrete value for Waresport clubs and their members", '
        '"value_for_brand": "what the brand gains from Waresport\'s club network", '
        '"strongest_reason": "the single strongest reason this partnership makes sense", '
        '"club_scenario": "one realistic scenario at a club, 2-3 sentences"}'
    )
    raw = llm_client._json_call(_ANALYSIS_SYSTEM, user, max_chars=LONG_INPUT)
    if not raw:
        return None
    raw["viable"] = bool(raw.get("viable"))
    return raw


# ---- 4. Write ---------------------------------------------------------------
_WRITE_SYSTEM = """You are a senior B2B partnerships marketer with 20+ years of experience in sports and club management software. You write genuine, well-reasoned business pitches that earn replies -- never mass-mailing copy.

Write one partnership email from Sharaan Thayanithi, Senior Sales Agent at Waresport, to the target brand, from the research and analysis provided.

Requirements:
- Subject: under 60 characters, specific to them (their name or what they do), never the generic "Partnership Opportunity with Waresport".
- Body: 150 to 200 words, plain text, 4 short paragraphs:
  1. Open with something specific about their business that shows we did our homework (a real fact from the fact sheet).
  2. Briefly introduce Waresport (what it does, for whom), using only the Waresport facts, and mention the Waresport website once as plain text exactly as given.
  3. Make the partnership case: a real-world club scenario (from the analysis) and clear benefits for BOTH sides -- what clubs and members get, and what the brand gains.
  4. Close with a request for a 15 to 20 minute call.
- Tone: professional, confident, persuasive. No buzzwords or hype (revolutionary, game-changing, synergy, seamless, leverage, cutting-edge), no exclamation marks, no emojis.
- Use no numbers, customers, awards, pricing, integrations or claims that are not in the fact sheet or the Waresport facts. Do not say we use their product. Do not criticize competitors. Never imply Waresport already has a relationship with them.
- Greet them as "Hi <Brand Name> team,". Do NOT write a sign-off, signature, postal address or unsubscribe text -- those are added automatically. The Waresport website is the only web address allowed in the body."""


def write(name: str, facts: dict, plan: dict, feedback: str = "") -> dict | None:
    user = (
        f"WARESPORT FACTS:\n{WARESPORT_FACTS}\n\nTARGET BRAND: {name}\nFACT SHEET:\n{_fact_sheet_text(facts)}\n\n"
        f"ANALYSIS:\n{json.dumps({k: plan.get(k) for k in ('workflow_fit', 'value_for_clubs', 'value_for_brand', 'strongest_reason', 'club_scenario')}, indent=2)}\n"
        + (f"\nYour previous draft was rejected: {feedback}. Rewrite it and fix that.\n" if feedback else "")
        + '\nRespond as JSON: {"subject": "...", "body": "..."}'
    )
    return llm_client._json_call(_WRITE_SYSTEM, user, max_chars=LONG_INPUT)


# ---- 5. Verify --------------------------------------------------------------
_VERIFY_SYSTEM = (
    "You are a strict fact-checker for outbound sales emails. Given an email, a researched fact sheet "
    "about the recipient, and the allowed facts about the sender (Waresport), list every concrete "
    "claim in the email that neither source supports: invented numbers, customers, awards, "
    "launches, integrations, product features, or statements about the recipient not in the fact "
    "sheet. Hypothetical benefits framed as a proposal ('could', 'would') and the club scenario are "
    "fine. Return an empty list if everything checks out."
)


def verify(subject: str, body: str, facts: dict) -> list[str]:
    user = (
        f"EMAIL SUBJECT: {subject}\nEMAIL BODY:\n{body}\n\nFACT SHEET ABOUT RECIPIENT:\n{_fact_sheet_text(facts)}\n\n"
        f"ALLOWED FACTS ABOUT SENDER:\n{WARESPORT_FACTS}\n\n"
        'Respond as JSON: {"unsupported_claims": ["..."]}'
    )
    result = llm_client._json_call(_VERIFY_SYSTEM, user, max_chars=LONG_INPUT)
    if not result:
        return ["fact-check unavailable"]  # fail closed: an unchecked email is not sent
    claims = result.get("unsupported_claims", [])
    return [str(c) for c in claims if str(c).strip()] if isinstance(claims, list) else []


# ---- Orchestration ----------------------------------------------------------
_SIGNOFF_RE = re.compile(r"^(best|best regards|kind regards|regards|sincerely|cheers|thanks|thank you|warmly)[,.!]?$", re.I)


def _strip_signoff(body: str) -> str:
    """The signature is added by code; drop any sign-off the model wrote anyway."""
    lines = body.rstrip().splitlines()
    while lines and (not lines[-1].strip() or _SIGNOFF_RE.match(lines[-1].strip())):
        lines.pop()
    return "\n".join(lines).strip()


def _summary_for_sheet(facts: dict) -> str:
    parts = [facts["what_they_do"]]
    if facts["business_model"]:
        parts.append(f"Business model: {facts['business_model']}")
    if facts["customers"]:
        parts.append(f"Customers: {facts['customers']}")
    for item in facts["recent_developments"][:3]:
        parts.append(f"Recent: {item['fact']}")
    return " ".join(parts)


def _analysis_for_sheet(plan: dict) -> str:
    return (
        f"Fit in workflow: {plan.get('workflow_fit', '')} | Value for clubs: {plan.get('value_for_clubs', '')} | "
        f"Value for brand: {plan.get('value_for_brand', '')} | Strongest reason: {plan.get('strongest_reason', '')}"
    )


def build_draft(row: dict, validate) -> tuple[dict | None, str]:
    """Research -> analyze -> write -> verify. Returns (draft, "") or
    (None, reason). `validate(subject, body)` returns a problem string or ""."""
    name = (row.get("partner_name") or "").strip()
    website = (row.get("website") or "").strip()
    category = (row.get("category") or "").strip()

    evidence = gather_evidence(name, website)
    if not evidence:
        return None, "no research material could be fetched"
    facts = research(name, website, category, evidence)
    if not facts:
        return None, "the research step found nothing usable"
    plan = analyze(name, facts)
    if not plan:
        return None, "the analysis step returned nothing"
    if not plan["viable"]:
        return None, f"not a fit: {plan.get('viability_reason', 'no natural fit with Waresport')}"

    feedback = ""
    for _ in range(2):
        written = write(name, facts, plan, feedback)
        if not written:
            feedback = "the model returned no usable response"
            continue
        subject = str(written.get("subject", "")).strip()
        body = _strip_signoff(str(written.get("body", "")).strip())
        problem = validate(subject, body)
        if not problem:
            unsupported = verify(subject, body, facts)
            if not unsupported:
                cited = []
                for item in facts["recent_developments"] + facts["key_facts"]:
                    if item["url"] not in cited:
                        cited.append(item["url"])
                if not cited:
                    cited = [e["url"] for e in evidence[:3]]
                return {
                    "subject": subject,
                    "body": body,
                    "research_summary": _summary_for_sheet(facts),
                    "analysis": _analysis_for_sheet(plan),
                    "sources": "\n".join(f"{i}. {u}" for i, u in enumerate(cited, 1)),
                }, ""
            problem = "unsupported claims: " + "; ".join(unsupported[:4])
        feedback = problem
    return None, feedback
