import csv
import time
from pathlib import Path

from . import discovery, gdrive
from .enrich import enrich
from .llm_select import llm_client as openai_client
from .models import Partner
from .scrape_select import scrape_client as firecrawl_client

FIELDNAMES = [
    "competitor_name", "competitor_website", "competitor_description",
    "partner_name", "category", "fit",
    "description", "website", "website_confidence", "contact_page",
    "email_1", "email_2", "email_3", "email_confidence", "source_url", "notes",
]

DEFAULT_TARGET_COMPETITORS = 10
DEFAULT_MAX_PER_COMPETITOR = 15


def _process_competitor(
    competitor: dict,
    seen_keys: set,
    seen_names: set[str],
    seen_emails: set[str],
    delay: float,
    max_per_competitor: int,
    enrich_contacts: bool,
) -> list[Partner]:
    """Collects one competitor's partners/integrations, drops any that are
    already listed under an earlier competitor (same company domain, or -- once
    emails are looked up -- the same email), and returns what's left. An
    empty list means this competitor contributes nothing new, so the caller
    moves on to the next candidate."""
    name, website = competitor["name"], competitor["website"]

    print(f"  finding integrations/partners pages for {name}...")
    pages = discovery.find_integrations_pages(name, website)
    if not pages:
        print(f"    [skip] no integrations/partners page found for {name}")
        return []

    found: list[Partner] = []
    for url in pages:
        print(f"    scraping {url}")
        already = seen_names | {p.name.lower() for p in found}
        batch = discovery.scrape_integrations_page(name, website, url, skip_names=already)
        print(f"    found {len(batch)} new candidate partners")
        found.extend(batch)
        time.sleep(delay)
        if len(found) >= max_per_competitor:
            break

    fresh: list[Partner] = []
    for p in found:
        seen_names.add(p.name.lower())
        key = p.dedupe_key()
        if key in seen_keys:
            print(f"    [dup] {p.name} is already listed under another competitor -- skipping")
            continue
        seen_keys.add(key)
        fresh.append(p)
    fresh = fresh[:max_per_competitor]

    kept: list[Partner] = []
    for i, p in enumerate(fresh, 1):
        if enrich_contacts:
            print(f"    [{i}/{len(fresh)}] looking up email for {p.name} -> {p.website or '(no website resolved)'}")
            enrich(p, delay=delay)
            time.sleep(delay)
            emails = {e.lower() for e in p.emails}
            if emails & seen_emails:
                print(f"    [dup] {p.name} shares an email with an earlier row -- skipping")
                continue
            seen_emails.update(emails)
        kept.append(p)

    if kept:
        description = discovery.describe_competitor(name, website)
        for p in kept:
            p.competitor_description = description
    return kept


def collect(
    seed_names: list[str],
    expand: bool,
    delay: float,
    target_competitors: int = DEFAULT_TARGET_COMPETITORS,
    max_per_competitor: int = DEFAULT_MAX_PER_COMPETITOR,
    enrich_contacts: bool = True,
) -> list[Partner]:
    """Finds Waresport's competitors (OpenAI + DuckDuckGo), then works down
    that candidate list collecting each competitor's partners/integrations
    (page search + free/Firecrawl fetch + OpenAI structured extraction) and
    a contact email for each, until `target_competitors` competitors each have
    at least one partner that no earlier competitor already listed.

    Competitors with no findable integrations page, or whose partners were
    all already listed under an earlier competitor, don't count toward the
    target -- the loop just moves on to the next candidate (and, if the
    candidate list runs out, expands one hop to competitors of competitors).
    """
    if not openai_client.available():
        print("  [skip] OpenAI isn't configured -- needs OPENAI_API_KEY in .env")
        return []
    if not firecrawl_client.available():
        print("  [warn] Firecrawl isn't configured -- JS-rendered pages can't be scraped (free fetch + DuckDuckGo search still work)")

    print("  discovering Waresport's club-management competitors...")
    pool = discovery.discover_competitors(seed_names)
    print(f"    {len(pool)} candidate competitors: {', '.join(c['name'] for c in pool)}")

    expanded = expand
    if expand:
        print("  expanding one hop: competitors of competitors...")
        pool = discovery.discover_competitors_of_competitors(pool)
        print(f"    {len(pool)} candidates after expansion: {', '.join(c['name'] for c in pool)}")

    seen_keys: set = set()
    seen_names: set[str] = set()
    seen_emails: set[str] = set()
    tried: set[str] = set()
    partners: list[Partner] = []
    collected = 0
    queue = list(pool)

    while collected < target_competitors:
        if not queue:
            if expanded:
                break
            expanded = True
            print("  ran out of candidates -- expanding one hop: competitors of competitors...")
            pool = discovery.discover_competitors_of_competitors(pool)
            queue = [c for c in pool if c["name"].lower() not in tried]
            if not queue:
                break
            continue

        competitor = queue.pop(0)
        tried.add(competitor["name"].lower())
        print(f"\n  === {competitor['name']} (competitors with partners so far: {collected}/{target_competitors}) ===")
        kept = _process_competitor(
            competitor, seen_keys, seen_names, seen_emails, delay, max_per_competitor, enrich_contacts
        )
        if kept:
            collected += 1
            partners.extend(kept)
            print(f"    -> {competitor['name']}: {len(kept)} unique partners/integrations kept")
        else:
            print(f"    [skip] {competitor['name']}: nothing new to add -- trying the next competitor")

    if collected < target_competitors:
        print(f"\n  [warn] only found {collected} of {target_competitors} competitors with usable partner data -- ran out of candidates")
    return partners


