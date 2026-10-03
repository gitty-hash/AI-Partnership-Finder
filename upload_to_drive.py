#!/usr/bin/env python3
"""Upload a leads CSV to your Google Drive folder as a Google Sheet.

First time only (opens your browser to approve access):
    python upload_to_drive.py --auth

Then, to upload the newest output/leads_*.csv (or a specific file):
    python upload_to_drive.py
    python upload_to_drive.py output/leads_10_competitors.csv

main.py does this automatically at the end of every run once it's set up.
"""
import argparse
import os
from pathlib import Path

from finder import gdrive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv", nargs="?", type=Path, default=None, help="CSV to upload (default: newest output/leads_*.csv)")
    parser.add_argument("--auth", action="store_true", help="Sign in to Google once and save the token")
    parser.add_argument("--name", default=None, help="Name for the Google Sheet")
    args = parser.parse_args()

    if args.auth:
        gdrive.authorize()
        return

    folder = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
    if not folder:
        print("Set GOOGLE_DRIVE_FOLDER_ID in .env first (the Drive folder's link or ID).")
        return
    csv_path = args.csv
    if csv_path is None:
        files = list(Path("output").glob("leads_*.csv"))
        csv_path = max(files, key=lambda p: p.stat().st_mtime) if files else None
    if not csv_path or not csv_path.exists():
        print("No CSV found. Run main.py first, or pass the file path.")
        return

    print(f"[drive] uploading {csv_path} ...")
    print(gdrive.upload_csv_as_sheet(csv_path, folder, name=args.name, interactive=True))


if __name__ == "__main__":
    main()
