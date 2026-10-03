#!/usr/bin/env python3
"""Draft and send partnership outreach emails to the partners in a leads CSV.

Step 1 -- draft (sends nothing):
    python send_outreach.py draft --sheet          # newest sheet in your Drive folder
    python send_outreach.py draft --sheet <Google Sheet link>
    python send_outreach.py draft                  # newest local output/leads_*.csv
    python send_outreach.py draft --input output/leads_10_competitors.csv --limit 10

  Writes outreach/drafts_<date_time>.csv. Open it, edit any subject/body, and
  put "yes" in the `approved` column of each email you want sent.

Step 2 -- send:
    python send_outreach.py send --drafts outreach/drafts_<...>.csv --dry-run
        prints what would go out; sends nothing
    python send_outreach.py send --drafts outreach/drafts_<...>.csv --test-to you@example.com
        sends every approved draft to YOU instead of the partner (not logged)
    python send_outreach.py send --drafts outreach/drafts_<...>.csv
        real send to the partners; asks you to type SEND first

See README.md ("Outreach") for the .env settings sending needs.
"""
import argparse
from pathlib import Path

from finder import gdrive, outreach


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
    )


def cmd_setup(args: argparse.Namespace) -> None:
    """Prompts (hidden) for the Gmail App Password and stores it in .env as SMTP_PASSWORD."""
    import getpass

    env_path = Path(__file__).resolve().parent / ".env"
    password = getpass.getpass("Gmail App Password for waresportproduct@gmail.com (typing is hidden): ").strip()
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
    )


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
    d.set_defaults(func=cmd_draft)

    u = sub.add_parser("setup", help="Save the Gmail App Password to .env (hidden prompt)")
    u.set_defaults(func=cmd_setup)

    s = sub.add_parser("send", help="Send approved drafts")
    s.add_argument("--drafts", type=Path, required=True, help="Drafts CSV from the draft step")
    s.add_argument("--test-to", default="", help="Send everything to this address instead of the partners")
    s.add_argument("--dry-run", action="store_true", help="Print what would be sent; send nothing")
    s.add_argument("--approve-all", action="store_true", help="Send every draft, not only rows marked approved")
    s.add_argument("--max-send", type=int, default=25, help="Max emails this run (default: %(default)s)")
    s.add_argument("--delay", type=float, default=45.0, help="Average seconds between emails (default: %(default)s)")
    s.add_argument("--yes", action="store_true", help="Skip the type-SEND confirmation on a real send")
    s.set_defaults(func=cmd_send)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
