"""Best-effort contact enrichment: given a partner's website, find a public
contact email and/or contact page.

Escalation chain, cheapest/most-trustworthy first (goal: at least one email
per partner):
  1. Free, polite `requests` crawl of the homepage + a linked contact page
     (respects robots.txt, regex email extraction). No API cost.
  2. Common contact pages (/contact, /contact-us, /about, /support) fetched
     through page_fetch -- free fetch first, Firecrawl scrape for pages that
     need JS rendering -- with the same regex extraction.
  3. A web search (DuckDuckGo first) for "<company> contact email" -- often
     the search snippet itself contains a real email. Only emails on the
     partner's own domain are accepted from snippets, so a stray address
     from some unrelated page can't be attributed to the partner.
  4. page_fetch on the homepage + OpenAI reading the fetched text for a real
     email -- still only ever an email that's actually present in the page,
     never invented.
  5. OpenAI's own hosted web_search tool searches and reads pages itself -- a
     genuinely independent attempt from whatever was already tried.
  6. If nothing real turns up anywhere, fall back to a standard-convention
     guess (info@domain) so the CSV cell isn't blank -- always labeled
     "pattern_guess" via email_confidence, never presented as verified.

Never submits forms or logs in -- read-only at every step.
"""
import re
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from . import page_fetch
from .llm_select import llm_client as openai_client
from .models import Partner, registrable_domain
from .search import web_search

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; WaresportPartnershipFinder/0.1)"}
TIMEOUT = 12

# Negative lookbehind/lookahead excludes both word chars AND backslash from the
# boundary -- without the backslash exclusion, JS unicode-escape artifacts like
# `...>sales@stripe.com` (a literal, un-decoded ">" sitting next to a
# real address in a page's embedded JSON/JS) get matched as a bogus email
# starting right after the backslash.
EMAIL_RE = re.compile(r"(?<![\w\\])[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}(?!\w)")

JUNK_EMAIL_SUBSTRINGS = (
    "noreply@", "no-reply@", "example.com", "sentry.io", "wixpress.com",
    "godaddy.com", "schema.org", "@2x.", "@3x.", ".png", ".jpg", ".jpeg",
    ".svg", ".gif", ".webp", "cloudflare.com", "yoursite.com",
)

CONTACT_LINK_WORDS = ("contact", "get in touch", "reach us", "partnerships", "partner with us")


def _allowed_by_robots(url: str) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = RobotFileParser()
    try:
        rp.set_url(robots_url)
        rp.read()
    except Exception:
        return True  # no robots.txt or unreadable -> assume allowed
    return rp.can_fetch(HEADERS["User-Agent"], url)


def _fetch(url: str) -> str | None:
    if not _allowed_by_robots(url):
        return None
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException:
        return None


def _extract_emails(html: str) -> set[str]:
    found = set()
    for m in EMAIL_RE.findall(html):
        low = m.lower()
        if any(j in low for j in JUNK_EMAIL_SUBSTRINGS):
            continue
        found.add(m)
    return found


def _find_contact_link(soup: BeautifulSoup, base_url: str) -> str:
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        href = a["href"].lower()
        if any(word in text for word in CONTACT_LINK_WORDS) or any(word in href for word in CONTACT_LINK_WORDS):
            return urljoin(base_url, a["href"])
    return ""


