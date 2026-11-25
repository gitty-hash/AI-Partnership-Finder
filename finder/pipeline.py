import csv
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import discovery, fit, gdrive
from .enrich import enrich
from .lead_store import LeadStore, default_store, key_for
from .llm_select import llm_client as openai_client
from .models import Partner
from .scrape_select import scrape_client as firecrawl_client

FIELDNAMES = [
    "competitor_name", "competitor_website", "competitor_description",
    "partner_name", "category", "fit", "fit_score", "fit_reason",
    "description", "website", "website_confidence", "contact_page",
    "email_1", "email_2", "email_3", "email_confidence", "email_source", "source_url",
    "is_new", "first_seen", "lead_status", "notes",
]

DEFAULT_TARGET_COMPETITORS = 10
DEFAULT_MAX_PER_COMPETITOR = 15
# Contact lookups for different partners hit different websites, so they can run
# side by side. Shared services (DuckDuckGo, Firecrawl) keep their own global pacing.
DEFAULT_ENRICH_WORKERS = int(os.environ.get("ENRICH_WORKERS", "4"))
# A verified email found in an earlier run is reused for this long instead of re-crawling.
KNOWN_CONTACT_MAX_AGE_DAYS = int(os.environ.get("KNOWN_CONTACT_MAX_AGE_DAYS", "30"))
# Fit types that are listed in the sheet but never worth a contact lookup.
NOT_A_PARTNER = frozenset({"customer", "competitor"})


def _apply_known_contact(partner: Partner, contact: dict) -> None:
    partner.emails = list(contact["emails"])
    partner.email_confidence = contact["email_confidence"]
    partner.email_source = "known"
    partner.contact_page = partner.contact_page or contact["contact_page"]
    partner.notes = (partner.notes + " " if partner.notes else "") + f"Email reused from an earlier run (checked {contact['checked_at'][:10]})."


def _enrich_all(partners: list[Partner], *, delay: float, workers: int, store: LeadStore | None, reused: set[str]) -> None:
    """Fills in contacts: reuses recent verified emails from the lead store, and
    looks the rest up in parallel. Results stay in the original order."""
    todo = []
    for p in partners:
        contact = store.known_contact(p, KNOWN_CONTACT_MAX_AGE_DAYS) if store else None
        if contact:
            _apply_known_contact(p, contact)
            reused.add(key_for(p))
            print(f"    [known] {p.name}: reusing {contact['emails'][0]} from an earlier run")
        else:
            todo.append(p)
    if not todo:
        return

    def run(indexed):
        i, p = indexed
        print(f"    [{i}/{len(todo)}] looking up email for {p.name} -> {p.website or '(no website resolved)'}")
        try:
            enrich(p, delay=delay)
        except Exception as e:  # one bad site must not lose the rest of the run
            print(f"    [warn] contact lookup failed for {p.name}: {type(e).__name__}: {e}")
            p.emails, p.email_source = [], ""
            p.email_confidence = "not_found"
            p.notes = (p.notes + " " if p.notes else "") + "Contact lookup failed -- will be retried on the next run."
        time.sleep(delay)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(run, enumerate(todo, 1)))