def dedupe(partners: list[Partner]) -> list[Partner]:
    """Final safety pass. The real dedup happens inside collect() as each
    competitor is processed (so the competitor count reflects what survives);
    this just guarantees the exported sheet never has a repeated company."""
    seen = set()
    unique = []
    for p in partners:
        key = p.dedupe_key()
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)
    removed = len(partners) - len(unique)
    if removed:
        print(f"  removed {removed} duplicate row(s)")
    return unique


def _write_csv(partners_sorted: list[Partner], out_path: Path) -> None:
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for p in partners_sorted:
            writer.writerow(p.as_row())


def export(partners: list[Partner], out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Keep each competitor's rows together; niche/vertical fits (the real
    # outreach targets) surface first within the sheet.
    order = {"niche_vertical": 0, "": 1, "horizontal_platform": 2}
    partners_sorted = sorted(partners, key=lambda p: (order.get(p.fit, 1), p.competitor, p.name))

    try:
        _write_csv(partners_sorted, out_path)
    except PermissionError:
        # Usually means the file is open in Excel. Don't lose the run's
        # results over that -- write alongside it under a free name instead.
        alt_path = out_path
        n = 1
        while True:
            alt_path = out_path.with_name(f"{out_path.stem}_{n}{out_path.suffix}")
            if not alt_path.exists():
                break
            n += 1
        print(f"\n[warn] {out_path} is locked (probably open in Excel) -- writing to {alt_path} instead.")
        _write_csv(partners_sorted, alt_path)
        out_path = alt_path

    print(f"\nWrote {len(partners_sorted)} rows to {out_path}")
    return out_path


def run(
    enrich_contacts: bool,
    delay: float,
    out_path: Path,
    seed_names: list[str] | None = None,
    expand: bool = False,
    max_competitors: int = DEFAULT_TARGET_COMPETITORS,
    max_per_competitor: int = DEFAULT_MAX_PER_COMPETITOR,
    upload_to_drive: bool = True,
) -> list[Partner]:
    print("Discovering competitors and their partners...")
    partners = collect(
        seed_names or [],
        expand=expand,
        delay=delay,
        target_competitors=max_competitors,
        max_per_competitor=max_per_competitor,
        enrich_contacts=enrich_contacts,
    )

    print("\nFinal duplicate check...")
    partners = dedupe(partners)

    final_path = export(partners, out_path)
    if upload_to_drive and partners:
        gdrive.upload_if_configured(final_path)

    competitor_count = len({p.competitor for p in partners})
    niche = sum(1 for p in partners if p.fit == "niche_vertical")
    verified = sum(1 for p in partners if p.email_confidence == "verified_found")
    guessed = sum(1 for p in partners if p.email_confidence == "pattern_guess")
    print(
        f"\nSummary: {competitor_count} competitors, {len(partners)} unique partners/integrations total, "
        f"{niche} flagged niche_vertical (best outreach fit). Emails: {verified} verified, {guessed} convention guesses."
    )
    print("Remember: verify every guessed website and every auto-found/guessed email before sending outreach.")

    return partners
