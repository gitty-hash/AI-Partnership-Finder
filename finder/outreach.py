"""Partnership outreach: draft curated emails to the partners/integrations in a
leads CSV, then (after a human approves them) send them.

Two stages, on purpose -- cold email to real companies can't be unsent:

  draft  Reads the leads CSV, picks the best business contact address per
         partner, and has the LLM write a short, specific email for each. Output
         is a drafts CSV you review/edit; nothing is sent.
  send   Sends only the drafts marked approved, via SMTP, paced and capped, with
         a signature/mailing address/opt-out line added by code (not the model).

Guardrails built in:
  - Everything defaults to safe: `draft` never sends; `send --dry-run` and
    `send --test-to you@example.com` never reach a partner.
  - Guessed addresses (info@<domain> conventions, email_confidence=pattern_guess)
    are skipped by default -- they bounce, and bounces hurt sender reputation.
  - Support/legal/press/careers-style mailboxes are never used as the target.
  - A send log (outreach/sent_log.csv) means nobody is emailed twice, by address
    or by company domain; outreach/suppression.txt lists anyone to never contact.
  - A per-day send cap and a randomized delay between messages.
"""
import csv
import os
import random
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from . import deep_dive, page_fetch
from .discovery import WARESPORT_DESCRIPTION
from .llm_select import llm_client
from .models import registrable_domain

OUTREACH_DIR = Path(__file__).resolve().parent.parent / "outreach"
SENT_LOG = OUTREACH_DIR / "sent_log.csv"
SUPPRESSION_FILE = OUTREACH_DIR / "suppression.txt"

DRAFT_FIELDS = [
    "approved", "to_email", "partner_name", "partner_website", "competitor_name",
    "email_confidence", "subject", "body", "status", "research_summary", "analysis", "sources",
]
SENT_LOG_FIELDS = ["sent_at", "to_email", "partner_name", "partner_domain", "competitor_name", "subject", "status", "error"]

# ---- Choosing who to write to ---------------------------------------------
# Only addresses that nobody reads, or that look broken, are dropped. Everything
# else is a real inbox that can answer -- including HR, support and press
# mailboxes, which often forward a partnership note to the right person -- so
# those stay eligible and are simply ranked last.
NEVER_PREFIXES = ("noreply", "donotreply", "abuse", "postmaster", "dmca", "unsubscribe", "spam")
_LOW_PRIORITY_TOKENS = {
    "privacy", "legal", "careers", "jobs", "hr", "billing", "claims", "press", "media",
    "security", "webmaster", "compliance", "invoices", "accounting",
}
_ROLE_SCORES = (
    ({"partner", "partners", "partnership", "partnerships", "alliances", "bizdev", "business"}, 100),
    ({"hello", "hi", "info", "contact", "team", "inquiries", "enquiries", "general", "office"}, 70),
    ({"sales", "growth", "marketing"}, 60),
    ({"support", "help", "customerservice", "customersuccess", "service"}, 25),
)
_NAMED_PERSON_SCORE = 40
_LOW_PRIORITY_SCORE = 10

BIG_PLATFORM_DOMAINS = {
    "google.com", "microsoft.com", "apple.com", "amazon.com", "meta.com", "facebook.com",
    "stripe.com", "paypal.com", "zapier.com", "mailchimp.com", "salesforce.com", "hubspot.com",
    "slack.com", "zoom.us", "intuit.com", "quickbooks.com", "twilio.com", "shopify.com",
}
FREE_MAIL_DOMAINS = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "aol.com"}
_EMAIL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._%+\-]*@([A-Za-z0-9\-]+\.)+[A-Za-z]{2,}$")


def _email_domain(email: str) -> str:
    return registrable_domain(email.split("@", 1)[1]) if "@" in email else ""


def _score_email(email: str) -> int:
    local = email.split("@", 1)[0].lower()
    tokens = set(re.split(r"[._\-+]", local)) | {local}
    if tokens & _LOW_PRIORITY_TOKENS:
        return _LOW_PRIORITY_SCORE
    for words, score in _ROLE_SCORES:
        if tokens & words:
            return score
    return _NAMED_PERSON_SCORE