def _process_competitor(
    competitor: dict,
    seen_keys: set,
    seen_names: set[str],
    seen_emails: set[str],
    delay: float,
    max_per_competitor: int,
    enrich_contacts: bool,
    *,
    store: LeadStore | None = None,
    reused: set[str] | None = None,
    workers: int = DEFAULT_ENRICH_WORKERS,
) -> list[Partner]:
    """Collects one competitor's partners/integrations, drops any that are
    already listed under an earlier competitor (same company domain, or -- once
    emails are looked up -- the same email), and returns what's left. An
    empty list means this competitor contributes nothing new, so the caller
    moves on to the next candidate."""
    name, website = competitor["name"], competitor["website"]
    reused = reused if reused is not None else set()

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
    if not fresh:
        return []

    # Score first: clubs/leagues (customers) and competing software aren't partnership
    # targets, so they are listed but not worth a contact crawl.
    fit.score_partners(fresh, product_description=discovery.WARESPORT_DESCRIPTION, competitor_name=name)

    if enrich_contacts:
        to_enrich = [p for p in fresh if p.fit not in NOT_A_PARTNER]
        for p in fresh:
            if p.fit in NOT_A_PARTNER:
                p.email_confidence = "not_found"
                p.notes = (p.notes + " " if p.notes else "") + f"Scored as a {p.fit}, not a partner -- contact lookup skipped."
        _enrich_all(to_enrich, delay=delay, workers=workers, store=store, reused=reused)

    kept: list[Partner] = []
    for p in fresh:
        if enrich_contacts:
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
    *,
    store: LeadStore | None = None,
    reused: set[str] | None = None,
    workers: int = DEFAULT_ENRICH_WORKERS,
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
        try:
            kept = _process_competitor(
                competitor, seen_keys, seen_names, seen_emails, delay, max_per_competitor, enrich_contacts,
                store=store, reused=reused, workers=workers,
            )
        except Exception as e:  # keep everything collected so far and move on
            print(f"    [warn] {competitor['name']} failed: {type(e).__name__}: {e}")
            kept = []
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


def _free_name(path: Path) -> Path:
    """path, or path_1/path_2/... when it already exists (a second run the same day
    must not overwrite the first run's sheet)."""
    if not path.exists():
        return path
    n = 1
    while True:
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def sort_for_export(partners: list[Partner]) -> list[Partner]:
    """Each competitor's rows stay together; within a competitor, new leads first,
    then the best fits."""
    return sorted(
        partners,
        key=lambda p: (p.competitor.lower(), not p.is_new, -(p.fit_score if p.fit_score is not None else -1), p.name.lower()),
    )


def export(partners: list[Partner], out_path: Path, *, overwrite: bool = False) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not overwrite:
        out_path = _free_name(out_path)
    partners_sorted = sort_for_export(partners)

    try:
        _write_csv(partners_sorted, out_path)
    except PermissionError:
        # Usually means the file is open in Excel. Don't lose the run's
        # results over that -- write alongside it under a free name instead.
        alt_path = _free_name(out_path.with_name(f"{out_path.stem}_1{out_path.suffix}"))
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
    *,
    only_new: bool = False,
    use_memory: bool = True,
    workers: int = DEFAULT_ENRICH_WORKERS,
    overwrite: bool = False,
) -> list[Partner]:
    store = default_store() if use_memory else None
    reused: set[str] = set()

    print("Discovering competitors and their partners...")
    partners = collect(
        seed_names or [],
        expand=expand,
        delay=delay,
        target_competitors=max_competitors,
        max_per_competitor=max_per_competitor,
        enrich_contacts=enrich_contacts,
        store=store,
        reused=reused,
        workers=workers,
    )

    print("\nFinal duplicate check...")
    partners = dedupe(partners)

    memory = {"new": len(partners), "known": 0}
    if store:
        memory = store.record_run(partners, reused_contacts=reused)
        print(f"Lead memory: {memory['new']} new lead(s), {memory['known']} already known ({store.path}).")
    exported = [p for p in partners if p.is_new] if only_new else partners

    final_path = export(exported, out_path, overwrite=overwrite)
    if upload_to_drive and exported:
        gdrive.upload_if_configured(final_path)

    competitor_count = len({p.competitor for p in partners})
    strong = sum(1 for p in partners if (p.fit_score or 0) >= 70)
    verified = sum(1 for p in partners if p.email_confidence == "verified_found")
    guessed = sum(1 for p in partners if p.email_confidence == "pattern_guess")
    print(
        f"\nSummary: {competitor_count} competitors, {len(partners)} unique partners/integrations "
        f"({memory['new']} new, {memory['known']} seen before), {strong} strong fits (score 70+). "
        f"Emails: {verified} verified ({len(reused)} reused from earlier runs), {guessed} convention guesses."
    )
    if only_new:
        print(f"Exported only the {len(exported)} new lead(s) (--only-new).")
    print("Remember: verify every guessed website and every auto-found/guessed email before sending outreach.")

    return partners
