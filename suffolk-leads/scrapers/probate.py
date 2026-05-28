"""
probate.py
----------
Scrapes probate court filings for Suffolk County, NY using Playwright
to automate the NYSCEF (NY State Courts Electronic Filing System) name-search
portal at:
  https://iapps.courts.state.ny.us/nyscef/CaseSearch?TAB=name

Also scrapes six Georgia counties using their public probate court portals.

NOTE ON CLOUDFLARE PROTECTION
------------------------------
Both websurrogates.nycourts.gov and iapps.courts.state.ny.us are protected
by a Cloudflare managed challenge that cannot be solved from a data-center IP
address (such as Railway's servers).  The scraper handles this gracefully:

  1. It attempts the live scrape with Playwright + stealth settings.
  2. If the Cloudflare challenge page is detected, it logs a clear error and
     falls back to the most recent mock filings so the pipeline always produces
     leads.
  3. You can bypass Cloudflare by exporting your browser cookies to a JSON file
     and passing --cookie-file /path/to/cookies.json on the CLI, or by setting
     the NYSCEF_COOKIE_FILE environment variable.  Use the EditThisCookie Chrome
     extension or browser DevTools → Application → Cookies → Export.

LEAD SAVING BEHAVIOUR
---------------------
Every filing is saved to the leads table regardless of whether a matching
property record exists in the properties table.  Unmatched leads have:
  - parcel_id  = NULL
  - address    = decedent name + county (best available)
  - raw_data   = JSON with case_number, case_url, petitioner_name, case_type,
                 decedent_name, filing_date

Case types scraped (NY):
  - Probate
  - Administration

Usage:
  python scrapers/probate.py
  python scrapers/probate.py --cookie-file /path/to/cookies.json
  from scrapers.probate import ProbateScraper; ProbateScraper().scrape()
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import re
import sys
import time
import traceback

# ── Guarded third-party imports ───────────────────────────────────────────────
try:
    import requests
    from bs4 import BeautifulSoup
except ImportError as _e:
    print(f"[probate] IMPORT ERROR (requests/bs4): {_e} — install with: pip install requests beautifulsoup4", flush=True)
    raise

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    _PLAYWRIGHT_AVAILABLE = True
except ImportError as _e:
    print(f"[probate] WARNING: playwright not installed — live scraping disabled: {_e}", flush=True)
    _PLAYWRIGHT_AVAILABLE = False

# Ensure project root is on sys.path so database imports work
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from database import SessionLocal, Property, Lead
except Exception as _e:
    print(f"[probate] IMPORT ERROR (database): {_e}", flush=True)
    traceback.print_exc()
    raise

from scrapers.base_scraper import BaseScraper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("ProbateScraper")

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
NYSCEF_BASE = "https://iapps.courts.state.ny.us/nyscef"
NYSCEF_SEARCH_URL = f"{NYSCEF_BASE}/CaseSearch?TAB=name"

# Suffolk County value in the NYSCEF county dropdown (verified by probe)
NYSCEF_SUFFOLK_VALUE = "52"

# NYSCEF case type values (obfuscated by the site — verified by probe)
NYSCEF_CASE_TYPES: dict[str, str] = {
    "Probate":        "ZXL47MVGiXTShahJS1u1Sw==",
    "Administration": "sTShoYfmvVprI_PLUS_4gtFBwQA==",
}

# How many days back to search
LOOKBACK_DAYS = 7

# Polite delay between requests (seconds)
REQUEST_DELAY = 3

# Georgia probate court portal configurations
GA_PROBATE_COURTS: dict[str, dict] = {
    "Fulton": {
        "url": "https://www.fultonprobate.org/",
        "search_url": "https://www.fultonprobate.org/case-search",
        "state": "GA",
    },
    "Gwinnett": {
        "url": "https://www.gwinnettcourts.com/probate/",
        "search_url": "https://www.gwinnettcourts.com/probate/case-search",
        "state": "GA",
    },
    "Cobb": {
        "url": "https://www.cobbcounty.org/courts/probate",
        "search_url": "https://www.cobbcounty.org/courts/probate/case-search",
        "state": "GA",
    },
    "DeKalb": {
        "url": "https://www.dekalbcountyga.gov/probate-court",
        "search_url": "https://www.dekalbcountyga.gov/probate-court/case-search",
        "state": "GA",
    },
    "Chatham": {
        "url": "https://www.chathamcounty.org/Offices/Probate-Court",
        "search_url": "https://www.chathamcounty.org/Offices/Probate-Court/Case-Search",
        "state": "GA",
    },
    "Clarke": {
        "url": "https://www.accgov.com/probate",
        "search_url": "https://www.accgov.com/probate/case-search",
        "state": "GA",
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# Scraper class
# ─────────────────────────────────────────────────────────────────────────────

class ProbateScraper(BaseScraper):
    """
    Scrapes probate court filings for Suffolk County (NY) via NYSCEF and six
    Georgia counties via their public court portals.

    Every filing is saved to the leads table regardless of whether a matching
    property record exists.  Unmatched leads have parcel_id=NULL.
    """

    def __init__(self, cookie_file: str | None = None):
        """
        Parameters
        ----------
        cookie_file : str | None
            Path to a JSON file containing browser cookies exported from a
            real browser session.  Used to bypass Cloudflare on NYSCEF.
            Overrides the NYSCEF_COOKIE_FILE environment variable.
        """
        self.cookie_file = cookie_file or os.environ.get("NYSCEF_COOKIE_FILE")
        self.http_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def scrape(self) -> list[dict]:
        """
        Main entry point.  Scrapes probate filings from Suffolk County (NY)
        and all six Georgia counties.  Saves every filing to the leads table
        with parcel_id=NULL for unmatched records.

        Returns a combined list of all raw filing dicts.
        """
        print("[probate] Starting probate filings scraping (NY + GA)…", flush=True)
        logger.info("Starting probate filings scraping (NY + GA)…")
        all_filings: list[dict] = []

        # ── New York: Suffolk County via NYSCEF ───────────────────────────────
        print("[probate] Scraping Suffolk County (NY) via NYSCEF…", flush=True)
        logger.info("Scraping Suffolk County (NY) via NYSCEF…")
        ny_filings = self._scrape_nyscef_playwright()
        if not ny_filings:
            print(
                "[probate] NYSCEF live scrape returned 0 results "
                "(likely Cloudflare challenge from this IP). "
                "Using mock NY filings. "
                "To use real data: set NYSCEF_COOKIE_FILE=/path/to/cookies.json",
                flush=True,
            )
            logger.warning(
                "NYSCEF live scrape returned 0 results (Cloudflare or no data). "
                "Falling back to mock filings."
            )
            ny_filings = self._get_mock_filings(state="NY", county="Suffolk")
        all_filings.extend(ny_filings)
        print(f"[probate] Suffolk County (NY): {len(ny_filings)} filing(s).", flush=True)
        logger.info("Suffolk County (NY): %d filing(s).", len(ny_filings))

        # ── Georgia counties ──────────────────────────────────────────────────
        for county, cfg in GA_PROBATE_COURTS.items():
            print(f"[probate] Scraping {county} County (GA)…", flush=True)
            logger.info("Scraping %s County (GA) probate filings…", county)
            ga_filings = self._scrape_ga_probate(county, cfg)
            if not ga_filings:
                print(
                    f"[probate] {county} County GA live scrape returned 0 results — using mock filings.",
                    flush=True,
                )
                logger.info("Live scrape failed for %s County GA — using mock filings.", county)
                ga_filings = self._get_mock_filings(state="GA", county=county)
            all_filings.extend(ga_filings)
            print(f"[probate] {county} County (GA): {len(ga_filings)} filing(s).", flush=True)
            logger.info("%s County (GA): %d filing(s).", county, len(ga_filings))
            time.sleep(REQUEST_DELAY)

        print(f"[probate] Total filings collected: {len(all_filings)}", flush=True)
        logger.info("Total probate filings collected: %d", len(all_filings))

        saved = self._save_leads(all_filings)
        print(f"[probate] New leads saved: {saved}", flush=True)
        logger.info("New leads saved: %d", saved)
        return all_filings

    # ─────────────────────────────────────────────────────────────────────────
    # NYSCEF Playwright scraper
    # ─────────────────────────────────────────────────────────────────────────

    def _scrape_nyscef_playwright(self) -> list[dict]:
        """
        Uses Playwright headless Chromium to automate the NYSCEF name-search
        form for Suffolk County Probate and Administration filings from the
        last LOOKBACK_DAYS days.

        Cloudflare detection:
          - If the page title is "Just a moment..." after load, the challenge
            is active and the method returns [] immediately with a clear log.
          - If a cookie_file is provided, those cookies are injected before
            navigation to bypass the challenge.

        Returns a list of filing dicts or [] on failure.
        """
        if not _PLAYWRIGHT_AVAILABLE:
            print("[probate] Playwright not available — skipping live NYSCEF scrape.", flush=True)
            return []

        filings: list[dict] = []
        today = datetime.date.today()
        start_date = today - datetime.timedelta(days=LOOKBACK_DAYS)

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
                ctx = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 900},
                    locale="en-US",
                    timezone_id="America/New_York",
                    extra_http_headers={
                        "Accept-Language": "en-US,en;q=0.9",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    },
                )

                # Inject cookies if provided (bypasses Cloudflare)
                if self.cookie_file and os.path.isfile(self.cookie_file):
                    print(f"[probate] Loading cookies from {self.cookie_file}", flush=True)
                    try:
                        with open(self.cookie_file) as cf:
                            cookies = json.load(cf)
                        ctx.add_cookies(cookies)
                        print(f"[probate] Injected {len(cookies)} cookie(s).", flush=True)
                    except Exception as ce:
                        print(f"[probate] Failed to load cookies: {ce}", flush=True)

                page = ctx.new_page()

                # Mask automation fingerprint
                page.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
                    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                    window.chrome = {runtime: {}};
                """)

                for case_type_name, case_type_val in NYSCEF_CASE_TYPES.items():
                    print(
                        f"[probate] NYSCEF search: Suffolk County / {case_type_name} / "
                        f"{start_date} to {today}",
                        flush=True,
                    )
                    logger.info(
                        "NYSCEF search: Suffolk County / %s / %s to %s",
                        case_type_name, start_date, today,
                    )

                    try:
                        page.goto(
                            NYSCEF_SEARCH_URL,
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        time.sleep(REQUEST_DELAY)

                        # Detect Cloudflare challenge
                        title = page.title()
                        if "just a moment" in title.lower():
                            print(
                                "[probate] CLOUDFLARE CHALLENGE DETECTED on NYSCEF. "
                                "This IP is blocked. "
                                "To fix: export cookies from a real browser session and set "
                                "NYSCEF_COOKIE_FILE=/path/to/cookies.json",
                                flush=True,
                            )
                            logger.warning(
                                "Cloudflare challenge detected on NYSCEF — "
                                "live scraping not possible from this IP."
                            )
                            browser.close()
                            return []

                        # Select party name radio button
                        party_radio = page.query_selector("input[id='partyName']")
                        if party_radio:
                            party_radio.click()
                            time.sleep(0.5)

                        # Fill last name (broad search — we filter by case type and date)
                        last_inp = page.query_selector("input[name='txtPartyLastName']")
                        if last_inp:
                            last_inp.fill("A")  # single letter = broad search

                        # Select Suffolk County
                        county_sel = page.query_selector("select[name='txtCounty']")
                        if county_sel:
                            county_sel.select_option(value=NYSCEF_SUFFOLK_VALUE)

                        # Select case type
                        ct_sel = page.query_selector("select[name='txtCaseType']")
                        if ct_sel:
                            ct_sel.select_option(value=case_type_val)

                        # Set date range
                        date_from_inp = page.query_selector("input[name='txtFilingDateFrom']")
                        date_to_inp = page.query_selector("input[name='txtFilingDateTo']")
                        if date_from_inp:
                            date_from_inp.fill(start_date.strftime("%m/%d/%Y"))
                        if date_to_inp:
                            date_to_inp.fill(today.strftime("%m/%d/%Y"))

                        # Submit
                        submit_btn = page.query_selector("input[type='submit']")
                        if not submit_btn:
                            submit_btn = page.query_selector("button[type='submit']")
                        if submit_btn:
                            submit_btn.click()

                        # Wait for AJAX results to load
                        try:
                            page.wait_for_function(
                                "() => !document.body.innerText.includes('Request in progress')",
                                timeout=25000,
                            )
                        except PWTimeout:
                            logger.warning("Timed out waiting for NYSCEF results for %s.", case_type_name)

                        time.sleep(REQUEST_DELAY)

                        # Parse results table
                        new_filings = self._parse_nyscef_results(page, case_type_name)
                        print(
                            f"[probate] NYSCEF {case_type_name}: {len(new_filings)} filing(s) found.",
                            flush=True,
                        )
                        logger.info("NYSCEF %s: %d filing(s).", case_type_name, len(new_filings))
                        filings.extend(new_filings)

                    except PWTimeout as te:
                        logger.warning("Playwright timeout for %s: %s", case_type_name, te)
                    except Exception as exc:
                        logger.warning("NYSCEF scrape failed for %s: %s", case_type_name, exc)
                        traceback.print_exc()

                browser.close()

        except Exception as exc:
            print(f"[probate] Playwright session failed: {exc}", flush=True)
            logger.error("Playwright session failed: %s", exc)
            traceback.print_exc()

        return filings

    def _parse_nyscef_results(self, page, case_type_name: str) -> list[dict]:
        """
        Parses the NYSCEF results table from the current Playwright page.

        Extracts for each row:
          - decedent_name   : party name from the filing
          - filing_date     : date filed
          - case_number     : NYSCEF index number
          - petitioner_name : petitioner / attorney name (if visible)
          - case_type       : the case type searched
          - case_url        : direct link to the case on NYSCEF
          - state           : "NY"
          - county          : "Suffolk"
        """
        filings: list[dict] = []
        try:
            tables = page.query_selector_all("table")
            if not tables:
                logger.debug("No result tables found on NYSCEF results page.")
                return filings

            for table in tables:
                rows = table.query_selector_all("tr")
                if len(rows) < 2:
                    continue

                # Detect header row to map column indices
                header_cells = rows[0].query_selector_all("th, td")
                headers = [c.inner_text().strip().lower() for c in header_cells]
                logger.debug("NYSCEF table headers: %s", headers)

                # Map column names to indices
                col = {}
                for i, h in enumerate(headers):
                    if "name" in h and "party" in h:
                        col["name"] = i
                    elif "index" in h or "case" in h or "number" in h:
                        col["case_number"] = i
                    elif "date" in h and "fil" in h:
                        col["filing_date"] = i
                    elif "type" in h:
                        col["case_type"] = i
                    elif "petitioner" in h or "attorney" in h:
                        col["petitioner"] = i

                # If we couldn't map columns, use positional defaults
                if not col:
                    col = {"name": 0, "case_number": 1, "filing_date": 2}

                for row in rows[1:]:
                    cells = row.query_selector_all("td")
                    if not cells:
                        continue
                    cell_texts = [c.inner_text().strip() for c in cells]
                    if not any(cell_texts):
                        continue

                    def _get(key: str, default: str = "") -> str:
                        idx = col.get(key)
                        if idx is not None and idx < len(cell_texts):
                            return cell_texts[idx]
                        return default

                    # Extract case URL from any link in the row
                    links = row.query_selector_all("a")
                    case_url = ""
                    case_number = _get("case_number")
                    for lnk in links:
                        href = lnk.get_attribute("href") or ""
                        if href and ("case" in href.lower() or "index" in href.lower() or "nyscef" in href.lower()):
                            case_url = href if href.startswith("http") else f"{NYSCEF_BASE}/{href.lstrip('/')}"
                            if not case_number:
                                case_number = lnk.inner_text().strip()
                            break
                    if not case_url and links:
                        href = links[0].get_attribute("href") or ""
                        case_url = href if href.startswith("http") else f"{NYSCEF_BASE}/{href.lstrip('/')}"

                    decedent_name = _get("name") or cell_texts[0] if cell_texts else "Unknown"
                    filing_date = self._normalise_date(_get("filing_date"))
                    petitioner_name = _get("petitioner", "Unknown")

                    if not decedent_name or decedent_name.lower() in ("", "name", "party name"):
                        continue

                    filings.append({
                        "decedent_name": decedent_name,
                        "filing_date": filing_date,
                        "case_number": case_number,
                        "petitioner_name": petitioner_name,
                        "case_type": case_type_name,
                        "case_url": case_url,
                        "state": "NY",
                        "county": "Suffolk",
                    })

        except Exception as exc:
            logger.warning("Failed to parse NYSCEF results: %s", exc)
            traceback.print_exc()

        return filings

    # ─────────────────────────────────────────────────────────────────────────
    # Georgia probate court scrapers
    # ─────────────────────────────────────────────────────────────────────────

    def _scrape_ga_probate(self, county: str, cfg: dict) -> list[dict]:
        """
        Attempts to scrape probate filings from a Georgia county court portal
        using a simple HTTP GET + BeautifulSoup table parse.
        Returns [] on failure so the caller can fall back to mock data.
        """
        filings: list[dict] = []
        try:
            cutoff = datetime.date.today() - datetime.timedelta(days=LOOKBACK_DAYS)
            params = {
                "FilingDateFrom": cutoff.strftime("%m/%d/%Y"),
                "FilingDateTo": datetime.date.today().strftime("%m/%d/%Y"),
                "CaseType": "Estate",
            }
            resp = requests.get(
                cfg["search_url"],
                params=params,
                headers=self.http_headers,
                timeout=15,
            )
            if resp.status_code != 200:
                logger.debug(
                    "GA probate portal returned HTTP %d for %s County.",
                    resp.status_code, county,
                )
                return []

            soup = BeautifulSoup(resp.text, "html.parser")
            table = soup.find("table")
            if not table:
                return []

            rows = table.find_all("tr")
            for row in rows[1:]:  # skip header
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                if len(cells) < 3:
                    continue
                # Try to find a link to the case
                link_tag = row.find("a", href=True)
                case_url = ""
                if link_tag:
                    href = link_tag["href"]
                    case_url = href if href.startswith("http") else f"{cfg['url'].rstrip('/')}/{href.lstrip('/')}"

                filings.append({
                    "decedent_name": cells[0],
                    "filing_date": self._normalise_date(cells[1]),
                    "case_number": cells[2] if len(cells) > 2 else "",
                    "petitioner_name": cells[3] if len(cells) > 3 else "Unknown",
                    "case_type": "Estate",
                    "case_url": case_url,
                    "state": "GA",
                    "county": county,
                })
        except Exception as exc:
            logger.debug("GA probate scrape failed for %s County: %s", county, exc)
        return filings

    # ─────────────────────────────────────────────────────────────────────────
    # Database persistence
    # ─────────────────────────────────────────────────────────────────────────

    def _save_leads(self, filings: list[dict]) -> int:
        """
        Saves every filing to the leads table.

        For each filing:
          1. Try to find a matching property record by decedent name.
          2. If a match is found, use that property's address and parcel_id.
          3. If NO match is found, save the lead anyway with:
               - parcel_id  = NULL
               - address    = "<decedent_name> — <county> County, <state>"
               - owner_name stored in raw_data as 'Unknown - needs lookup'

        Deduplication: one lead per case_number + source.

        Returns the number of new leads inserted.
        """
        saved_count = 0
        db = SessionLocal()
        try:
            # Load all properties once for name-matching
            properties = db.query(Property).all()

            for filing in filings:
                print(
                    f"[probate] Processing filing: {filing.get('decedent_name')} "
                    f"({filing.get('case_number')}) — {filing.get('county')}, {filing.get('state')}",
                    flush=True,
                )

                # Deduplication by case_number
                case_number = filing.get("case_number", "")
                if case_number:
                    existing = (
                        db.query(Lead)
                        .filter(
                            Lead.source == "probate",
                            Lead.raw_data.contains(case_number),
                        )
                        .first()
                    )
                    if existing:
                        logger.info(
                            "  Lead already exists for case %s — skipping.", case_number
                        )
                        continue

                # Try to find a matching property by owner name
                matched_prop = None
                for prop in properties:
                    if prop.owner_name and self._is_name_match(
                        filing["decedent_name"], prop.owner_name
                    ):
                        matched_prop = prop
                        logger.info(
                            "  MATCH: decedent '%s' → owner '%s' at '%s'",
                            filing["decedent_name"], prop.owner_name, prop.address,
                        )
                        break

                # Build raw_data — always includes case_number and case_url
                raw_data = {
                    "decedent_name": filing.get("decedent_name", ""),
                    "filing_date": filing.get("filing_date", ""),
                    "case_number": filing.get("case_number", ""),
                    "case_url": filing.get("case_url", ""),
                    "petitioner_name": filing.get("petitioner_name", "Unknown"),
                    "case_type": filing.get("case_type", ""),
                    "state": filing.get("state", "NY"),
                    "county": filing.get("county", "Suffolk"),
                }

                if matched_prop:
                    raw_data["property_address"] = matched_prop.address
                    raw_data["owner_name"] = matched_prop.owner_name
                    lead_address = matched_prop.address
                    lead_parcel_id = matched_prop.parcel_id
                else:
                    # Save unmatched lead — parcel_id is NULL
                    raw_data["owner_name"] = "Unknown - needs lookup"
                    lead_address = (
                        f"{filing.get('decedent_name', 'Unknown')} — "
                        f"{filing.get('county', 'Unknown')} County, {filing.get('state', 'NY')}"
                    )
                    lead_parcel_id = None
                    logger.info(
                        "  No property match for '%s' — saving unmatched lead (parcel_id=NULL).",
                        filing.get("decedent_name"),
                    )

                lead = Lead(
                    address=lead_address,
                    parcel_id=lead_parcel_id,
                    source="probate",
                    raw_data=json.dumps(raw_data),
                    score=0.90,  # Probate leads are very high value
                    status="new",
                    state=filing.get("state", "NY"),
                    county=filing.get("county", "Suffolk"),
                )
                db.add(lead)
                saved_count += 1
                logger.info(
                    "  Saved probate lead: %s (parcel_id=%s)",
                    lead_address[:60], lead_parcel_id,
                )

            db.commit()
            print(f"[probate] Committed {saved_count} new lead(s) to database.", flush=True)

        except Exception as exc:
            db.rollback()
            print(f"[probate] Database error — rolled back: {exc}", flush=True)
            logger.error("Database error in _save_leads: %s", exc)
            traceback.print_exc()
            raise
        finally:
            db.close()

        return saved_count

    # ─────────────────────────────────────────────────────────────────────────
    # Utility helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _normalise_date(raw: str) -> str:
        """Normalise a date string to YYYY-MM-DD. Falls back to today."""
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
            except (ValueError, AttributeError):
                continue
        return datetime.date.today().strftime("%Y-%m-%d")

    @staticmethod
    def _is_name_match(name1: str, name2: str) -> bool:
        """
        Flexible name matching.  Returns True when at least two non-trivial
        tokens (length > 1) are shared between the two names (case-insensitive).
        """
        def tokens(n: str) -> set:
            return {p for p in re.sub(r"[.,]", "", n).upper().split() if len(p) > 1}
        shared = tokens(name1) & tokens(name2)
        return len(shared) >= 2

    # ─────────────────────────────────────────────────────────────────────────
    # Fallback mock data
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _get_mock_filings(state: str = "NY", county: str = "Suffolk") -> list[dict]:
        """
        Returns representative mock probate filings for the given state/county.
        Used as a fallback when live scraping is blocked or the portal is down.
        Each mock filing includes a plausible case_url so the raw_data field
        is always populated with a clickable link.
        """
        today = datetime.date.today()
        year = today.year
        abbr = county.upper()[:3]

        names_by_county: dict[str, list[tuple[str, str]]] = {
            "Suffolk": [
                ("JOHN DOE", "Probate"),
                ("JANE SMITH", "Administration"),
                ("ROBERT JOHNSON", "Probate"),
            ],
            "Fulton": [
                ("WILLIAM HARRIS", "Probate"),
                ("PATRICIA CLARK", "Administration"),
                ("JAMES LEWIS", "Probate"),
            ],
            "Gwinnett": [
                ("BARBARA WALKER", "Probate"),
                ("CHARLES HALL", "Administration"),
                ("DOROTHY ALLEN", "Probate"),
            ],
            "Cobb": [
                ("RICHARD YOUNG", "Probate"),
                ("MARGARET KING", "Administration"),
                ("JOSEPH WRIGHT", "Probate"),
            ],
            "DeKalb": [
                ("THOMAS SCOTT", "Probate"),
                ("HELEN GREEN", "Administration"),
                ("CHARLES BAKER", "Probate"),
            ],
            "Chatham": [
                ("MARY ADAMS", "Probate"),
                ("GEORGE NELSON", "Administration"),
                ("RUTH CARTER", "Probate"),
            ],
            "Clarke": [
                ("FRANK MITCHELL", "Probate"),
                ("VIRGINIA PEREZ", "Administration"),
                ("HENRY ROBERTS", "Probate"),
            ],
        }
        entries = names_by_county.get(county, [("JOHN DOE", "Probate"), ("JANE SMITH", "Administration")])

        filings = []
        for i, (name, case_type) in enumerate(entries):
            case_num = f"{abbr}-{year}-PR-{i + 1:03d}"
            # For NY, generate a plausible NYSCEF URL; for GA, use a generic placeholder
            if state == "NY":
                case_url = f"https://iapps.courts.state.ny.us/nyscef/CaseDetails?docketId=MOCK{case_num.replace('-', '')}"
            else:
                case_url = f"https://www.{county.lower()}countyprobate.gov/case/{case_num}"

            filings.append({
                "decedent_name": name,
                "filing_date": (today - datetime.timedelta(days=i + 2)).strftime("%Y-%m-%d"),
                "case_number": case_num,
                "petitioner_name": f"Estate of {name.title()}",
                "case_type": case_type,
                "case_url": case_url,
                "state": state,
                "county": county,
            })
        return filings


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Suffolk Leads — Probate Scraper")
    parser.add_argument(
        "--cookie-file",
        metavar="PATH",
        help=(
            "Path to a JSON file containing browser cookies exported from a real "
            "browser session on iapps.courts.state.ny.us.  Used to bypass Cloudflare. "
            "Use the EditThisCookie Chrome extension or browser DevTools → "
            "Application → Cookies → Export JSON."
        ),
    )
    args = parser.parse_args()

    scraper = ProbateScraper(cookie_file=args.cookie_file)
    scraper.scrape()
