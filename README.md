# Waresport Partnership Finder

Finds partnership leads for [Waresport](https://www.waresport.com) (sports club management software) and reaches out to them.

1. **Finds** Waresport's competitors.
2. **Collects** each competitor's partners and integrations, scores how good a partnership fit each one is (0-100), finds a verified contact email, and saves them as a CSV and a Google Sheet.
3. **Remembers** every partner across runs (lead memory), so each sheet shows what's new and where every conversation stands.
4. **Drafts and sends** a researched, personalized partnership email to each partner, after a human approves it.
5. **Tracks** replies, opt-outs and bounces from the inbox, drafts follow-ups for anyone who hasn't answered, and reports reply and meeting rates.

It runs locally on your machine, and lead finding is scheduled to run twice a week.

## How it works

| Stage | Command | What happens |
|---|---|---|
| 1. Find leads | `python main.py` | Competitors are discovered, their partner/integration pages are scraped, partners are de-duplicated across competitors and fit-scored, emails are looked up (or reused from an earlier run), and a CSV (plus a Google Sheet copy) is produced. Every partner is recorded in the lead memory. Aims for 10 competitors that each have at least one partner not already listed in that run. |
| 2. Schedule | `schedule_task.ps1` | A Windows scheduled task runs stage 1 every Monday and Thursday at 8 AM Pacific. |
| 3. Outreach | `python send_outreach.py draft` / `send` | Reads the sheet, researches each partner, drafts a fact-checked email, waits for your approval, then sends through Gmail. |
| 4. Track | `python send_outreach.py sync` / `followups` / `report` | Reads replies, opt-outs and bounces (read-only), drafts follow-ups after 4 and 9 days of silence, and reports reply, meeting and bounce rates. |

## Services used, and for what

| Service | Used for | Cost / key |
|---|---|---|
| **OpenAI** (`gpt-4o-mini`) | Naming competitors; choosing the right website from search results; extracting partner names from messy pages; finding emails in page text; describing competitors; the partner research, analysis, email writing and fact-check | Paid per use. `OPENAI_API_KEY` |
| **DuckDuckGo** (`ddgs` package) | All web searches (competitor lists, websites, integration pages, news, contact snippets) | Free, no key |
| **Firecrawl** | Scraping pages a plain fetch can't read (JavaScript-rendered or blocked), including contact pages. Also a fallback if DuckDuckGo returns nothing. Calls are rate-limited and cached for 24h | Paid / free tier. `FIRECRAWL_API_KEY` |
| **Google Drive API** | Uploads each run's sheet to a Drive folder as a Google Sheet; reads the latest sheet for outreach | Free. Your own Google sign-in (see below) |
| **Gmail SMTP / IMAP** | Sends the outreach emails; `sync` reads replies and bounces (read-only) | Free. Gmail App Password (turn on IMAP in Gmail settings for `sync`) |
| **DNS** (`dnspython`) | Checks that a contact email's domain can receive mail before it is labelled verified | Free, no key |
| *Gemini, Crawl4AI* | Optional comparison experiments only. Off by default | Not needed |

## Setup

**Requirements:** Python 3.10+.

```bash
git clone <this repo>
cd partnership-finder
pip install -r requirements.txt
```

`crawl4ai` and `google-genai` in `requirements.txt` are only for the optional experiments; you can remove them if you don't need those.

### 1. Keys: copy `.env.example` to `.env` and fill it in

| Key | Needed for | How to get it |
|---|---|---|
| `OPENAI_API_KEY` | **Required** | platform.openai.com > API keys |
| `FIRECRAWL_API_KEY` | Recommended (JS-heavy pages are skipped without it) | firecrawl.dev > API keys |
| `GOOGLE_DRIVE_FOLDER_ID` | Optional: Drive upload | Folder link or the ID at the end of its URL |
| `OUTREACH_SENDER_NAME`, `OUTREACH_SENDER_TITLE` | Sending emails | Your name and job title (signature) |
| `OUTREACH_FROM_EMAIL` | Sending emails | The Gmail address you send from |
| `OUTREACH_MAILING_ADDRESS` | Sending emails | A real postal address, printed in every email footer (required by US law) |
| `SMTP_PASSWORD` | Sending emails | Gmail **App Password** (16 characters), not your normal password. Google Account > Security > turn on 2-Step Verification > App passwords. Or run `python send_outreach.py setup` |
| `OUTREACH_REPLY_TO`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `OUTREACH_DAILY_CAP`, `OUTREACH_COMPANY`, `OUTREACH_COMPANY_WEBSITE` | Optional | Defaults: Gmail SMTP on 587, 25 sends/day, Waresport |

`GOOGLE_API_KEY` (Gemini) and `CRAWL4AI_API_KEY` are **not used** and are commented out in `.env.example`.

### 2. Google Drive upload (optional, one-time)

1. In [Google Cloud Console](https://console.cloud.google.com), create or pick a project and enable the **Google Drive API**.
2. Set up the **OAuth consent screen**. Choose *Internal* for a Workspace account. Otherwise choose *External*, add yourself as a test user, then click **Publish app** (apps left in "Testing" sign you out after 7 days).
3. **Credentials > Create credentials > OAuth client ID > Desktop app**. Download the JSON and save it as `secrets/google_oauth_client.json`.
4. Set `GOOGLE_DRIVE_FOLDER_ID` in `.env`, then run `python upload_to_drive.py --auth` once and approve access in the browser.

After that, every run uploads automatically. Skip it for one run with `--no-drive`.

## Usage

### Find leads
```bash
python main.py                       # full run: 10 competitors, partners, emails, CSV + Drive upload
python main.py --max-competitors 5   # quicker test run
python main.py --no-enrich           # skip email lookup (fast)
python main.py --no-drive            # don't upload to Drive
python main.py --seed CourtReserve   # make sure a competitor you care about is included
python main.py --out my_leads.csv
python main.py --only-new            # the sheet lists only partners no earlier run found
python main.py --no-memory           # ignore the lead memory for this run (everything counts as new)
python main.py --workers 6           # look up more partners' contacts at the same time (default 4)
```
Other options: `--expand`, `--max-per-competitor`, `--delay`. See `python main.py --help`.

Output: `output/leads_<date>.csv` (a second run the same day writes `leads_<date>_1.csv`; nothing is overwritten) with the columns `competitor_name, competitor_website, competitor_description, partner_name, category, fit, fit_score, fit_reason, description, website, website_confidence, contact_page, email_1-3, email_confidence, email_source, source_url, is_new, first_seen, lead_status, notes`. Rows are grouped by competitor, new leads and best fits first.

- `fit` / `fit_score` / `fit_reason`: the AI scores each competitor's partners together. `fit` is `niche_vertical` (sports-specific vendor, the best targets), `horizontal_platform` (Stripe, Google, Mailchimp...; always capped at 40), `customer` (clubs and leagues), `competitor` or `other`. Customers and competitors are listed but their contacts aren't looked up.
- `email_confidence` is `verified_found`, `pattern_guess` (an `info@domain` guess, NOT verified) or `not_found`. **Verified** now means the address was really on a page (an AI may only report an address that appears in the text it read, or one on the company's own domain), and its domain accepts mail (MX/DNS check). Addresses on a domain that can't receive mail are dropped.
- `email_source`: `page`, `search_snippet`, `page_llm`, `web_search`, `guess`, or `known` (reused from an earlier run's verified email, up to 30 days old).
- `is_new` / `first_seen` / `lead_status`: from the lead memory (below).

### Lead memory
Every run records its partners in `data/leads.db` (SQLite, git-ignored): one row per company, with when it was first and last seen, which competitors list it, its best verified email, its fit score, and an outreach status with a full history:

`new → drafted → contacted → replied → meeting → partner`, or a stop state `not_interested`, `bounced`, `do_not_contact`.

Statuses move forward on their own (drafting, sending, and `sync` reading the inbox), never backwards. Leads that replied, booked a meeting, opted out or bounced are never drafted or followed up again. Set a status by hand when something happens outside the inbox:
```bash
python send_outreach.py status acme.com meeting --note "Call booked for Tuesday"
python send_outreach.py status jo@acme.com not_interested   # also adds acme.com to the suppression list
python send_outreach.py leads --status replied              # list leads, best fits first
```

### Run it on a schedule (Windows)
```powershell
.\schedule_task.ps1            # Mondays and Thursdays, 8 AM Pacific (converted to your local time)
.\schedule_task.ps1 -Remove    # remove the schedule
```
The computer must be on. Re-run the script when daylight saving time changes. Logs go to `logs/`.

### Send outreach emails
```bash
python send_outreach.py draft --sheet            # draft from the newest Google Sheet in your Drive folder
python send_outreach.py draft --input output/leads_x.csv
#   open outreach/drafts_<time>.csv, edit if you like, put "yes" in the approved column
python send_outreach.py send --drafts outreach/drafts_<time>.csv --dry-run
python send_outreach.py send --drafts outreach/drafts_<time>.csv --test-to you@example.com
python send_outreach.py send --drafts outreach/drafts_<time>.csv          # real send, type SEND to confirm
```
**Always do the dry run and the `--test-to` send to your own inbox first.**

How drafting works (per partner):
1. **Pick an address:** one email per company, to its best trustworthy address (partnership/business inboxes first; HR, support and press last). The other good addresses are listed in the `backup_emails` column. `--all-addresses` writes to up to 3 instead. Skipped, and printed with a reason: no email, unverified guessed addresses, broken or unrelated-domain addresses, duplicates, anyone already contacted, anyone in `outreach/suppression.txt`, and leads the lead memory has as replied, meeting, partner, not interested or bounced.
2. **Fit check:** clear non-partners (clubs, leagues, associations, competitors) are skipped as `not a fit`. If the check itself fails (AI service error), the row is marked `failed` and retried on the next draft run, never skipped as a non-fit.
3. **Deep dive:** the brand's own pages and recent news are gathered; a fact sheet is built only from that evidence; the fit with Waresport is analyzed; a 150-200 word pitch is written; a fact-check pass rejects unsupported claims (rewrite once, otherwise no draft).
4. **Review:** the drafts CSV shows the research summary, analysis and source URLs next to each email.
5. **Send:** through Gmail, about 45 seconds apart, at most 25 a day. The signature, postal address and opt-out line are added by code. The "from" name in the email body comes from `OUTREACH_SENDER_NAME` / `OUTREACH_SENDER_TITLE`, the same as the signature. Every send is logged in `outreach/sent_log.csv` (with its Message-ID), so nobody is emailed twice, and the lead becomes `contacted`.

### Track replies and follow up
```bash
python send_outreach.py sync         # read the inbox: replies, opt-outs, bounces (read-only)
python send_outreach.py followups    # draft follow-ups for companies that haven't replied
python send_outreach.py send --drafts outreach/followups_<time>.csv --dry-run   # then approve and send as usual
python send_outreach.py report       # how outreach is doing
```
- **`sync`** opens the sending Gmail inbox **read-only** (nothing is marked read or moved) over IMAP, with the same App Password (turn on IMAP in Gmail settings). A reply from a contacted address or company marks the lead `replied`. "Not interested", "unsubscribe" and similar replies mark it `not_interested` and add the company domain to the suppression list. A bounce marks it `bounced` and suppresses the address. Out-of-office replies are ignored. Run it before `followups`.
- **`followups`** drafts at most two short follow-ups per company, after 4 and 9 days without a reply (`--first`, `--second`), to the best address it was emailed at, threaded under the earlier email (`Re:` subject, `In-Reply-To`). The text is fixed, not AI-written, so there is nothing new to fact-check. Companies that replied, opted out or bounced are never followed up, and first emails older than 30 days (`--max-age`) get none. Follow-ups go through the same approve → send step.
- **`report`** prints companies contacted, response rate, positive replies, meetings booked, partners signed, opt-outs, bounce rate, follow-ups due, and new leads in the last 7 days.

## What is customised for Waresport (change these to reuse it for another company)
- `finder/discovery.py`: `WARESPORT_DESCRIPTION` and the `Waresport alternatives competitors` search query.
- `finder/deep_dive.py`: `WARESPORT_FACTS`, the only Waresport facts the email writer may state. Keep it in line with the website.

## Project files

| File | What it does |
|---|---|
| `main.py` | Command-line entry point for stage 1 (finding leads) |
| `send_outreach.py` | Command-line entry point for outreach: `draft`, `send`, `setup`, `sync`, `followups`, `report`, `status`, `leads` |
| `upload_to_drive.py` | Manual Google Drive sign-in (`--auth`) and upload of a CSV |
| `run_scheduled.ps1` | One scheduled run: runs `main.py` and writes a log |
| `schedule_task.ps1` | Creates or removes the Windows scheduled task (Mon/Thu 8 AM Pacific) |
| `requirements.txt` | Python dependencies |
| `.env.example` | Template listing every setting; copy to `.env` |
| `compare_gemini.py` | One-off script that compared Gemini and OpenAI competitor lists (not part of the pipeline) |
| `finder/pipeline.py` | Orchestrates a run: loops through competitors until the target is met, de-duplicates, fit-scores, enriches in parallel (reusing known emails), records the run in the lead memory, exports the CSV, uploads to Drive |
| `finder/discovery.py` | Finds competitors and websites, finds integration pages, extracts partners, describes competitors |
| `finder/enrich.py` | Finds a contact email per partner (escalating methods, ending in a labeled guess); every address is checked before it counts as verified |
| `finder/email_verify.py` | Deliverability checks: the email's domain accepts mail (MX/DNS), the address appears in the text, it's on the company's domain |
| `finder/robots.py` | Cached robots.txt checks with a timeout; a disallowed page is never fetched, by either path |
| `finder/fit.py` | Fit scoring: one AI call per competitor, giant platforms capped, heuristic fallback |
| `finder/lead_store.py` | Lead memory (SQLite `data/leads.db`): every partner across runs, statuses and history |
| `finder/inbox.py` | Read-only IMAP inbox reader and reply / opt-out / bounce / auto-reply classifier |
| `finder/models.py` | The `Partner` record, the company-domain helper and the cross-competitor duplicate key |
| `finder/search.py` | Web search: DuckDuckGo first, Firecrawl search as fallback |
| `finder/ddg_client.py` | DuckDuckGo wrapper (paced, retried) |
| `finder/page_fetch.py` | Fetches a page: free request first, Firecrawl only if needed; a plain 404 is never escalated |
| `finder/firecrawl_client.py` | The only gateway to Firecrawl: shared rate limit, retries with backoff, 24h cache |
| `finder/openai_client.py` | Every OpenAI call (extraction, picking, describing, competitor lists, email lookup) |
| `finder/deep_dive.py` | Research, analysis, email writing and fact-checking for outreach |
| `finder/outreach.py` | Outreach logic: address selection and rules, fit check, drafts file, Gmail sending, send log, follow-ups, inbox sync, report |
| `tests/` | `python -m pytest tests`: 30 tests, no network or API keys needed |
| `finder/gdrive.py` | Google Drive sign-in, upload as a Sheet, reading the latest Sheet |
| `finder/llm_select.py`, `finder/scrape_select.py` | Switches for the optional Gemini / Crawl4AI experiments; defaults to OpenAI / Firecrawl |
| `finder/gemini_client.py`, `finder/crawl4ai_client.py` | Optional experiment backends, off by default |
| `finder/courtreserve.py`, `finder/seeds.py` | First-version scrapers for CourtReserve and PodPlay; no longer used |
| `finder/__init__.py` | Marks `finder` as a Python package |

Folders created while running (all git-ignored): `output/` (CSVs), `outreach/` (drafts, follow-up drafts, sent log, suppression list, downloaded sheets), `data/` (lead memory), `logs/`, `secrets/` (Google sign-in files), `.cache/` (Firecrawl cache).

Run the tests with `python -m pytest tests` (no network or API keys needed).

## Important notes
- **This is a starting point, not a verified list.** Extraction is LLM-driven and can be wrong. Check websites with `website_confidence` other than `llm_picked`, treat `pattern_guess` emails as guesses, and read each draft before approving it.
- **Cold email is regulated.** The tool adds a postal address and an easy opt-out (US CAN-SPAM). Recipients in Canada (CASL) and the EU/UK (GDPR) have stricter rules, so check before mailing them. Add anyone who says "no thanks" to `outreach/suppression.txt` (one email or domain per line).
- **Keep keys private.** `.env` and `secrets/` are git-ignored; never commit them. If a key is ever shared in chat or a screenshot, rotate it.
- A new Gmail sending address should start small (the default cap is 25 a day).
