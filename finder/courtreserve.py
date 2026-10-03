"""Scraper for CourtReserve's public integrations directory.

CourtReserve lists its integration partners at /integrations/ as a server-rendered
grid (no JS rendering needed). Each card's "View details" link points to
CourtReserve's own help docs for that integration, not the partner's own site --
so we make a best-effort guess at the partner's real website by looking at the
external links referenced from that help article.
"""
import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .models import Partner

INTEGRATIONS_URL = "https://courtreserve.com/integrations/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; WaresportPartnershipFinder/0.1)"}

# Domains that show up in help articles but are never the partner's own site.
DOMAIN_BLACKLIST = {
    "courtreserve.com", "intercom.help", "youtube.com", "youtu.be", "vimeo.com",
    "wistia.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
    "linkedin.com", "calendly.com", "docs.google.com", "forms.gle", "zoom.us",
    "apple.com", "play.google.com", "apps.apple.com", "cloudflare.com",
}

# Substrings that mark a domain as a CDN/asset/tracking host rather than a
# company's real site -- these show up constantly in help-article HTML
# (embedded screenshots, fonts, analytics beacons) and would otherwise
# dominate the link-frequency count.
ASSET_DOMAIN_MARKERS = (
    "cdn", "assets", "static", "img.", "images.", "fonts.", "analytics",
    "cloudfront", "amazonaws", "gstatic", "googletagmanager", "google-analytics",
    "doubleclick", "hotjar", "segment.io", "sentry", "intercomcdn", "intercomassets",
    "intercomusercontent",
)

# Subdomain prefixes commonly used for help/support sites that we should
# strip to guess the company's main domain.
SUPPORT_PREFIXES = ("support.", "help.", "docs.", "kb.")

# Names known to be huge horizontal platforms rather than niche sports-tech
# vendors -- see finder.seeds for why this matters for outreach prioritization.
HORIZONTAL_PLATFORM_NAMES = {"stripe", "reserve with google", "google"}


def _guess_fit(name: str) -> str:
    return "horizontal_platform" if name.strip().lower() in HORIZONTAL_PLATFORM_NAMES else "niche_vertical"


def _root_domain(netloc: str) -> str:
    if netloc.startswith("www."):
        netloc = netloc[len("www."):]
    for prefix in SUPPORT_PREFIXES:
        if netloc.startswith(prefix):
            return netloc[len(prefix):]
    return netloc


def _is_blacklisted(root: str) -> bool:
    return any(root == b or root.endswith("." + b) for b in DOMAIN_BLACKLIST)


def _guess_website(detail_url: str) -> tuple[str, str]:
    """Best-effort guess of the partner's own website from their help article.

    Returns (website, confidence_note). Empty website means "couldn't guess,
    needs manual lookup".
    """
    if not detail_url:
        return "", ""
    try:
        resp = requests.get(detail_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException:
        return "", ""

    # Only look at real, clickable <a href> links -- not img/link/script/preload
    # src attributes, which is where most CDN/asset noise comes from.
    soup = BeautifulSoup(resp.text, "html.parser")
    hrefs = [a["href"] for a in soup.find_all("a", href=True) if a["href"].startswith("http")]

    counts: dict[str, int] = {}
    for href in hrefs:
        netloc = urlparse(href).netloc.lower()
        if not netloc or any(marker in netloc for marker in ASSET_DOMAIN_MARKERS):
            continue
        root = _root_domain(netloc)
        if not root or _is_blacklisted(root):
            continue
        counts[root] = counts.get(root, 0) + 1

    if not counts:
        return "", ""

    best = max(counts, key=counts.get)
    return f"https://{best}", "guessed"


def scrape(resolve_websites: bool = True, delay: float = 1.0) -> list[Partner]:
    resp = requests.get(INTEGRATIONS_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    partners: list[Partner] = []
    for card in soup.select(".integration-gallery-item"):
        name_el = card.select_one(".integration-gallery-title-heading")
        if not name_el:
            continue
        name = name_el.get_text(strip=True)

        categories = [c.get_text(strip=True) for c in card.select(".integration-gallery-category")]
        desc_el = card.select_one(".integration-gallery-description p")
        description = desc_el.get_text(strip=True) if desc_el else ""

        link_el = card.select_one(".integration-gallery-details-link a")
        detail_url = link_el["href"] if link_el and link_el.has_attr("href") else ""

        partner = Partner(
            competitor="CourtReserve",
            competitor_website="https://courtreserve.com",
            name=name,
            category="; ".join(categories),
            description=description,
            source_url=detail_url,
            fit=_guess_fit(name),
        )

        if resolve_websites and detail_url:
            website, confidence = _guess_website(detail_url)
            partner.website = website
            partner.website_confidence = confidence
            if not website:
                partner.notes = "Could not guess website from help article; look up manually."
            else:
                partner.notes = "Website guessed from links in CourtReserve's help doc -- verify before outreach."
            time.sleep(delay)

        partners.append(partner)

    return partners
