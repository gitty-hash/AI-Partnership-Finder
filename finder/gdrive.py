"""Upload a finished leads CSV to a Google Drive folder as a Google Sheet.

Runs entirely inside this program with your own Google sign-in -- no connector
or third party involved. One-time setup (see README, "Google Drive upload"):
create a Desktop OAuth client in Google Cloud, save its JSON as
secrets/google_oauth_client.json, then run `python upload_to_drive.py --auth`
once to approve access in your browser. That stores a refresh token in
secrets/google_token.json, and every later run (including scheduled ones)
uploads without asking again.

Drive converts the CSV to a native Google Sheet on upload, so no Sheets API is
needed. The upload is optional: if it isn't set up, or fails, the CSV in
output/ is still there and the run is not treated as failed.
"""
import os
import re
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

# Full Drive scope (not drive.file): drive.file can't write into a folder this
# app didn't create, and the target folder already exists.
SCOPES = ["https://www.googleapis.com/auth/drive"]
SECRETS_DIR = Path(__file__).resolve().parent.parent / "secrets"
CLIENT_FILE = SECRETS_DIR / "google_oauth_client.json"
TOKEN_FILE = SECRETS_DIR / "google_token.json"


def folder_id_from(value: str) -> str:
    """Accepts a bare folder ID or a full https://drive.google.com/drive/folders/<id> link."""
    match = re.search(r"folders/([\w-]+)", value or "")
    return match.group(1) if match else (value or "").strip()


def is_configured() -> bool:
    return bool(folder_id_from(os.environ.get("GOOGLE_DRIVE_FOLDER_ID", ""))) and CLIENT_FILE.exists()


def _credentials(interactive: bool):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except Exception:
            pass  # refresh token revoked/expired -- fall through to a fresh sign-in

    if not interactive:
        raise RuntimeError("Google sign-in is missing or expired. Run: python upload_to_drive.py --auth")

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
    creds = flow.run_local_server(port=0)
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds


def authorize() -> None:
    """One-time browser sign-in; stores the token for future (unattended) runs."""
    if not CLIENT_FILE.exists():
        raise RuntimeError(f"Missing {CLIENT_FILE}. See README, 'Google Drive upload'.")
    _credentials(interactive=True)
    print(f"[drive] signed in; token saved to {TOKEN_FILE}")


def upload_csv_as_sheet(csv_path: Path, folder: str, name: str | None = None, interactive: bool = False) -> str:
    """Uploads the CSV into the folder as a Google Sheet and returns its URL."""
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    service = build("drive", "v3", credentials=_credentials(interactive), cache_discovery=False)
    metadata = {
        "name": name or f"Waresport partners {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "parents": [folder_id_from(folder)],
        "mimeType": "application/vnd.google-apps.spreadsheet",
    }
    media = MediaFileUpload(str(csv_path), mimetype="text/csv", resumable=False)
    created = service.files().create(
        body=metadata, media_body=media, fields="id,webViewLink", supportsAllDrives=True
    ).execute()
    return created["webViewLink"]


def sheet_id_from(value: str) -> str:
    """Accepts a bare Sheet ID or a full https://docs.google.com/spreadsheets/d/<id>/... link."""
    match = re.search(r"/d/([\w-]+)", value or "")
    return match.group(1) if match else (value or "").strip()


def latest_sheet_in_folder(folder: str, name_prefix: str = "Waresport partners") -> tuple[str, str]:
    """(file_id, name) of the newest Google Sheet in the folder whose name starts
    with the prefix the automatic upload uses. Raises if there isn't one."""
    from googleapiclient.discovery import build

    service = build("drive", "v3", credentials=_credentials(interactive=False), cache_discovery=False)
    folder_id = folder_id_from(folder)
    response = service.files().list(
        q=(
            f"'{folder_id}' in parents and trashed = false "
            "and mimeType = 'application/vnd.google-apps.spreadsheet' "
            f"and name contains '{name_prefix}'"
        ),
        orderBy="createdTime desc",
        pageSize=1,
        fields="files(id,name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = response.get("files", [])
    if not files:
        raise RuntimeError(f"No Google Sheet named '{name_prefix}...' found in that Drive folder")
    return files[0]["id"], files[0]["name"]


def download_sheet_as_csv(sheet: str, dest_dir: Path) -> Path:
    """Exports a Google Sheet (first tab) to a local CSV and returns its path.
    `sheet` is a Sheet link/ID, or "latest" for the newest automatic upload in
    the configured Drive folder. Reads whatever is in the Sheet right now, so
    rows you deleted or edited there are what get used."""
    from googleapiclient.discovery import build

    if sheet.strip().lower() == "latest":
        folder = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
        if not folder:
            raise RuntimeError("Set GOOGLE_DRIVE_FOLDER_ID in .env to read the latest sheet from your Drive folder")
        file_id, name = latest_sheet_in_folder(folder)
    else:
        file_id, name = sheet_id_from(sheet), "(sheet)"

    service = build("drive", "v3", credentials=_credentials(interactive=False), cache_discovery=False)
    data = service.files().export(fileId=file_id, mimeType="text/csv").execute()
    dest_dir.mkdir(parents=True, exist_ok=True)
    out_path = dest_dir / f"source_sheet_{datetime.now().strftime('%Y-%m-%d_%H%M')}.csv"
    out_path.write_bytes(data)
    print(f"[drive] read Google Sheet {name} -> {out_path}")
    return out_path


def upload_if_configured(csv_path: Path) -> None:
    """Called after every run. Never raises: a failed upload must not lose or
    fail a run whose CSV was already written."""
    if not is_configured():
        print("[drive] upload skipped (not set up -- see README, 'Google Drive upload')")
        return
    try:
        url = upload_csv_as_sheet(csv_path, os.environ["GOOGLE_DRIVE_FOLDER_ID"])
        print(f"[drive] uploaded as a Google Sheet: {url}")
    except Exception as e:
        print(f"[drive] upload failed ({e}); the CSV is still at {csv_path}")
