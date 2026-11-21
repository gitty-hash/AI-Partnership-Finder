from finder import email_verify, enrich, page_fetch, robots
from finder.models import Partner

# Captured at import, before the autouse fixture stubs robots.allowed.
REAL_ALLOWED = robots.allowed


def make_partner():
    return Partner(competitor="CourtReserve", name="CourtTech", website="https://courttech.io")


def rebuild_tiers(monkeypatch):
    monkeypatch.setattr(enrich, "TIERS", (
        ("page", enrich._free_crawl), ("page", enrich._contact_paths_crawl),
        ("search_snippet", enrich._search_snippet_search), ("page_llm", enrich._fetch_and_llm_fallback),
        ("web_search", enrich._openai_web_search_fallback),
    ))
    monkeypatch.setattr(enrich.time, "sleep", lambda s: None)


def stub_tiers(monkeypatch, **found):
    for tier in ("_free_crawl", "_contact_paths_crawl", "_search_snippet_search", "_fetch_and_llm_fallback", "_openai_web_search_fallback"):
        value = found.get(tier, set())
        monkeypatch.setattr(enrich, tier, lambda p, v=value: set(v))
    rebuild_tiers(monkeypatch)


def test_page_email_is_verified_and_own_domain_preferred(monkeypatch):
    stub_tiers(monkeypatch, _free_crawl={"help@agency.com", "partners@courttech.io"})
    p = make_partner()
    enrich.enrich(p, delay=0)
    assert p.emails == ["partners@courttech.io"]
    assert (p.email_confidence, p.email_source) == ("verified_found", "page")


def test_undeliverable_domain_is_dropped(monkeypatch):
    stub_tiers(monkeypatch, _free_crawl={"hi@dead-domain.example"}, _search_snippet_search={"hello@courttech.io"})
    monkeypatch.setattr(email_verify, "domain_accepts_mail", lambda d: d != "dead-domain.example")
    p = make_partner()
    enrich.enrich(p, delay=0)
    assert p.emails == ["hello@courttech.io"]
    assert p.email_source == "search_snippet"
    assert "Dropped undeliverable" in p.notes


def test_llm_email_must_appear_in_the_page_text(monkeypatch):
    monkeypatch.setattr(enrich.openai_client, "available", lambda: True)
    monkeypatch.setattr(enrich.page_fetch, "fetch", lambda url: {"markdown": "Write to team@courttech.io for help.", "links": []})
    monkeypatch.setattr(enrich.openai_client, "extract_contact_email", lambda *a: {"email": "invented@courttech.io"})
    assert enrich._fetch_and_llm_fallback(make_partner()) == set()
    monkeypatch.setattr(enrich.openai_client, "extract_contact_email", lambda *a: {"email": "team@courttech.io"})
    assert enrich._fetch_and_llm_fallback(make_partner()) == {"team@courttech.io"}


def test_web_search_email_must_be_on_the_company_domain(monkeypatch):
    monkeypatch.setattr(enrich.openai_client, "available", lambda: True)
    monkeypatch.setattr(enrich.openai_client, "find_contact_email_via_web_search", lambda *a: {"email": "someone@gmail.com", "contact_page": None})
    assert enrich._openai_web_search_fallback(make_partner()) == set()
    monkeypatch.setattr(enrich.openai_client, "find_contact_email_via_web_search", lambda *a: {"email": "bd@courttech.io", "contact_page": None})
    assert enrich._openai_web_search_fallback(make_partner()) == {"bd@courttech.io"}


def test_pattern_guess_only_for_a_domain_that_takes_mail(monkeypatch):
    stub_tiers(monkeypatch)
    p = make_partner()
    enrich.enrich(p, delay=0)
    assert (p.emails, p.email_confidence, p.email_source) == (["info@courttech.io"], "pattern_guess", "guess")

    monkeypatch.setattr(email_verify, "domain_accepts_mail", lambda d: False)
    q = make_partner()
    enrich.enrich(q, delay=0)
    assert (q.emails, q.email_confidence) == ([], "not_found")


def test_robots_disallowed_page_never_reaches_firecrawl(monkeypatch):
    calls = []
    monkeypatch.setattr(robots, "allowed", lambda url: False)
    monkeypatch.setattr(page_fetch, "_free_fetch", lambda url: calls.append("free"))
    monkeypatch.setattr(page_fetch.firecrawl_client, "available", lambda: True)
    monkeypatch.setattr(page_fetch.firecrawl_client, "scrape", lambda url: calls.append("firecrawl"))
    assert page_fetch.fetch("https://courttech.io/private") is None
    assert calls == []


def test_robots_rules_are_parsed_and_cached(monkeypatch):
    fetched = []

    class Resp:
        status_code = 200
        text = "User-agent: *\nDisallow: /private\n"

    monkeypatch.setattr(robots.requests, "get", lambda url, **kw: fetched.append(url) or Resp())
    robots.clear_cache()
    assert REAL_ALLOWED("https://courttech.io/about") is True
    assert REAL_ALLOWED("https://courttech.io/private/page") is False
    assert fetched == ["https://courttech.io/robots.txt"]  # one download per host
    robots.clear_cache()


def test_email_helpers():
    assert email_verify.appears_in("a@b.com", "mail A@B.com today")
    assert not email_verify.appears_in("a@b.com", "mail xa@b.com.au")
    assert email_verify.same_company("x@mail.courttech.io", "https://www.courttech.io")
    assert not email_verify.same_company("x@gmail.com", "https://courttech.io")
