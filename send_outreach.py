#!/usr/bin/env python3
"""Draft, send and track partnership outreach to the partners in a leads CSV.

Step 1 -- draft (sends nothing):
    python send_outreach.py draft --sheet          # newest sheet in your Drive folder
    python send_outreach.py draft --sheet <Google Sheet link>
    python send_outreach.py draft                  # newest local output/leads_*.csv
    python send_outreach.py draft --input output/leads_10_competitors.csv --limit 10

  Writes outreach/drafts_<date_time>.csv. Open it, edit any subject/body, and
  put "yes" in the `approved` column of each email you want sent. One email per
  company by default (its best address); --all-addresses writes to up to three.

Step 2 -- send:
    python send_outreach.py send --drafts outreach/drafts_<...>.csv --dry-run
        prints what would go out; sends nothing
    python send_outreach.py send --drafts outreach/drafts_<...>.csv --test-to you@example.com
        sends every approved draft to YOU instead of the partner (not logged)
    python send_outreach.py send --drafts outreach/drafts_<...>.csv
        real send to the partners; asks you to type SEND first

Step 3 -- track replies and follow up:
    python send_outreach.py sync          # read replies/bounces/opt-outs from the inbox (read-only)
    python send_outreach.py followups     # draft follow-ups for anyone who hasn't replied (4 and 9 days)
    python send_outreach.py report        # reply rate, meetings, bounces, follow-ups due
    python send_outreach.py status acme.com meeting --note "Call booked for Tue"
    python send_outreach.py leads --status replied

See README.md ("Outreach") for the .env settings sending needs.
"""
import argparse
import os
from pathlib import Path

from finder import gdrive, outreach
from finder.lead_store import STATUSES, default_store, domain_key


def cmd_draft(args: argparse.Namespace) -> None:
    if args.sheet:
        try:
            leads = gdrive.download_sheet_as_csv(args.sheet, outreach.OUTREACH_DIR)
        except Exception as e:
            print(f"Couldn't read the Google Sheet: {e}")
            return
    else:
        leads = args.input or outreach.latest_leads_csv()
    if not leads or not leads.exists():
        print("No leads CSV found. Run main.py first, or pass --input path\\to\\leads.csv")
        return
    print(f"[outreach] using leads file: {leads}")
    outreach.create_drafts(
        leads,
        limit=args.limit,
        include_guessed=args.include_guessed,
        skip_big_platforms=args.skip_big_platforms,
        all_addresses=args.all_addresses,
        store=default_store(),
    )