def address_problem(email: str, partner_domain: str = "") -> str:
    """Why an address looks broken or untrustworthy, or "" if it looks fine.
    Catches scraping debris (fused text like 'loopspark.comQuick', a stray
    leading dot, 'ex.comwexer.com') and addresses on a domain that has nothing
    to do with the company."""
    email = email.strip()
    if not _EMAIL_RE.match(email) or ".." in email:
        return "malformed address"
    local, host = email.rsplit("@", 1)
    if re.search(r"[a-z][A-Z]", host.rsplit(".", 1)[-1]):
        return "malformed address (text fused onto the end)"
    if re.sub(r"[^a-z]", "", local.lower()).startswith(NEVER_PREFIXES):
        return "an address nobody reads (no-reply, abuse...)"
    domain = registrable_domain(host)
    if partner_domain and domain not in FREE_MAIL_DOMAINS:
        email_label, partner_label = domain.split(".")[0], partner_domain.split(".")[0]
        if email_label != partner_label and email_label[:3] in {"com", "net", "org"} and email_label[3:] == partner_label:
            return "malformed domain"
        related = email_label == partner_label or (
            min(len(email_label), len(partner_label)) >= 4
            and (email_label in partner_label or partner_label in email_label)
        )
        if not related:
            return f"on a domain unrelated to the company ({domain})"
    return ""


def usable_emails(emails: list[str], partner_domain: str = "") -> tuple[list[str], list[tuple[str, str]]]:
    """(up to 3 trustworthy addresses, best first, [(rejected address, why)])."""
    seen: set[str] = set()
    on_domain: list[str] = []
    off_domain: list[tuple[str, str]] = []
    bad: list[tuple[str, str]] = []
    for raw in emails:
        email = raw.strip()
        if not email or email.lower() in seen:
            continue
        seen.add(email.lower())
        problem = address_problem(email, partner_domain)
        if not problem:
            on_domain.append(email.lower())
        elif problem.startswith("on a domain unrelated"):
            off_domain.append((email.lower(), problem))
        else:
            bad.append((email, problem))
    # An address on a different domain can still be right (a parent company, a
    # sister brand), so it is only dropped when the company ALSO has an address on
    # its own domain -- which is the one to trust.
    if on_domain:
        bad += off_domain
        good = on_domain
    else:
        good = [e for e, _ in off_domain]
    good.sort(key=_score_email, reverse=True)
    return good[:3], bad


# ---- Suppression / sent log -------------------------------------------------
def load_suppression() -> set[str]:
    if not SUPPRESSION_FILE.exists():
        return set()
    entries = set()
    for line in SUPPRESSION_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if line:
            entries.add(line)
    return entries


def _is_suppressed(email: str, domain: str, suppressed: set[str]) -> bool:
    return email.lower() in suppressed or domain in suppressed or _email_domain(email) in suppressed


def _read_sent_log() -> list[dict]:
    if not SENT_LOG.exists():
        return []
    with SENT_LOG.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_contacted() -> tuple[set[str], set[str]]:
    """(emails, company domains) already sent to successfully."""
    emails, domains = set(), set()
    for row in _read_sent_log():
        if row.get("status") == "sent":
            emails.add((row.get("to_email") or "").lower())
            if row.get("partner_domain"):
                domains.add(row["partner_domain"].lower())
    return emails, domains


def _sent_today_count() -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    return sum(1 for r in _read_sent_log() if r.get("status") == "sent" and (r.get("sent_at") or "").startswith(today))


def _append_sent_log(entry: dict) -> None:
    OUTREACH_DIR.mkdir(parents=True, exist_ok=True)
    new_file = not SENT_LOG.exists()
    with SENT_LOG.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SENT_LOG_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(entry)


