"""Lead memory: every partner the tool has ever found, across runs.

Before this, each run started from nothing, so a twice-weekly schedule mostly
produced the same partners again and nobody could tell which leads were new or
where each conversation stood. The store (SQLite, `data/leads.db`, git-ignored)
keeps one row per company (the same identity key the run dedupes on), plus:

- when it was first and last seen, how many runs found it, and under which competitors,
- its best known contact email (reused on later runs instead of re-scraping),
- its fit score,
- a pipeline status, with a history of every change:

    new -> drafted -> contacted -> replied -> meeting -> partner
                   \\-> not_interested | bounced | do_not_contact   (stop states)

Statuses only move forward on their own (a re-run never turns "replied" back into
"new"); `force=True` (the manual `status` command) can set anything.
"""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .models import Partner, registrable_domain

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "leads.db"

STATUSES = ("new", "drafted", "contacted", "replied", "meeting", "partner", "not_interested", "bounced", "do_not_contact")
_RANK = {"new": 0, "drafted": 1, "contacted": 2, "replied": 3, "meeting": 4, "partner": 5}
STOP_STATUSES = frozenset({"not_interested", "bounced", "do_not_contact"})
# Leads in these states are never emailed again automatically (first email or follow-up).
NO_OUTREACH_STATUSES = frozenset({"replied", "meeting", "partner"}) | STOP_STATUSES

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    domain TEXT,
    website TEXT,
    category TEXT,
    description TEXT,
    fit TEXT,
    fit_score INTEGER,
    fit_reason TEXT,
    competitors TEXT NOT NULL DEFAULT '[]',
    emails TEXT NOT NULL DEFAULT '[]',
    email_confidence TEXT,
    email_source TEXT,
    contact_page TEXT,
    contact_checked_at TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    status_updated_at TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    times_seen INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_leads_domain ON leads(domain);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE TABLE IF NOT EXISTS lead_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    at TEXT NOT NULL,
    type TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_lead_events_key ON lead_events(key, at);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def key_for(partner: Partner) -> str:
    kind, value = partner.dedupe_key()
    return f"{kind}:{value}"


def domain_key(domain_or_url: str) -> str:
    domain = registrable_domain(domain_or_url)
    return f"domain:{domain}" if domain else ""


