"""Search engines and the AI sometimes return null fields. One bad item must
never crash a run or lose the results already collected."""
import pytest

from finder import discovery, gemini_client, openai_client, pipeline, search
from finder.models import Partner

NULLS = [{"title": None, "url": "https://acme.com", "description": None}]


def partner(name):
    return Partner(competitor="TeamSnap", competitor_website="https://teamsnap.com", name=name, website=f"https://{name.lower()}.com")


@pytest.fixture(autouse=True)
def fresh_search_cache(monkeypatch):
    monkeypatch.setattr(search, "_cache", {})


def test_firecrawl_null_fields_become_empty_strings(monkeypatch):
    monkeypatch.setattr(search.ddg_client, "available", lambda: False)
    monkeypatch.setattr(search.firecrawl_client, "available", lambda: True)
    monkeypatch.setattr(search.firecrawl_client, "search", lambda q, limit=5: NULLS + [{"title": "x", "url": None, "description": "y"}])
    assert search.web_search("acme") == [{"title": "", "url": "https://acme.com", "description": ""}]


def test_duckduckgo_null_fields_become_empty_strings(monkeypatch):
    monkeypatch.setattr(search.ddg_client, "available", lambda: True)
    monkeypatch.setattr(search.ddg_client, "search", lambda q, max_results=5: [{"title": None, "url": "https://acme.com", "snippet": None}])
    assert search.web_search("acme") == [{"title": "", "url": "https://acme.com", "description": ""}]


@pytest.mark.parametrize("client", [openai_client, gemini_client])
def test_picking_a_website_survives_null_fields(client, monkeypatch):
    prompts = []
    monkeypatch.setattr(client, "_json_call", lambda system, user, *a, **k: prompts.append(user) or {"url": "https://acme.com", "confident": True})
    assert client.pick_best_website("Acme", "", NULLS) == "https://acme.com"
    assert "None" not in prompts[0]


def test_extracted_partner_with_null_fields(monkeypatch):
    monkeypatch.setattr(discovery.page_fetch, "fetch", lambda url: {"markdown": "Our partners " * 50, "links": []})
    monkeypatch.setattr(discovery.openai_client, "available", lambda: True)
    monkeypatch.setattr(discovery.openai_client, "extract_partners", lambda n, t: [{"name": "Acme", "category": None, "description": None}])
    contexts = []
    monkeypatch.setattr(discovery, "resolve_company_website", lambda n, c, query_hint="": contexts.append(c) or ("https://acme.com", "x"))
    [p] = discovery.scrape_integrations_page("TeamSnap", "https://teamsnap.com", "https://teamsnap.com/partners", skip_names=set())
    assert (p.category, p.description) == ("", "")
    assert "None" not in contexts[0]


def test_one_failed_contact_lookup_keeps_the_other_partners(monkeypatch):
    def enrich(p, delay):
        if p.name == "Bad":
            raise TypeError("boom")
        p.email_confidence = "verified_found"

    monkeypatch.setattr(pipeline, "enrich", enrich)
    bad, good = partner("Bad"), partner("Good")
    pipeline._enrich_all([bad, good], delay=0, workers=2, store=None, reused=set())
    assert good.email_confidence == "verified_found"
    assert bad.email_confidence == "not_found" and "lookup failed" in bad.notes.lower()


def test_one_failed_competitor_keeps_the_others(monkeypatch):
    def process(competitor, *a, **k):
        if competitor["name"] == "Bad":
            raise TypeError("boom")
        return [partner(competitor["name"])]

    monkeypatch.setattr(pipeline.openai_client, "available", lambda: True)
    monkeypatch.setattr(pipeline.firecrawl_client, "available", lambda: True)
    monkeypatch.setattr(pipeline.discovery, "discover_competitors", lambda seeds: [{"name": n, "website": ""} for n in ("One", "Bad", "Two")])
    monkeypatch.setattr(pipeline, "_process_competitor", process)
    got = pipeline.collect([], expand=False, delay=0, target_competitors=2)
    assert [p.name for p in got] == ["One", "Two"]