# ---- Reading leads ----------------------------------------------------------
def latest_leads_csv(output_dir: Path = Path("output")) -> Path | None:
    files = [p for p in output_dir.glob("leads_*.csv")]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def load_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def select_leads(
    rows: list[dict],
    *,
    include_guessed: bool = False,
    skip_big_platforms: bool = False,
    limit: int | None = None,
) -> tuple[list[tuple[dict, list[str]]], list[tuple[str, str]]]:
    """Returns ([(row, [emails to write to]), ...], [(partner, why_skipped), ...]).
    A row is only skipped for a concrete reason, and every skip is reported --
    including rows never reached because of `limit`."""
    suppressed = load_suppression()
    contacted_emails, contacted_domains = load_contacted()
    chosen: list[tuple[dict, list[str]]] = []
    skipped: list[tuple[str, str]] = []
    seen_emails: set[str] = set()
    seen_domains: set[str] = set()

    for index, row in enumerate(rows):
        partner = (row.get("partner_name") or "").strip()
        raw = [(row.get(f"email_{i}") or "").strip() for i in (1, 2, 3)]
        raw = [e for e in raw if e]
        if not raw:
            skipped.append((partner, "no email found"))
            continue
        if row.get("email_confidence") == "pattern_guess" and not include_guessed:
            skipped.append((partner, "its only email is an unverified info@ guess (likely to bounce)"))
            continue
        site_domain = registrable_domain(row.get("website") or "")
        if skip_big_platforms and site_domain in BIG_PLATFORM_DOMAINS:
            skipped.append((partner, "large platform (skipped because --skip-big-platforms is on)"))
            continue
        emails, bad = usable_emails(raw, site_domain)
        if not emails:
            why = "; ".join(f"{e}: {p}" for e, p in bad) or "no usable address"
            skipped.append((partner, f"no trustworthy address -- {why}"))
            continue
        domain = site_domain or ("" if _email_domain(emails[0]) in FREE_MAIL_DOMAINS else _email_domain(emails[0]))
        emails = [e for e in emails if not _is_suppressed(e, domain, suppressed)]
        if not emails:
            skipped.append((partner, "on the suppression list"))
            continue
        if (domain and domain in contacted_domains) or all(e in contacted_emails for e in emails):
            skipped.append((partner, "already contacted"))
            continue
        emails = [e for e in emails if e not in contacted_emails]
        if (domain and domain in seen_domains) or all(e in seen_emails for e in emails):
            skipped.append((partner, "duplicate of another row in this list"))
            continue
        seen_emails.update(emails)
        if domain:
            seen_domains.add(domain)
        chosen.append((row, emails))
        if limit and len(chosen) >= limit:
            left = len(rows) - index - 1
            if left:
                skipped.append(("(rest of the sheet)", f"{left} more rows not looked at, because the limit of {limit} was reached"))
            break
    return chosen, skipped


# ---- Drafting ---------------------------------------------------------------
def _company_site() -> str:
    """The Waresport website as plain text for the email body (waresport.com)."""
    url = os.environ.get("OUTREACH_COMPANY_WEBSITE", "https://www.waresport.com")
    return re.sub(r"^https?://(www\.)?", "", url.strip()).rstrip("/")


def _word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


def _validate_draft(subject: str, body: str) -> str:
    """Returns a description of what's wrong, or "" if the draft is fine."""
    if not subject:
        return "subject is empty"
    if len(subject) > 80:
        return "subject is too long (keep it under 60 characters)"
    words = _word_count(body)
    if words < 130:
        return f"body is too short ({words} words; aim for 150-200)"
    if words > 230:
        return f"body is too long ({words} words; aim for 150-200)"
    if re.search(r"[\[\]<>]|\{\{", body):
        return "body contains a placeholder or bracket"
    site = _company_site().lower()
    low = body.lower()
    if site not in low:
        return f"body must mention the Waresport website ({site}) once, as plain text"
    without_site = low.replace("www." + site, "").replace(site, "")
    if "http" in without_site or "www." in without_site:
        return f"body contains a web address other than {site}"
    return ""


def _research_homepage(row: dict) -> str:
    """The partner's own homepage text (free fetch, Firecrawl for JS-rendered
    pages), so the email can be grounded in what the company actually does
    rather than only the one-line description in the sheet."""
    website = (row.get("website") or "").strip()
    if not website:
        return ""
    fetched = page_fetch.fetch(website)
    return (fetched or {}).get("markdown", "")[:3500] if fetched else ""


