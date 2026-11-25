from dataclasses import dataclass, field
from urllib.parse import urlparse

_TWO_PART_SLD = {"co", "com", "org", "net", "gov", "edu", "ac"}


def registrable_domain(value: str) -> str:
    """Company-identity domain for a URL or bare host: leagueapps.com from
    https://svnjbspl.leagueapps.com/x, ncsasports.org from
    https://www.ncsasports.org/who-is-ncsa. Naive (a small set of two-part
    suffixes like co.uk/com.au, not the full public-suffix list), which is
    good enough for telling companies apart here."""
    if not value:
        return ""
    host = urlparse(value if "://" in value else f"https://{value}").netloc.lower().split(":")[0]
    parts = [p for p in host.split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    if len(parts[-1]) == 2 and parts[-2] in _TWO_PART_SLD:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


@dataclass
class Partner:
    competitor: str                 # which competitor this partner was found under (e.g. "CourtReserve")
    name: str
    competitor_website: str = ""    # the competitor's own site, so it's clear which company each row belongs to
    competitor_description: str = ""  # what the competitor itself does (one or two sentences)
    category: str = ""
    description: str = ""
    source_url: str = ""            # the competitor's own page describing the integration
    website: str = ""               # the partner company's own site (may be empty if unresolved)
    website_confidence: str = ""    # "known" | "llm_picked" | "guessed" | ""
    fit: str = ""                   # "niche_vertical" | "horizontal_platform" | ""
    contact_page: str = ""
    emails: list = field(default_factory=list)
    # "verified_found": scraped/extracted from a real page.
    # "pattern_guess": no real email found anywhere -- a standard convention
    #   guess (info@domain) so the cell isn't blank, clearly labeled as such.
    # "not_found": exhausted all sources, not even a pattern guess was possible
    #   (no resolved website to build a guess from).
    email_confidence: str = ""
    notes: str = ""

    def dedupe_key(self) -> tuple:
        """Global (cross-competitor) company identity. Two rows are the same
        partner -- and would carry the same contact email -- whenever they
        resolve to the same company domain, whichever competitor's page they
        were found on and however the name was spelled ("Mailchimp" vs
        "Mailchimp Marketing"). Falls back to the normalized name only when
        no website was resolved. First one found wins."""
        domain = registrable_domain(self.website)
        if domain:
            return ("domain", domain)
        return ("name", self.name.strip().lower())

    def as_row(self) -> dict:
        emails = self.emails[:3]  # at most 3 columns
        row = {
            "competitor_name": self.competitor,
            "competitor_website": self.competitor_website,
            "competitor_description": self.competitor_description or "(no description available)",
            "partner_name": self.name,
            "category": self.category or "Unspecified",
            "fit": self.fit,
            "description": self.description or "(no description available from source)",
            "website": self.website,
            "website_confidence": self.website_confidence,
            "contact_page": self.contact_page or "(none found)",
        }
        for i in range(3):
            row[f"email_{i + 1}"] = emails[i] if i < len(emails) else ""
        row["email_confidence"] = self.email_confidence
        row["source_url"] = self.source_url
        row["notes"] = self.notes
        return row