def cmd_setup(args: argparse.Namespace) -> None:
    """Prompts (hidden) for the Gmail App Password and stores it in .env as SMTP_PASSWORD."""
    import getpass

    env_path = Path(__file__).resolve().parent / ".env"
    account = os.environ.get("SMTP_USER") or os.environ.get("OUTREACH_FROM_EMAIL") or "your sending Gmail account"
    password = getpass.getpass(f"Gmail App Password for {account} (typing is hidden): ").strip()
    password = password.replace(" ", "")  # Google shows it in 4 groups of 4
    if len(password) != 16 or not password.isalnum():
        print("That doesn't look like a Gmail App Password (16 letters/digits, e.g. 'abcd efgh ijkl mnop').")
        print("Create one at Google Account -> Security -> App passwords (needs 2-Step Verification). Nothing was saved.")
        return

    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    updated = False
    for i, line in enumerate(lines):
        if line.startswith("SMTP_PASSWORD="):
            lines[i] = f"SMTP_PASSWORD={password}"
            updated = True
    if not updated:
        lines.append(f"SMTP_PASSWORD={password}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("Saved SMTP_PASSWORD to .env.")


def cmd_send(args: argparse.Namespace) -> None:
    if not args.drafts.exists():
        print(f"Drafts file not found: {args.drafts}")
        return
    outreach.send_drafts(
        args.drafts,
        test_to=args.test_to,
        max_send=args.max_send,
        approve_all=args.approve_all,
        dry_run=args.dry_run,
        delay=args.delay,
        confirm=not args.yes,
        store=default_store(),
    )


def cmd_sync(args: argparse.Namespace) -> None:
    try:
        outreach.sync_inbox(default_store())
    except Exception as e:
        print(f"[inbox] couldn't read the inbox: {e}")
        print("For Gmail: turn on IMAP (Gmail settings > Forwarding and POP/IMAP) and use the App Password in SMTP_PASSWORD.")


def cmd_followups(args: argparse.Namespace) -> None:
    outreach.create_follow_up_drafts(store=default_store(), days=(args.first, args.second), max_age_days=args.max_age)


def cmd_report(args: argparse.Namespace) -> None:
    store = default_store()
    outreach.import_sent_log(store)
    r = outreach.outreach_report(store)
    pct = lambda v: "-" if v is None else f"{v}%"  # noqa: E731
    print("Outreach report")
    print(f"  Companies contacted : {r['companies_contacted']}  ({r['emails_sent']} first emails, {r['follow_ups_sent']} follow-ups)")
    print(f"  Responded           : {r['responded']}  ({pct(r['response_rate'])})")
    print(f"  Positive replies    : {r['positive_replies']}  ({pct(r['positive_rate'])})")
    print(f"  Meetings booked     : {r['meetings']}   Partners signed: {r['partners']}")
    print(f"  Not interested      : {r['not_interested']}   Bounced: {r['bounced']} ({pct(r['bounce_rate'])})")
    print(f"  Follow-ups due now  : {r['follow_ups_due']}  (run: python send_outreach.py followups)")
    print(f"  New leads, last 7 days: {r['new_leads_last_7_days']}")
    print("  Leads by status     : " + ", ".join(f"{k} {v}" for k, v in sorted(r["leads_by_status"].items())))


def cmd_status(args: argparse.Namespace) -> None:
    store = default_store()
    target = args.lead.strip()
    lead = store.find(email=target) if "@" in target else store.get(domain_key(target))
    if not lead:
        print(f"No lead found for {target}. Use the company's domain (e.g. acme.com) or a contacted email address.")
        return
    if store.set_status(lead["key"], args.status, detail=args.note or "set manually", force=True):
        print(f"{lead['name']} ({lead['domain']}): {lead['status']} -> {args.status}")
    else:
        print(f"{lead['name']} is already '{args.status}'.")
    if args.status in ("not_interested", "do_not_contact"):
        outreach.add_to_suppression(lead["domain"], f"marked {args.status}")


def cmd_leads(args: argparse.Namespace) -> None:
    rows = default_store().leads(status=args.status)
    rows.sort(key=lambda r: -(r["fit_score"] or 0))
    for r in rows[: args.limit]:
        email = r["emails"][0] if r["emails"] else "-"
        score = "-" if r["fit_score"] is None else r["fit_score"]
        print(f"{r['status']:<15} {score!s:>3}  {r['name'][:32]:<32} {r['domain'] or '':<28} {email:<34} first seen {r['first_seen'][:10]}")
    print(f"\n{len(rows)} lead(s){f' with status {args.status}' if args.status else ''}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("draft", help="Write curated email drafts for review (sends nothing)")
    d.add_argument("--input", type=Path, default=None, help="Leads CSV (default: newest output/leads_*.csv)")
    d.add_argument("--sheet", nargs="?", const="latest", default=None, metavar="LINK_OR_ID",
                   help="Read the contacts from a Google Sheet instead of a CSV: a Sheet link/ID, or just "
                        "--sheet for the newest 'Waresport partners' sheet in your Drive folder")
    d.add_argument("--limit", type=int, default=25, help="Max drafts to write (default: %(default)s)")
    d.add_argument("--include-guessed", action="store_true",
                   help="Also draft for partners whose only email is an unverified info@ guess (likely to bounce)")
    d.add_argument("--skip-big-platforms", action="store_true",
                   help="Skip giant platforms like Google/Stripe/Mailchimp (they are included by default)")
    d.add_argument("--all-addresses", action="store_true",
                   help="Write to up to 3 addresses per company instead of only the best one")
    d.set_defaults(func=cmd_draft)

    u = sub.add_parser("setup", help="Save the Gmail App Password to .env (hidden prompt)")
    u.set_defaults(func=cmd_setup)

    s = sub.add_parser("send", help="Send approved drafts (first emails or follow-ups)")
    s.add_argument("--drafts", type=Path, required=True, help="Drafts CSV from the draft or followups step")
    s.add_argument("--test-to", default="", help="Send everything to this address instead of the partners")
    s.add_argument("--dry-run", action="store_true", help="Print what would be sent; send nothing")
    s.add_argument("--approve-all", action="store_true", help="Send every draft, not only rows marked approved")
    s.add_argument("--max-send", type=int, default=25, help="Max emails this run (default: %(default)s)")
    s.add_argument("--delay", type=float, default=45.0, help="Average seconds between emails (default: %(default)s)")
    s.add_argument("--yes", action="store_true", help="Skip the type-SEND confirmation on a real send")
    s.set_defaults(func=cmd_send)

    y = sub.add_parser("sync", help="Read replies, opt-outs and bounces from the inbox (read-only) and update leads")
    y.set_defaults(func=cmd_sync)

    f = sub.add_parser("followups", help="Draft follow-ups for contacted companies that haven't replied")
    f.add_argument("--first", type=int, default=outreach.DEFAULT_FOLLOW_UP_DAYS[0], help="Days until follow-up 1 (default: %(default)s)")
    f.add_argument("--second", type=int, default=outreach.DEFAULT_FOLLOW_UP_DAYS[1], help="Days until follow-up 2 (default: %(default)s)")
    f.add_argument("--max-age", type=int, default=outreach.DEFAULT_FOLLOW_UP_MAX_AGE_DAYS,
                   help="Don't start follow-ups for first emails older than this many days (default: %(default)s)")
    f.set_defaults(func=cmd_followups)

    r = sub.add_parser("report", help="Outreach metrics: reply rate, meetings, bounces, follow-ups due")
    r.set_defaults(func=cmd_report)

    t = sub.add_parser("status", help="Set a lead's status by hand, e.g. after booking a meeting")
    t.add_argument("lead", help="Company domain (acme.com) or a contacted email address")
    t.add_argument("status", choices=STATUSES)
    t.add_argument("--note", default="", help="Why (kept in the lead's history)")
    t.set_defaults(func=cmd_status)

    ls = sub.add_parser("leads", help="List leads from the lead memory, best fits first")
    ls.add_argument("--status", choices=STATUSES, default=None)
    ls.add_argument("--limit", type=int, default=50)
    ls.set_defaults(func=cmd_leads)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