_FIT_SYSTEM_PROMPT = (
    "You screen targets for a partnership cold email from Waresport, a sports club management software "
    "company. Decide whether the target is a commercial brand, vendor, technology or service provider "
    "that Waresport could realistically integrate with, co-market with, or refer clubs to.\n"
    "Answer suitable=false if the target is any of: a sports club, league, association, federation, "
    "school or team (a customer/member organization, not a business partner); a company that simply "
    "uses a competing club-management product; itself a club-management or league-management software "
    "competitor; an individual's personal page; a government body; or something the evidence shows is "
    "not a real standalone company. When genuinely in doubt, answer true: later steps (research, analysis and a fact-check) "
    "catch weak fits, and wrongly skipping a real prospect costs us a lead. Only answer false when "
    "the target clearly falls into one of the categories above."
)


def check_fit(row: dict, research: str) -> tuple[bool, str]:
    """(suitable, reason). Cheap deterministic checks first, then the model."""
    own = registrable_domain(row.get("website") or "")
    competitor_site = registrable_domain(row.get("competitor_website") or "")
    if own and competitor_site and own == competitor_site:
        return False, "its website is the competitor's own page (likely a customer announcement, not a partner)"
    result = llm_client._json_call(
        _FIT_SYSTEM_PROMPT,
        f"Target company: {row.get('partner_name', '')}\nWebsite: {row.get('website', '')}\n"
        f"Category: {row.get('category', '')}\nDescription: {row.get('description', '')}\n"
        f"Listed as a partner of competitor: {row.get('competitor_name', '')}\n\n"
        f"Homepage text:\n{research or '(unavailable)'}\n\n"
        'Respond as JSON: {"suitable": true or false, "reason": "one short sentence"}',
    )
    if not result:
        return False, "fit check unavailable"
    return bool(result.get("suitable")), str(result.get("reason", "")).strip()


def draft_email(row: dict) -> tuple[dict | None, str]:
    """Returns (draft, "") or (None, why not). A cheap suitability check runs
    first (clubs, leagues, competitors... are skipped with a reason starting
    "not a fit"); then the deep dive researches the brand, analyzes how it
    relates to Waresport, writes the pitch and fact-checks it. The draft dict
    has subject, body, research_summary, analysis and sources."""
    suitable, why = check_fit(row, _research_homepage(row))
    if not suitable:
        return None, f"not a fit: {why}"
    return deep_dive.build_draft(row, _validate_draft)


def create_drafts(
    leads_csv: Path,
    *,
    limit: int | None = 25,
    include_guessed: bool = False,
    skip_big_platforms: bool = False,
) -> Path | None:
    if not llm_client.available():
        print("[outreach] OpenAI isn't configured -- drafting needs OPENAI_API_KEY in .env")
        return None

    rows = load_rows(leads_csv)
    chosen, skipped = select_leads(
        rows, include_guessed=include_guessed, skip_big_platforms=skip_big_platforms, limit=limit
    )
    print(f"[outreach] {len(rows)} rows in {leads_csv.name}: {len(chosen)} eligible, {len(skipped)} skipped")
    for partner, why in skipped:
        print(f"    skip {partner or '(unnamed)'}: {why}")

    drafts: list[dict] = []
    for i, (row, emails) in enumerate(chosen, 1):
        partner = row.get("partner_name", "")
        print(f"  [{i}/{len(chosen)}] drafting for {partner} -> {', '.join(emails)}")
        draft, problem = draft_email(row)
        drafts.append({
            "approved": "",
            "to_email": "; ".join(emails),
            "partner_name": partner,
            "partner_website": row.get("website", ""),
            "competitor_name": row.get("competitor_name", ""),
            "email_confidence": row.get("email_confidence", ""),
            "subject": draft["subject"] if draft else "",
            "body": draft["body"] if draft else "",
            "status": "draft" if draft else (f"skipped: {problem}" if problem.startswith("not a fit") else f"failed: {problem}"),
            "research_summary": draft["research_summary"] if draft else "",
            "analysis": draft["analysis"] if draft else "",
            "sources": draft["sources"] if draft else "",
        })

    if not drafts:
        print("[outreach] nothing to draft")
        return None

    OUTREACH_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTREACH_DIR / f"drafts_{datetime.now().strftime('%Y-%m-%d_%H%M')}.csv"
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=DRAFT_FIELDS)
        writer.writeheader()
        writer.writerows(drafts)

    ok = sum(1 for d in drafts if d["status"] == "draft")
    print(f"\n[outreach] wrote {ok} drafts ({len(drafts) - ok} failed) to {out_path}")
    print("Review/edit subject and body, put 'yes' in the approved column for each email to send, then run the send step.")
    return out_path