def _root_domain(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def _free_crawl(partner: Partner) -> set[str]:
    home_html = _fetch(partner.website)
    if home_html is None:
        return set()

    emails = _extract_emails(home_html)
    soup = BeautifulSoup(home_html, "html.parser")
    contact_url = _find_contact_link(soup, partner.website)
    if contact_url:
        contact_html = _fetch(contact_url)
        if contact_html:
            emails |= _extract_emails(contact_html)
            partner.contact_page = contact_url
    return emails


CONTACT_PATHS = ("/contact", "/contact-us", "/about", "/support")


def _contact_paths_crawl(partner: Partner) -> set[str]:
    """Tries the usual contact/about URLs on the partner's own site through
    page_fetch, so a JS-rendered contact page (where the free crawl sees an
    empty shell) still gets scraped via Firecrawl. A plain 404 is not
    escalated -- page_fetch treats it as 'no such page'."""
    parsed = urlparse(partner.website)
    root = f"{parsed.scheme}://{parsed.netloc}"
    for path in CONTACT_PATHS:
        fetched = page_fetch.fetch(root + path)
        if not fetched or not fetched["markdown"]:
            continue
        emails = _extract_emails(fetched["markdown"])
        if emails:
            if not partner.contact_page:
                partner.contact_page = root + path
            return emails
    return set()


def _search_snippet_search(partner: Partner) -> set[str]:
    partner_domain = registrable_domain(partner.website)
    results = web_search(f"{partner.name} {partner_domain} contact email", limit=5)
    emails = set()
    for r in results:
        for email in _extract_emails(r.get("description", "")):
            if registrable_domain(email.split("@", 1)[1]) == partner_domain:
                emails.add(email)
    return emails


def _fetch_and_llm_fallback(partner: Partner) -> set[str]:
    if not openai_client.available():
        return set()

    fetched = page_fetch.fetch(partner.website)
    if not fetched or not fetched["markdown"]:
        return set()

    text = fetched["markdown"]
    # Also try a likely contact page if we haven't already found one.
    if not partner.contact_page:
        for link in fetched.get("links", []) or []:
            if isinstance(link, str) and "contact" in link.lower():
                contact_fetched = page_fetch.fetch(link)
                if contact_fetched and contact_fetched["markdown"]:
                    text += "\n\n" + contact_fetched["markdown"]
                    partner.contact_page = link
                break

    result = openai_client.extract_contact_email(partner.name, partner.website, text)
    email = result.get("email")
    if email and "@" in email:
        low = email.lower()
        if not any(j in low for j in JUNK_EMAIL_SUBSTRINGS):
            return {email}
    return set()


def _openai_web_search_fallback(partner: Partner) -> set[str]:
    """Last resort before a pattern guess: OpenAI's own hosted web_search
    tool searches and reads pages itself, independent of whatever was
    already tried -- a genuinely separate attempt, not just re-processing
    the same fetched content again."""
    if not openai_client.available():
        return set()
    result = openai_client.find_contact_email_via_web_search(partner.name, partner.website)
    email = result.get("email")
    if not partner.contact_page and result.get("contact_page"):
        partner.contact_page = result["contact_page"]
    if email and "@" in email:
        low = email.lower()
        if not any(j in low for j in JUNK_EMAIL_SUBSTRINGS):
            return {email}
    return set()


def enrich(partner: Partner, delay: float = 1.5) -> None:
    """Mutates partner.contact_page / partner.emails / partner.email_confidence
    / partner.notes in place, walking the escalation chain described above."""
    if not partner.website:
        partner.email_confidence = "not_found"
        partner.notes = (partner.notes + " " if partner.notes else "") + "No resolved website to look up contact info from."
        return

    emails = _free_crawl(partner)

    if not emails:
        emails = _contact_paths_crawl(partner)

    if not emails:
        emails = _search_snippet_search(partner)

    if not emails:
        time.sleep(delay)
        emails = _fetch_and_llm_fallback(partner)

    if not emails:
        emails = _openai_web_search_fallback(partner)

    if emails:
        partner.emails = sorted(emails)
        partner.email_confidence = "verified_found"
        return

    # Nothing real found anywhere -- fall back to a labeled convention guess
    # rather than leaving the cell blank.
    domain = _root_domain(partner.website)
    if domain:
        partner.emails = [f"info@{domain}"]
        partner.email_confidence = "pattern_guess"
        partner.notes = (
            (partner.notes + " " if partner.notes else "")
            + "No public email found on the site -- info@<domain> is a convention guess, NOT verified. Confirm before outreach."
        )
    else:
        partner.email_confidence = "not_found"
        partner.notes = (partner.notes + " " if partner.notes else "") + "No public email found and no usable domain to guess from."
