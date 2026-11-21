"""Reply and bounce handling: reads the sending Gmail inbox and updates leads.

For every message received since the first outreach email:
  - a bounce (mailer-daemon / delivery failure)  -> lead "bounced", address suppressed
  - an opt-out ("unsubscribe", "not interested", "remove me", ...) -> "not_interested", domain suppressed
  - an out-of-office / auto-reply                 -> ignored
  - any other reply from a contacted address or company domain -> "replied"

So nobody who said no, or whose address bounced, is ever emailed again, and
follow-ups stop the moment someone answers.

Read-only: the mailbox is opened with IMAP EXAMINE (select readonly), so nothing
is marked as read, moved or deleted. Uses the same Gmail App Password as sending
(IMAP must be enabled in Gmail settings).
"""
import email
import imaplib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parseaddr

from .models import registrable_domain

BOUNCE_SENDERS = re.compile(r"^(mailer-daemon|postmaster|mail-daemon|bounce|bounces)@", re.I)
BOUNCE_SUBJECT = re.compile(
    r"delivery status notification|undeliverable|undelivered mail|delivery failure|failure notice|"
    r"returned mail|mail delivery (failed|subsystem)|address not found|could not be delivered",
    re.I,
)
AUTO_REPLY_SUBJECT = re.compile(r"^(auto(matic)?[ -]?reply|out of (the )?office|ooo\b|away from|on vacation|autoreply)", re.I)
OPT_OUT = re.compile(
    r"\b(unsubscribe|remove (me|us)|take (me|us) off|stop (emailing|contacting|sending)|do not (contact|email)|"
    r"don'?t (contact|email)|not interested|no thanks|no, thank you|not a fit|not the right fit|please don'?t)\b",
    re.I,
)
# A failed recipient inside a bounce body: "Final-Recipient: rfc822; a@b.com" or "wasn't delivered to a@b.com".
FAILED_RECIPIENT = re.compile(
    r"(?:final-recipient:\s*rfc822;\s*|original-recipient:\s*rfc822;\s*|delivered to\s+|delivery to\s+(?:the following recipient[s]?\s+)?(?:failed[^\n]*?\n\s*)?)"
    r"<?([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})>?",
    re.I,
)
QUOTED_REPLY = re.compile(r"^(>|on .+ wrote:$)", re.I | re.M)


@dataclass
class InboxEvent:
    kind: str          # "bounce" | "opt_out" | "auto_reply" | "reply" | "ignore"
    address: str       # the outreach address it concerns
    subject: str
    received_at: str
    snippet: str = ""


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _text_body(msg: email.message.Message) -> str:
    parts = msg.walk() if msg.is_multipart() else [msg]
    texts = []
    for part in parts:
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "message/delivery-status", "text/rfc822-headers"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None and ctype != "text/plain":
            payload = str(part.get_payload()).encode()
        if payload:
            texts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
    return "\n".join(texts)


def _fresh_text(body: str) -> str:
    """The reply's own words, without the quoted original email below it (whose
    footer says "Not the right fit? Just reply..." and would look like an opt-out)."""
    match = QUOTED_REPLY.search(body)
    return body[: match.start()] if match else body


def classify(*, from_addr: str, subject: str, body: str, headers: dict, contacted: set[str], contacted_domains: set[str]) -> InboxEvent:
    """Pure classification of one message against the addresses we emailed."""
    sender = from_addr.lower()
    received = headers.get("date", "")
    if BOUNCE_SENDERS.match(sender) or BOUNCE_SUBJECT.search(subject):
        for candidate in FAILED_RECIPIENT.findall(body):
            if candidate.lower() in contacted:
                return InboxEvent("bounce", candidate.lower(), subject, received)
        # Fall back: any contacted address mentioned in the bounce text.
        low = body.lower()
        for address in contacted:
            if address in low:
                return InboxEvent("bounce", address, subject, received)
        return InboxEvent("ignore", "", subject, received)

    sender_domain = registrable_domain(sender.rsplit("@", 1)[-1]) if "@" in sender else ""
    if sender in contacted:
        address = sender
    elif sender_domain and sender_domain in contacted_domains:
        address = next((a for a in contacted if a.endswith("@" + sender_domain) or registrable_domain(a.rsplit("@", 1)[-1]) == sender_domain), sender)
    else:
        return InboxEvent("ignore", "", subject, received)

    if (headers.get("auto-submitted", "no").lower() not in ("", "no")
            or headers.get("x-autoreply") or headers.get("x-autorespond")
            or AUTO_REPLY_SUBJECT.search(subject.strip())):
        return InboxEvent("auto_reply", address, subject, received)

    fresh = _fresh_text(body)
    snippet = re.sub(r"\s+", " ", fresh).strip()[:200]
    if OPT_OUT.search(fresh) or OPT_OUT.search(subject):
        return InboxEvent("opt_out", address, subject, received, snippet)
    return InboxEvent("reply", address, subject, received, snippet)


def fetch_events(*, host: str, user: str, password: str, since: datetime, contacted: set[str], contacted_domains: set[str],
                 mailbox: str = "INBOX", limit: int = 2000) -> list[InboxEvent]:
    """Reads messages received since `since` (read-only) and classifies them."""
    imap = imaplib.IMAP4_SSL(host)
    try:
        imap.login(user, password)
        imap.select(mailbox, readonly=True)
        status, data = imap.search(None, "SINCE", since.strftime("%d-%b-%Y"))
        if status != "OK":
            return []
        ids = data[0].split()[-limit:]
        events = []
        for msg_id in ids:
            status, parts = imap.fetch(msg_id, "(BODY.PEEK[])")  # PEEK: never sets the \\Seen flag
            if status != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            msg = email.message_from_bytes(parts[0][1])
            from_addr = parseaddr(msg.get("From", ""))[1]
            headers = {k.lower(): str(v) for k, v in msg.items()}
            event = classify(
                from_addr=from_addr,
                subject=_decode(msg.get("Subject")),
                body=_text_body(msg),
                headers=headers,
                contacted=contacted,
                contacted_domains=contacted_domains,
            )
            if event.kind != "ignore":
                events.append(event)
        return events
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def default_since(first_sent: datetime | None) -> datetime:
    return (first_sent or datetime.now() - timedelta(days=30)) - timedelta(days=1)
