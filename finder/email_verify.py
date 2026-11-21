"""Cheap deliverability checks for contact emails, before anything is labelled verified.

- `domain_accepts_mail`: the domain publishes MX records (or, per RFC 5321, an
  A/AAAA record as an implicit MX). An address on a domain that can't receive
  mail always bounces, and bounces hurt the sender's reputation. Needs the
  `dnspython` package; without it the check is skipped (returns None = unknown).
- `appears_in`: the address literally occurs in the page text an LLM read, so a
  model can never "find" an email that wasn't there.
- `same_company`: the address is on the partner's own domain (or a sub-domain).

No SMTP probing: mailbox-level checks are unreliable and look like abuse to mail servers.
"""
import re
import threading

from .models import registrable_domain

_lock = threading.Lock()
_mx_cache: dict[str, bool | None] = {}
DNS_TIMEOUT = 5.0


def _resolve(domain: str) -> bool | None:
    try:
        import dns.exception
        import dns.resolver
    except ImportError:
        return None
    resolver = dns.resolver.Resolver()
    resolver.lifetime = DNS_TIMEOUT
    for record in ("MX", "A", "AAAA"):
        try:
            if resolver.resolve(domain, record):
                return True
        except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):
            return False  # the domain doesn't exist at all
        except dns.resolver.NoAnswer:
            continue
        except dns.exception.DNSException:
            return None  # timeout etc.: unknown, don't punish the address
    return False


def domain_accepts_mail(domain: str) -> bool | None:
    """True / False, or None when it couldn't be checked (no dnspython, DNS timeout)."""
    domain = (domain or "").strip().lower().rstrip(".")
    if not domain or "." not in domain:
        return False
    with _lock:
        if domain in _mx_cache:
            return _mx_cache[domain]
    result = _resolve(domain)
    with _lock:
        _mx_cache[domain] = result
    return result


def email_domain(email: str) -> str:
    return email.rsplit("@", 1)[1].lower() if "@" in email else ""


def deliverable(email: str) -> bool:
    """False only when the domain definitely can't receive mail."""
    return domain_accepts_mail(email_domain(email)) is not False


def appears_in(email: str, text: str) -> bool:
    if not email or not text:
        return False
    return re.search(rf"(?<![\w.]){re.escape(email)}(?![\w])", text, re.IGNORECASE) is not None


def same_company(email: str, website: str) -> bool:
    site = registrable_domain(website)
    return bool(site) and registrable_domain(email_domain(email)) == site


def clear_cache() -> None:
    with _lock:
        _mx_cache.clear()
