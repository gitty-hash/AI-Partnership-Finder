from datetime import datetime, timedelta

import pytest

from finder.lead_store import key_for
from finder.models import Partner


def partner(name="CourtTech", website="https://www.courttech.io", competitor="CourtReserve", **kw):
    return Partner(competitor=competitor, name=name, website=website, **kw)


def test_first_run_marks_new_second_run_marks_known(store):
    first = [partner(email_confidence="verified_found", emails=["hi@courttech.io"], email_source="page")]
    assert store.record_run(first) == {"new": 1, "known": 0}
    assert first[0].is_new and first[0].lead_status == "new" and first[0].first_seen

    again = [partner(competitor="PodPlay")]
    assert store.record_run(again) == {"new": 0, "known": 1}
    assert again[0].is_new is False
    assert again[0].first_seen == first[0].first_seen

    lead = store.get(key_for(again[0]))
    assert lead["times_seen"] == 2
    assert lead["competitors"] == ["CourtReserve", "PodPlay"]
    # A run that found no email doesn't wipe the verified one.
    assert lead["emails"] == ["hi@courttech.io"]


def test_a_guess_never_overwrites_a_verified_email(store):
    store.record_run([partner(email_confidence="verified_found", emails=["hi@courttech.io"])])
    store.record_run([partner(email_confidence="pattern_guess", emails=["info@courttech.io"])])
    assert store.get("domain:courttech.io")["emails"] == ["hi@courttech.io"]


def test_known_contact_is_reused_only_while_fresh(store):
    p = partner(email_confidence="verified_found", emails=["hi@courttech.io"], contact_page="https://courttech.io/contact")
    store.record_run([p])
    contact = store.known_contact(partner())
    assert contact["emails"] == ["hi@courttech.io"]
    assert contact["contact_page"] == "https://courttech.io/contact"

    with store._conn() as conn:
        old = (datetime.now() - timedelta(days=45)).isoformat(timespec="seconds")
        conn.execute("UPDATE leads SET contact_checked_at = ?", (old,))
    assert store.known_contact(partner(), max_age_days=30) is None


def test_status_only_moves_forward_and_stop_states_stick(store):
    store.record_run([partner()])
    key = "domain:courttech.io"
    assert store.set_status(key, "contacted")
    assert not store.set_status(key, "drafted")          # no going back
    assert store.set_status(key, "replied")
    assert store.set_status(key, "not_interested")       # a stop state can always be entered
    assert not store.set_status(key, "meeting")          # ...and is never left automatically
    assert store.set_status(key, "meeting", force=True)  # except by hand
    assert [e["type"] for e in store.events(key)] == ["found", "status:contacted", "status:replied", "status:not_interested", "status:meeting"]
    with pytest.raises(ValueError):
        store.set_status(key, "excited")


def test_find_by_domain_email_and_ensure(store):
    store.record_run([partner(email_confidence="verified_found", emails=["Partners@CourtTech.io"])])
    assert store.find(email="partners@courttech.io")["key"] == "domain:courttech.io"
    assert store.find(domain="sub.courttech.io")["key"] == "domain:courttech.io"

    key = store.ensure(name="Acme", emails=["sales@acme.com"])
    assert key == "domain:acme.com"
    assert store.ensure(name="Acme again", website="https://acme.com") == key
    assert store.get(key)["name"] == "Acme"
    assert store.ensure(name="No domain") == ""
