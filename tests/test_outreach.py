import csv
from datetime import datetime, timedelta

from conftest import write_sent_log

from finder import inbox, outreach


def lead_row(**kw):
    row = {
        "partner_name": "CourtTech", "website": "https://courttech.io", "competitor_name": "CourtReserve",
        "email_1": "partners@courttech.io", "email_2": "info@courttech.io", "email_3": "",
        "email_confidence": "verified_found", "category": "Booking", "description": "Court booking hardware",
    }
    row.update(kw)
    return row


# ---- fit check ---------------------------------------------------------------
def test_fit_check_failure_is_not_a_not_a_fit(monkeypatch):
    monkeypatch.setattr(outreach, "_research_homepage", lambda row: "")
    monkeypatch.setattr(outreach.llm_client, "_json_call", lambda *a, **k: None)
    draft, problem = outreach.draft_email(lead_row())
    assert draft is None
    assert not problem.startswith("not a fit")
    assert "retry" in problem

    monkeypatch.setattr(outreach.llm_client, "_json_call", lambda *a, **k: {"suitable": False, "reason": "It's a club."})
    assert outreach.draft_email(lead_row()) == (None, "not a fit: It's a club.")


# ---- choosing addresses --------------------------------------------------------
def test_one_email_per_company_by_default_with_backups():
    chosen, _ = outreach.select_leads([lead_row()])
    row, emails = chosen[0]
    assert emails == ["partners@courttech.io"]
    assert row["_backup_emails"] == "info@courttech.io"

    chosen, _ = outreach.select_leads([lead_row()], all_addresses=True)
    assert chosen[0][1] == ["partners@courttech.io", "info@courttech.io"]


def test_leads_that_replied_or_opted_out_are_not_drafted(store):
    key = store.ensure(name="CourtTech", website="https://courttech.io")
    store.set_status(key, "replied")
    chosen, skipped = outreach.select_leads([lead_row()], store=store)
    assert chosen == []
    assert "already 'replied'" in skipped[0][1]


# ---- sent log ----------------------------------------------------------------------
def test_old_sent_log_is_upgraded_in_place():
    old_header = ["sent_at", "to_email", "partner_name", "partner_domain", "competitor_name", "subject", "status", "error"]
    write_sent_log([{"sent_at": "2026-10-01 10:00:00", "to_email": "a@x.com", "status": "sent"}], header=old_header)
    outreach._append_sent_log({"sent_at": "2026-10-02 10:00:00", "to_email": "b@y.com", "status": "sent", "kind": "follow_up_1", "message_id": "<m2>"})
    with outreach.SENT_LOG.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [r["kind"] for r in rows] == ["initial", "follow_up_1"]
    assert rows[1]["message_id"] == "<m2>"


