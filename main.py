#!/usr/bin/env python3
"""Waresport Partnership Finder.

Finds Waresport's competitors (club/facility management software), then
collects each competitor's own partners/integrations along with a contact
email for every partner, and writes the result to a CSV you can open in Excel.

Who does what:
  - OpenAI: names the competitors, judges which search result is the real
    company website, pulls structured partner data out of a page regardless of
    its layout, and describes what each competitor does.
  - DuckDuckGo: free web search for all of the above (Firecrawl's search is
    only a fallback if DuckDuckGo returns nothing).
  - Firecrawl: scrapes pages a plain fetch can't read (JS-rendered/blocked
    pages), including contact pages when hunting for partner emails.

Each run aims for N competitors (default 10) that each have at least one
partner/integration. A partner already listed under an earlier competitor --
same company, so the same email -- is dropped instead of repeated, and the run
keeps working down the candidate list until N competitors are covered.

Requires OPENAI_API_KEY (and, for JS-heavy pages, FIRECRAWL_API_KEY) in a .env
file in this folder.

Usage:
    python main.py
    python main.py --max-competitors 5    # fewer competitors (quicker test run)
    python main.py --expand               # also search one hop further for
                                          # competitors of competitors up front
                                          # (it already expands automatically
                                          # if the first candidates run out)
    python main.py --seed CourtReserve PodPlay   # bias discovery with names
                                          # you already know matter, in
                                          # addition to what the LLM finds
    python main.py --no-enrich            # skip email lookup, just list
                                          # competitors+partners fast
"""
import argparse
from datetime import date
from pathlib import Path

from finder.pipeline import DEFAULT_MAX_PER_COMPETITOR, DEFAULT_TARGET_COMPETITORS, run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--seed", nargs="*", default=[],
        help="Optional competitor names to include in addition to whatever the LLM discovers",
    )
    parser.add_argument(
        "--expand", action="store_true",
        help="Also search one hop out for competitors of competitors up front (uses more API calls)",
    )
    parser.add_argument(
        "--max-competitors", type=int, default=DEFAULT_TARGET_COMPETITORS,
        help="How many competitors (each with at least one unique partner/integration) to collect (default: %(default)s)",
    )
    parser.add_argument(
        "--max-per-competitor", type=int, default=DEFAULT_MAX_PER_COMPETITOR,
        help="Max partners/integrations to keep per competitor, so one sponsor-wall page can't dominate the run (default: %(default)s)",
    )
    parser.add_argument("--no-enrich", action="store_true", help="Skip contact/email discovery")
    parser.add_argument("--no-drive", action="store_true", help="Don't upload the finished sheet to Google Drive")
    parser.add_argument("--delay", type=float, default=1.5, help="Seconds to wait between requests to any one site (politeness)")
    parser.add_argument("--out", type=Path, default=None, help="Output CSV path (default: output/leads_<date>.csv)")
    args = parser.parse_args()

    out_path = args.out or Path("output") / f"leads_{date.today().isoformat()}.csv"

    run(
        enrich_contacts=not args.no_enrich,
        delay=args.delay,
        out_path=out_path,
        seed_names=args.seed,
        expand=args.expand,
        max_competitors=args.max_competitors,
        max_per_competitor=args.max_per_competitor,
        upload_to_drive=not args.no_drive,
    )


if __name__ == "__main__":
    main()
