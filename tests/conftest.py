"""Shared test setup: every file the code writes (sent log, suppression list,
drafts, lead memory) goes to a temporary folder, and nothing reaches the network."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from finder import email_verify, outreach, robots  # noqa: E402
from finder.lead_store import LeadStore  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    out = tmp_path / "outreach"
    monkeypatch.setattr(outreach, "OUTREACH_DIR", out)
    monkeypatch.setattr(outreach, "SENT_LOG", out / "sent_log.csv")
    monkeypatch.setattr(outreach, "SUPPRESSION_FILE", out / "suppression.txt")
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "leads.db"))
    for key in ("OUTREACH_SENDER_NAME", "OUTREACH_SENDER_TITLE", "OUTREACH_COMPANY", "OUTREACH_COMPANY_WEBSITE"):
        monkeypatch.delenv(key, raising=False)
    # No DNS or robots.txt lookups in tests unless a test opts in.
    monkeypatch.setattr(email_verify, "domain_accepts_mail", lambda domain: True)
    monkeypatch.setattr(robots, "allowed", lambda url: True)
    yield tmp_path


@pytest.fixture
def store(tmp_path):
    return LeadStore(tmp_path / "leads.db")


def write_sent_log(rows: list[dict], header: list[str] | None = None) -> None:
    import csv

    outreach.OUTREACH_DIR.mkdir(parents=True, exist_ok=True)
    fields = header or outreach.SENT_LOG_FIELDS
    with outreach.SENT_LOG.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})