# ---- follow-ups ----------------------------------------------------------------------
def sent(email, days_ago, kind="initial", message_id="<m1@x>", now=None):
    now = now or datetime(2026, 11, 20, 12, 0)
    return {
        "sent_at": (now - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S"), "to_email": email,
        "partner_name": "CourtTech", "partner_domain": "courttech.io", "subject": "Court booking for clubs",
        "status": "sent", "kind": kind, "message_id": message_id,
    }


def test_follow_ups_are_due_on_schedule_and_threaded(store):
    now = datetime(2026, 11, 20, 12, 0)
    write_sent_log([sent("partners@courttech.io", 3)])
    assert outreach.due_follow_ups(now=now, store=store) == []

    write_sent_log([sent("partners@courttech.io", 5)])
    [draft] = outreach.due_follow_ups(now=now, store=store)
    assert draft["kind"] == "follow_up_1"
    assert draft["subject"] == "Re: Court booking for clubs"
    assert draft["in_reply_to"] == "<m1@x>"
    assert "Hi CourtTech team" in draft["body"]

    write_sent_log([sent("partners@courttech.io", 10), sent("partners@courttech.io", 5, "follow_up_1", "<m2@x>")])
    [draft] = outreach.due_follow_ups(now=now, store=store)
    assert (draft["kind"], draft["in_reply_to"]) == ("follow_up_2", "<m2@x>")
    assert "won't follow up again" in draft["body"]

    write_sent_log([sent("partners@courttech.io", 20), sent("partners@courttech.io", 15, "follow_up_1"), sent("partners@courttech.io", 10, "follow_up_2")])
    assert outreach.due_follow_ups(now=now, store=store) == []  # two follow-ups, then stop


def test_one_follow_up_per_company_to_its_best_address():
    now = datetime(2026, 11, 20, 12, 0)
    write_sent_log([sent("support@courttech.io", 5, message_id="<s>"), sent("partners@courttech.io", 5, message_id="<p>"),
                    sent("info@courttech.io", 5, message_id="<i>")])
    [draft] = outreach.due_follow_ups(now=now)
    assert (draft["to_email"], draft["in_reply_to"]) == ("partners@courttech.io", "<p>")


def test_no_follow_ups_for_old_first_emails():
    now = datetime(2026, 11, 20, 12, 0)
    write_sent_log([sent("partners@courttech.io", 45)])
    assert outreach.due_follow_ups(now=now) == []
    assert len(outreach.due_follow_ups(now=now, max_age_days=60)) == 1


def test_no_follow_up_after_a_reply_or_when_suppressed(store):
    now = datetime(2026, 11, 20, 12, 0)
    write_sent_log([sent("partners@courttech.io", 5)])
    key = store.ensure(name="CourtTech", website="https://courttech.io")
    store.set_status(key, "replied")
    assert outreach.due_follow_ups(now=now, store=store) == []

    write_sent_log([sent("hello@other.io", 5) | {"partner_domain": "other.io"}])
    outreach.add_to_suppression("other.io", "test")
    assert outreach.due_follow_ups(now=now, store=store) == []


class FakeSMTP:
    def __init__(self):
        self.sent = []

    def send_message(self, msg):
        self.sent.append(msg)

    def quit(self):
        pass


def test_sending_a_follow_up_threads_it_and_logs_the_message_id(monkeypatch, store, tmp_path):
    for k, v in {"OUTREACH_SENDER_NAME": "Sam", "OUTREACH_SENDER_TITLE": "Partnerships", "OUTREACH_FROM_EMAIL": "sam@waresport.test",
                 "OUTREACH_MAILING_ADDRESS": "1 Main St", "SMTP_PASSWORD": "x" * 16}.items():
        monkeypatch.setenv(k, v)
    fake = FakeSMTP()
    monkeypatch.setattr(outreach, "_connect", lambda cfg: fake)
    write_sent_log([sent("partners@courttech.io", 5, message_id="<first@x>")])
    drafts_path = outreach.write_drafts([{**outreach.due_follow_ups(store=store, now=datetime(2026, 11, 20, 12, 0))[0], "approved": "yes"}])

    outreach.send_drafts(drafts_path, confirm=False, delay=0, store=store)
    [msg] = fake.sent
    assert msg["In-Reply-To"] == "<first@x>"
    assert msg["Subject"].startswith("Re: ")
    rows = outreach._read_sent_log()
    assert rows[-1]["kind"] == "follow_up_1" and rows[-1]["message_id"].startswith("<")

    # The same follow-up is never sent twice.
    fake.sent.clear()
    outreach.send_drafts(drafts_path, confirm=False, delay=0, store=store)
    assert fake.sent == []


def test_first_email_marks_the_lead_contacted(monkeypatch, store):
    for k, v in {"OUTREACH_SENDER_NAME": "Sam", "OUTREACH_SENDER_TITLE": "Partnerships", "OUTREACH_FROM_EMAIL": "sam@waresport.test",
                 "OUTREACH_MAILING_ADDRESS": "1 Main St", "SMTP_PASSWORD": "x" * 16}.items():
        monkeypatch.setenv(k, v)
    fake = FakeSMTP()
    monkeypatch.setattr(outreach, "_connect", lambda cfg: fake)
    drafts_path = outreach.write_drafts([{
        "approved": "yes", "to_email": "partners@courttech.io", "partner_name": "CourtTech", "partner_website": "https://courttech.io",
        "subject": "Hello", "body": "Hi CourtTech team,\n\nBody.", "status": "draft", "kind": "initial",
    }])
    outreach.send_drafts(drafts_path, confirm=False, delay=0, store=store)
    assert len(fake.sent) == 1
    assert store.find(domain="courttech.io")["status"] == "contacted"


# ---- inbox ----------------------------------------------------------------------------
CONTACTED = {"partners@courttech.io"}
DOMAINS = {"courttech.io"}


def classify(from_addr, subject, body, headers=None):
    return inbox.classify(from_addr=from_addr, subject=subject, body=body, headers=headers or {},
                          contacted=CONTACTED, contacted_domains=DOMAINS)


def test_inbox_classification():
    assert classify("mailer-daemon@googlemail.com", "Delivery Status Notification (Failure)",
                    "Your message wasn't delivered to partners@courttech.io because the address couldn't be found.").kind == "bounce"
    assert classify("jo@courttech.io", "Re: Court booking", "Sounds great, can we talk Tuesday?").kind == "reply"
    assert classify("jo@courttech.io", "Re: Court booking", "No thanks, not for us.").kind == "opt_out"
    assert classify("partners@courttech.io", "Automatic reply: away", "I'm out until Monday.").kind == "auto_reply"
    assert classify("partners@courttech.io", "Re: x", "Back soon", {"auto-submitted": "auto-replied"}).kind == "auto_reply"
    assert classify("random@else.com", "Hello", "Unrelated").kind == "ignore"
    # The quoted original (whose footer says "Not the right fit? ...") is not an opt-out.
    quoted = "Yes, let's set up a call.\n\nOn Tue, Nov 18, 2026 Sam wrote:\n> Not the right fit? Just reply and I won't reach out again."
    assert classify("jo@courttech.io", "Re: x", quoted).kind == "reply"


def test_sync_inbox_updates_leads_and_suppression(store):
    write_sent_log([sent("partners@courttech.io", 5), sent("hello@gone.io", 5) | {"partner_domain": "gone.io", "partner_name": "Gone"},
                    sent("bd@nope.io", 5) | {"partner_domain": "nope.io", "partner_name": "Nope"}])
    events = [
        inbox.InboxEvent("reply", "partners@courttech.io", "Re: x", "", "Let's talk"),
        inbox.InboxEvent("bounce", "hello@gone.io", "Undeliverable", ""),
        inbox.InboxEvent("opt_out", "bd@nope.io", "Re: x", "", "Please remove me"),
    ]
    counts = outreach.sync_inbox(store, fetch=lambda **kw: events)
    assert counts == {"replies": 1, "opt_outs": 1, "bounces": 1, "auto_replies": 0}
    assert store.find(domain="courttech.io")["status"] == "replied"
    assert store.find(domain="gone.io")["status"] == "bounced"
    assert store.find(domain="nope.io")["status"] == "not_interested"
    suppressed = outreach.load_suppression()
    assert "hello@gone.io" in suppressed and "nope.io" in suppressed


def test_report_counts_rates(store):
    write_sent_log([sent("partners@courttech.io", 5), sent("hello@gone.io", 5) | {"partner_domain": "gone.io"},
                    sent("partners@courttech.io", 1, "follow_up_1")])
    outreach.import_sent_log(store)
    store.set_status("domain:courttech.io", "meeting")
    store.set_status("domain:gone.io", "bounced")
    r = outreach.outreach_report(store)
    assert (r["companies_contacted"], r["emails_sent"], r["follow_ups_sent"]) == (2, 2, 1)
    assert (r["responded"], r["response_rate"], r["meetings"], r["bounced"]) == (1, 50, 1, 1)