# ---- Sending ----------------------------------------------------------------
@dataclass
class OutreachConfig:
    sender_name: str
    sender_title: str
    company: str
    company_website: str
    from_email: str
    reply_to: str
    mailing_address: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    daily_cap: int

    @classmethod
    def from_env(cls) -> "OutreachConfig":
        from_email = os.environ.get("OUTREACH_FROM_EMAIL", "").strip()
        return cls(
            sender_name=os.environ.get("OUTREACH_SENDER_NAME", "").strip(),
            sender_title=os.environ.get("OUTREACH_SENDER_TITLE", "").strip(),
            company=os.environ.get("OUTREACH_COMPANY", "Waresport").strip(),
            company_website=os.environ.get("OUTREACH_COMPANY_WEBSITE", "https://www.waresport.com").strip(),
            from_email=from_email,
            reply_to=os.environ.get("OUTREACH_REPLY_TO", "").strip(),
            mailing_address=os.environ.get("OUTREACH_MAILING_ADDRESS", "").strip(),
            smtp_host=os.environ.get("SMTP_HOST", "smtp.gmail.com").strip(),
            smtp_port=int(os.environ.get("SMTP_PORT", "587")),
            smtp_user=os.environ.get("SMTP_USER", "").strip() or from_email,
            smtp_password=os.environ.get("SMTP_PASSWORD", "").strip(),
            daily_cap=int(os.environ.get("OUTREACH_DAILY_CAP", "25")),
        )

    def missing_for_send(self) -> list[str]:
        required = {
            "OUTREACH_SENDER_NAME": self.sender_name,
            "OUTREACH_SENDER_TITLE": self.sender_title,
            "OUTREACH_FROM_EMAIL": self.from_email,
            "OUTREACH_MAILING_ADDRESS": self.mailing_address,
            "SMTP_PASSWORD": self.smtp_password,
        }
        return [name for name, value in required.items() if not value]


def build_full_body(body: str, cfg: OutreachConfig) -> str:
    """The model's paragraph(s) plus the parts that must be exact -- signature,
    postal address, and an easy opt-out -- added by code so they can't be
    mangled or invented."""
    site = re.sub(r"^https?://", "", cfg.company_website).rstrip("/")
    signature = f"{cfg.sender_name}\n{cfg.sender_title}, {cfg.company}\n{cfg.from_email} | {site}"
    footer = (
        f"{cfg.company}, {cfg.mailing_address}\n"
        "Not the right fit? Just reply and I won't reach out again."
    )
    return f"{body.rstrip()}\n\n{signature}\n\n--\n{footer}\n"


def build_message(to_email: str, subject: str, body: str, cfg: OutreachConfig) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((cfg.sender_name, cfg.from_email))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg.from_email.split("@", 1)[-1])
    reply_to = cfg.reply_to or cfg.from_email
    msg["Reply-To"] = reply_to
    msg["List-Unsubscribe"] = f"<mailto:{reply_to}?subject=unsubscribe>"
    msg.set_content(build_full_body(body, cfg))
    return msg


def _connect(cfg: OutreachConfig) -> smtplib.SMTP:
    if cfg.smtp_port == 465:
        server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=ssl.create_default_context(), timeout=30)
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
        server.starttls(context=ssl.create_default_context())
    server.login(cfg.smtp_user, cfg.smtp_password)
    return server


def _is_approved(value: str) -> bool:
    return (value or "").strip().lower() in {"y", "yes", "true", "1", "approved"}


