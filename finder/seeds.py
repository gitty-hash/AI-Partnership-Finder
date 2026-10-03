"""Hand-curated partner lists for competitors whose integration/partner info
isn't in a clean, stable, scrapable HTML structure (e.g. logo carousels
rendered as images with no accessible link data, or one-off partnership
callouts in page copy).

Each entry becomes a Partner. `fit` is a rough heuristic to help prioritize
outreach:
  - "niche_vertical": a sports/facility-specific vendor -- good partnership fit,
    likely to have a real partnerships contact and care about a small club-software
    company reaching out.
  - "horizontal_platform": a huge general-purpose SaaS (Stripe, Google, Meta, ...) --
    technically an "integration partner" but unlikely to engage with a small-scale
    inbound partnership request. Kept in the list for completeness, ranked low.
"""
from .models import Partner

PODPLAY_SEED: list[dict] = [
    {
        "name": "PickleTile",
        "category": "Outdoor Court Surfacing",
        "description": "PodPlay's outdoor court solution partner (modular pickleball court tiles/surfacing).",
        "website": "",
        "website_confidence": "",
        "fit": "niche_vertical",
        "notes": "Called out as an explicit 'partnership' (not just a logo integration) on podplay.app homepage. Verify official site.",
    },
    {
        "name": "StackEleven Marketing",
        "category": "Marketing Agency (Pickleball/Padel)",
        "description": "Marketing agency exclusive to the pickleball/padel industry; integrates with PodPlay and CourtReserve.",
        "website": "https://www.stackeleven.com/",
        "website_confidence": "known",
        "fit": "niche_vertical",
        "notes": "Confirmed via web search. Also works with CourtReserve -- may already know both platforms and be receptive to a Waresport conversation.",
    },
    {
        "name": "Reunion",
        "category": "Member Community / Engagement",
        "description": "Logo shown in PodPlay's integrations widget; exact product/company not confirmed.",
        "website": "",
        "website_confidence": "",
        "fit": "niche_vertical",
        "notes": "Company identity unconfirmed -- needs manual lookup before outreach.",
    },
    {
        "name": "Stripe",
        "category": "Payments",
        "description": "Payment processing.",
        "website": "https://stripe.com/",
        "website_confidence": "known",
        "fit": "horizontal_platform",
        "notes": "Huge horizontal platform -- unlikely to do a bespoke partnership with a small club-software company. Low priority.",
    },
    {
        "name": "Google",
        "category": "Search / Business Profile",
        "description": "Google Business Profile / Reserve with Google style integration.",
        "website": "https://www.google.com/",
        "website_confidence": "known",
        "fit": "horizontal_platform",
        "notes": "Not a realistic partnership outreach target at this stage.",
    },
    {
        "name": "Meta",
        "category": "Social / Ads",
        "description": "Meta (Facebook/Instagram) integration, likely ads or business tools.",
        "website": "https://about.meta.com/",
        "website_confidence": "known",
        "fit": "horizontal_platform",
        "notes": "Not a realistic partnership outreach target at this stage.",
    },
    {
        "name": "Mailchimp",
        "category": "Email Marketing",
        "description": "Email marketing platform integration.",
        "website": "https://mailchimp.com/",
        "website_confidence": "known",
        "fit": "horizontal_platform",
        "notes": "Large horizontal platform, but does run partner/app-marketplace programs -- could be worth a self-serve integration listing rather than a partnerships email.",
    },
    {
        "name": "Klaviyo",
        "category": "Email Marketing",
        "description": "Email/SMS marketing platform integration.",
        "website": "https://www.klaviyo.com/",
        "website_confidence": "known",
        "fit": "horizontal_platform",
        "notes": "Has a public app marketplace/partner program -- self-serve listing may be more realistic than a cold partnership email.",
    },
    {
        "name": "ActiveCampaign",
        "category": "Email Marketing / CRM",
        "description": "Email marketing and CRM automation integration.",
        "website": "https://www.activecampaign.com/",
        "website_confidence": "known",
        "fit": "horizontal_platform",
        "notes": "Large horizontal platform. Low priority for direct partnership outreach.",
    },
    {
        "name": "Slack",
        "category": "Team Communication",
        "description": "Internal team communication/notifications integration.",
        "website": "https://slack.com/",
        "website_confidence": "known",
        "fit": "horizontal_platform",
        "notes": "Not a realistic partnership outreach target.",
    },
    {
        "name": "Twilio Segment",
        "category": "Customer Data Platform",
        "description": "Customer data platform integration.",
        "website": "https://segment.com/",
        "website_confidence": "known",
        "fit": "horizontal_platform",
        "notes": "Not a realistic partnership outreach target.",
    },
    {
        "name": "Tableau",
        "category": "Analytics / BI",
        "description": "Data visualization for the Analytics Plus reporting upgrade.",
        "website": "https://www.tableau.com/",
        "website_confidence": "known",
        "fit": "horizontal_platform",
        "notes": "Not a realistic partnership outreach target.",
    },
]


def load(fit_filter: str | None = None) -> list[Partner]:
    partners = []
    for row in PODPLAY_SEED:
        if fit_filter and row["fit"] != fit_filter:
            continue
        partners.append(
            Partner(
                competitor="PodPlay",
                competitor_website="https://podplay.app",
                name=row["name"],
                category=row["category"],
                description=row["description"],
                website=row["website"],
                website_confidence=row["website_confidence"],
                fit=row["fit"],
                source_url="https://podplay.app/",
                notes=row["notes"],
            )
        )
    return partners