class LeadStore:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or os.environ.get("LEADS_DB_PATH") or DEFAULT_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- reads -------------------------------------------------------------
    def get(self, key: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM leads WHERE key = ?", (key,)).fetchone()
        return self._row(row)

    def find(self, *, domain: str = "", email: str = "") -> dict | None:
        """A lead by company domain, or by one of its contact emails (or that email's domain)."""
        if email:
            low = email.strip().lower()
            with self._conn() as conn:
                for row in conn.execute("SELECT * FROM leads WHERE emails LIKE ?", (f"%{low}%",)):
                    if low in [e.lower() for e in json.loads(row["emails"] or "[]")]:
                        return self._row(row)
            domain = domain or low.rsplit("@", 1)[-1]
        if domain:
            return self.get(domain_key(domain))
        return None

    def leads(self, status: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM leads", ()
        if status:
            sql, args = sql + " WHERE status = ?", (status,)
        with self._conn() as conn:
            return [self._row(r) for r in conn.execute(sql + " ORDER BY last_seen DESC", args)]

    def events(self, key: str) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute("SELECT at, type, detail FROM lead_events WHERE key = ? ORDER BY id", (key,))]

    def known_contact(self, partner: Partner, max_age_days: int = 30) -> dict | None:
        """A verified email found for this company within `max_age_days`, so a later
        run can reuse it instead of crawling the site again."""
        lead = self.get(key_for(partner))
        if not lead or lead["email_confidence"] != "verified_found" or not lead["emails"] or not lead["contact_checked_at"]:
            return None
        checked = datetime.fromisoformat(lead["contact_checked_at"])
        if datetime.now() - checked > timedelta(days=max_age_days):
            return None
        return {
            "emails": lead["emails"],
            "email_confidence": lead["email_confidence"],
            "contact_page": lead["contact_page"] or "",
            "checked_at": lead["contact_checked_at"],
        }

    def status_counts(self) -> dict[str, int]:
        with self._conn() as conn:
            return {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM leads GROUP BY status")}

    def count_first_seen_since(self, since: datetime) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM leads WHERE first_seen >= ?", (since.isoformat(timespec="seconds"),)).fetchone()[0]

    # ---- writes ------------------------------------------------------------
    def record_run(self, partners: list[Partner], *, reused_contacts: set[str] | None = None) -> dict:
        """Upserts every partner found in this run and fills in each Partner's
        is_new / first_seen / lead_status. Returns {"new": n, "known": n}."""
        reused_contacts = reused_contacts or set()
        now = _now()
        new = known = 0
        with self._lock, self._conn() as conn:
            for p in partners:
                key = key_for(p)
                row = conn.execute("SELECT * FROM leads WHERE key = ?", (key,)).fetchone()
                found_contact = p.email_confidence == "verified_found" and key not in reused_contacts
                if row is None:
                    conn.execute(
                        """INSERT INTO leads (key, name, domain, website, category, description, fit, fit_score, fit_reason,
                               competitors, emails, email_confidence, email_source, contact_page, contact_checked_at,
                               status, status_updated_at, first_seen, last_seen, times_seen)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, 1)""",
                        (key, p.name, registrable_domain(p.website), p.website, p.category, p.description, p.fit, p.fit_score,
                         p.fit_reason, json.dumps([p.competitor]), json.dumps(p.emails), p.email_confidence, p.email_source,
                         p.contact_page, now if p.email_confidence else None, now, now, now),
                    )
                    conn.execute("INSERT INTO lead_events (key, at, type, detail) VALUES (?, ?, 'found', ?)",
                                 (key, now, f"Found under {p.competitor}"))
                    p.is_new, p.first_seen, p.lead_status = True, now, "new"
                    new += 1
                    continue

                competitors = json.loads(row["competitors"] or "[]")
                if p.competitor and p.competitor not in competitors:
                    competitors.append(p.competitor)
                updates = {
                    "name": p.name or row["name"],
                    "website": p.website or row["website"],
                    "domain": registrable_domain(p.website) or row["domain"],
                    "category": p.category or row["category"],
                    "description": p.description or row["description"],
                    "competitors": json.dumps(competitors),
                    "last_seen": now,
                    "times_seen": row["times_seen"] + 1,
                }
                if p.fit_score is not None:
                    updates.update(fit=p.fit, fit_score=p.fit_score, fit_reason=p.fit_reason)
                # A freshly verified email replaces the stored one; a guess never overwrites a verified one.
                if found_contact or (p.email_confidence and row["email_confidence"] != "verified_found"):
                    updates.update(emails=json.dumps(p.emails), email_confidence=p.email_confidence, email_source=p.email_source,
                                   contact_page=p.contact_page or row["contact_page"], contact_checked_at=now)
                sets = ", ".join(f"{k} = ?" for k in updates)
                conn.execute(f"UPDATE leads SET {sets} WHERE key = ?", (*updates.values(), key))
                p.is_new, p.first_seen, p.lead_status = False, row["first_seen"], row["status"]
                known += 1
        return {"new": new, "known": known}

    def ensure(self, *, name: str, website: str = "", emails: list[str] | tuple = (), competitor: str = "") -> str:
        """The key of the lead for this company, adding a minimal row first if it isn't
        known yet (leads from an older sheet, or already in the sent log). Returns ""
        when there is no domain to identify the company by."""
        domain = registrable_domain(website) or (registrable_domain(emails[0].rsplit("@", 1)[-1]) if emails else "")
        if not domain:
            return ""
        key = f"domain:{domain}"
        now = _now()
        with self._lock, self._conn() as conn:
            if conn.execute("SELECT 1 FROM leads WHERE key = ?", (key,)).fetchone() is None:
                conn.execute(
                    """INSERT INTO leads (key, name, domain, website, competitors, emails, email_confidence,
                           status, status_updated_at, first_seen, last_seen, times_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, 1)""",
                    (key, name or domain, domain, website or f"https://{domain}", json.dumps([competitor] if competitor else []),
                     json.dumps(list(emails)), "verified_found" if emails else None, now, now, now),
                )
                conn.execute("INSERT INTO lead_events (key, at, type, detail) VALUES (?, ?, 'imported', ?)",
                             (key, now, "Added from an outreach sheet or the sent log"))
        return key

    def set_status(self, key: str, status: str, *, detail: str = "", force: bool = False) -> bool:
        """Moves a lead to `status`. Without `force`, statuses only move forward and a
        stop state (not_interested, bounced, do_not_contact) is never left. Returns
        True when the status changed."""
        if status not in STATUSES:
            raise ValueError(f"Unknown status {status!r}. Use one of: {', '.join(STATUSES)}")
        now = _now()
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT status FROM leads WHERE key = ?", (key,)).fetchone()
            if row is None:
                return False
            current = row["status"]
            if current == status:
                return False
            if not force:
                if current in STOP_STATUSES:
                    return False
                if status not in STOP_STATUSES and _RANK.get(status, 0) <= _RANK.get(current, 0):
                    return False
            conn.execute("UPDATE leads SET status = ?, status_updated_at = ? WHERE key = ?", (status, now, key))
            conn.execute("INSERT INTO lead_events (key, at, type, detail) VALUES (?, ?, ?, ?)",
                         (key, now, f"status:{status}", detail or f"{current} -> {status}"))
        return True

    def add_event(self, key: str, type_: str, detail: str = "") -> None:
        with self._lock, self._conn() as conn:
            conn.execute("INSERT INTO lead_events (key, at, type, detail) VALUES (?, ?, ?, ?)", (key, _now(), type_, detail))

    @staticmethod
    def _row(row) -> dict | None:
        if row is None:
            return None
        out = dict(row)
        out["competitors"] = json.loads(out.get("competitors") or "[]")
        out["emails"] = json.loads(out.get("emails") or "[]")
        return out


_default: LeadStore | None = None


def default_store() -> LeadStore:
    global _default
    if _default is None:
        _default = LeadStore()
    return _default