def send_drafts(
    drafts_csv: Path,
    *,
    test_to: str = "",
    max_send: int = 25,
    approve_all: bool = False,
    dry_run: bool = False,
    delay: float = 45.0,
    confirm: bool = True,
) -> None:
    cfg = OutreachConfig.from_env()
    real_send = not test_to and not dry_run

    if not dry_run:
        missing = cfg.missing_for_send()
        if missing:
            print(f"[outreach] can't send -- set these in .env first: {', '.join(missing)}")
            return

    with drafts_csv.open(newline="", encoding="utf-8-sig") as f:
        drafts = list(csv.DictReader(f))
    to_send = [
        d for d in drafts
        if (approve_all or _is_approved(d.get("approved", "")))
        and d.get("status") == "draft" and d.get("subject") and d.get("body") and "@" in (d.get("to_email") or "")
    ]
    if not to_send:
        print("[outreach] no approved drafts to send (put 'yes' in the approved column, or use --approve-all)")
        return

    suppressed = load_suppression()
    contacted_emails, contacted_domains = load_contacted()
    remaining_today = max(0, cfg.daily_cap - _sent_today_count()) if real_send else max_send
    limit = min(max_send, remaining_today)
    if real_send and limit == 0:
        print(f"[outreach] today's cap of {cfg.daily_cap} sends is already used up -- try again tomorrow")
        return

    queue = []
    for d in to_send:
        partner_domain = registrable_domain(d.get("partner_website") or "")
        for email in [e.strip() for e in d["to_email"].split(";") if "@" in e]:
            domain = partner_domain or _email_domain(email)
            if real_send and (email.lower() in contacted_emails or (domain and domain in contacted_domains)):
                print(f"  skip {d['partner_name']} <{email}>: already contacted")
                continue
            if _is_suppressed(email, domain, suppressed):
                print(f"  skip {d['partner_name']} <{email}>: on the suppression list")
                continue
            queue.append((d, email, domain))
    queue = queue[:limit]
    if not queue:
        print("[outreach] nothing left to send after skipping contacted/suppressed")
        return

    mode = "DRY RUN (nothing is sent)" if dry_run else (f"TEST MODE (everything goes to {test_to})" if test_to else "REAL SEND to partners")
    print(f"[outreach] {mode}: {len(queue)} email(s) from {drafts_csv.name}")

    if dry_run:
        for i, (d, email, _) in enumerate(queue, 1):
            print(f"\n--- {i}. To: {email} ({d['partner_name']}) ---\nSubject: {d['subject']}\n")
            print(build_full_body(d["body"], cfg) if i == 1 else d["body"])
        return

    if real_send and confirm:
        answer = input(f"\nThis will email {len(queue)} real companies from {cfg.from_email}. Type SEND to continue: ")
        if answer.strip() != "SEND":
            print("[outreach] cancelled")
            return

    server = None
    sent = 0
    for i, (d, email, domain) in enumerate(queue, 1):
        recipient = test_to or email
        subject = d["subject"] if not test_to else f"[TEST for {email}] {d['subject']}"
        status, error = "sent", ""
        try:
            if server is None:
                server = _connect(cfg)
            server.send_message(build_message(recipient, subject, d["body"], cfg))
            sent += 1
            print(f"  [{i}/{len(queue)}] sent -> {recipient} ({d['partner_name']})")
        except smtplib.SMTPAuthenticationError as e:
            print(f"[outreach] login failed ({e}) -- stopping. For Gmail use an App Password, not your normal password.")
            return
        except Exception as e:
            status, error = "failed", str(e)
            server = None  # reconnect on the next message
            print(f"  [{i}/{len(queue)}] FAILED -> {recipient} ({d['partner_name']}): {e}")

        if real_send:
            _append_sent_log({
                "sent_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "to_email": email, "partner_name": d["partner_name"], "partner_domain": domain,
                "competitor_name": d.get("competitor_name", ""), "subject": d["subject"],
                "status": status, "error": error,
            })
        if i < len(queue):
            time.sleep(delay * random.uniform(0.8, 1.4))

    if server:
        try:
            server.quit()
        except Exception:
            pass
    print(f"\n[outreach] done: {sent} of {len(queue)} sent{'' if real_send else ' (not logged -- not a real send)'}")
