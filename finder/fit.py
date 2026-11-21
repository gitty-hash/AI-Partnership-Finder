"""Partner fit scoring: how promising each partner is for a Waresport partnership.

Replaces the old behaviour where every extracted partner was labelled
"niche_vertical" (so the column, the sort order and the summary meant nothing).

The LLM scores a whole competitor's partners in one call (type + 0-100 score +
one-line reason). Deterministic guardrails then apply on top: a known giant
platform is always "horizontal_platform" and capped, and when the LLM is
unavailable a simple heuristic fills in so the column is never blank.
"""
from .llm_select import llm_client
from .models import Partner, registrable_domain

BIG_PLATFORM_DOMAINS = frozenset({
    "google.com", "microsoft.com", "apple.com", "amazon.com", "meta.com", "facebook.com",
    "stripe.com", "paypal.com", "zapier.com", "mailchimp.com", "salesforce.com", "hubspot.com",
    "slack.com", "zoom.us", "intuit.com", "quickbooks.com", "twilio.com", "shopify.com",
    "klaviyo.com", "activecampaign.com", "segment.com", "tableau.com", "squareup.com", "square.com",
})
HORIZONTAL_MAX_SCORE = 40


def _normalize(name: str) -> str:
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def heuristic(partner: Partner) -> tuple[str, int, str]:
    if registrable_domain(partner.website) in BIG_PLATFORM_DOMAINS:
        return "horizontal_platform", 25, "Large general-purpose platform; partnership programs are usually self-serve."
    return "niche_vertical", 50, "Not scored by the AI; listed as a partner of a sports-software competitor."


def score_partners(partners: list[Partner], *, product_description: str, competitor_name: str) -> None:
    """Sets fit / fit_score / fit_reason on each partner in place."""
    if not partners:
        return
    scored = {}
    if llm_client.available():
        results = llm_client.score_partner_fit(
            product_description,
            competitor_name,
            [{"name": p.name, "category": p.category, "description": p.description, "website": p.website} for p in partners],
        )
        scored = {_normalize(r["name"]): r for r in results}

    for p in partners:
        result = scored.get(_normalize(p.name))
        if result:
            p.fit, p.fit_score, p.fit_reason = result["type"], result["score"], result["reason"]
        else:
            p.fit, p.fit_score, p.fit_reason = heuristic(p)
        if registrable_domain(p.website) in BIG_PLATFORM_DOMAINS:
            p.fit = "horizontal_platform"
            p.fit_score = min(p.fit_score, HORIZONTAL_MAX_SCORE)
