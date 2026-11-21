from finder import deep_dive, fit, pipeline
from finder.models import Partner


def p(name, website, competitor="CourtReserve", **kw):
    return Partner(competitor=competitor, name=name, website=website, **kw)


def test_fit_scores_from_the_model_with_guardrails(monkeypatch):
    monkeypatch.setattr(fit.llm_client, "available", lambda: True)
    monkeypatch.setattr(fit.llm_client, "score_partner_fit", lambda *a: [
        {"name": "CourtTech", "type": "niche_vertical", "score": 88, "reason": "Court booking hardware for clubs."},
        {"name": "Stripe", "type": "niche_vertical", "score": 90, "reason": "Payments."},
    ])
    partners = [p("CourtTech", "https://courttech.io"), p("Stripe", "https://stripe.com"), p("Unscored", "https://x.io")]
    fit.score_partners(partners, product_description="Waresport", competitor_name="CourtReserve")
    assert (partners[0].fit, partners[0].fit_score) == ("niche_vertical", 88)
    # A giant platform is always horizontal and capped, whatever the model said.
    assert (partners[1].fit, partners[1].fit_score) == ("horizontal_platform", fit.HORIZONTAL_MAX_SCORE)
    # Missing from the model's answer -> heuristic, never blank.
    assert partners[2].fit_score == 50 and partners[2].fit_reason


def test_known_contacts_are_reused_and_non_partners_not_crawled(monkeypatch, store):
    store.record_run([p("CourtTech", "https://courttech.io", email_confidence="verified_found", emails=["partners@courttech.io"])])
    monkeypatch.setattr(pipeline.discovery, "find_integrations_pages", lambda n, w: ["https://competitor.io/integrations"])
    monkeypatch.setattr(pipeline.discovery, "scrape_integrations_page", lambda *a, **k: [
        p("CourtTech", "https://courttech.io"), p("Riverside FC", "https://riversidefc.org"), p("NewCo", "https://newco.io"),
    ])
    monkeypatch.setattr(pipeline.discovery, "describe_competitor", lambda n, w: "A competitor.")
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)

    def score(partners, **kw):
        for x in partners:
            x.fit, x.fit_score = ("customer", 5) if "FC" in x.name else ("niche_vertical", 80)

    monkeypatch.setattr(pipeline.fit, "score_partners", score)
    crawled = []

    def fake_enrich(partner, delay):
        crawled.append(partner.name)
        partner.emails, partner.email_confidence = [f"hi@{partner.website.split('//')[1]}"], "verified_found"

    monkeypatch.setattr(pipeline, "enrich", fake_enrich)
    reused = set()
    kept = pipeline._process_competitor(
        {"name": "Competitor", "website": "https://competitor.io"}, set(), set(), set(), 0, 15, True, store=store, reused=reused, workers=2,
    )
    assert crawled == ["NewCo"]  # CourtTech reused from memory, the club skipped
    by_name = {x.name: x for x in kept}
    assert by_name["CourtTech"].email_source == "known"
    assert by_name["CourtTech"].emails == ["partners@courttech.io"]
    assert "not a partner" in by_name["Riverside FC"].notes
    assert reused == {"domain:courttech.io"}


def test_export_never_overwrites_and_sorts_new_and_best_first(tmp_path):
    a = p("Old", "https://old.io", fit_score=95, is_new=False)
    b = p("NewGood", "https://ng.io", fit_score=80)
    c = p("NewWeak", "https://nw.io", fit_score=20)
    assert [x.name for x in pipeline.sort_for_export([a, c, b])] == ["NewGood", "NewWeak", "Old"]

    target = tmp_path / "leads_2026-11-20.csv"
    first = pipeline.export([a], target)
    second = pipeline.export([b], target)
    assert first == target and second.name == "leads_2026-11-20_1.csv"
    header = target.read_text(encoding="utf-8").splitlines()[0].split(",")
    assert {"fit_score", "fit_reason", "email_source", "is_new", "first_seen", "lead_status"} <= set(header)


def test_writer_prompt_uses_the_configured_sender(monkeypatch):
    monkeypatch.setenv("OUTREACH_SENDER_NAME", "Jordan Lee")
    monkeypatch.setenv("OUTREACH_SENDER_TITLE", "Head of Partnerships")
    prompt = deep_dive.write_system_prompt()
    assert "from Jordan Lee, Head of Partnerships at Waresport" in prompt
    assert "Sharaan" not in prompt
    monkeypatch.delenv("OUTREACH_SENDER_NAME")
    assert "from the partnerships team at Waresport" in deep_dive.write_system_prompt()
