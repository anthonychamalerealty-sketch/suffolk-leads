"""
probate.py
----------
Scrapes real probate/administration petition filings from WebSurrogates
(websurrogates.nycourts.gov) — Suffolk County Surrogate's Court.

EXACT FLOW (matches screenshots provided):
==========================================
Step 1 — Navigate to https://websurrogates.nycourts.gov/File/FileSearch
  - First pass through the Welcome page to get a valid session cookie.
  - Select Court = "Suffolk County Surrogate's Court"
  - Run two File Information Searches (last 7 days):
      • File Proceeding = ADMINISTRATION PETITION
      • File Proceeding = PROBATE PETITION

Step 2 — Results table (/File/FileSearchResults)
  - Extract from every row: File #, File Date, File Name (decedent), Proceeding, DOD
  - Click each File # link

Step 3 — File History page (/File/FileHistory)
  - Extract all parties and their roles (DECEDENT, ADMINISTRATOR, EXECUTOR)
  - Find and click the ADMINISTRATION PETITION or PROBATE PETITION document link

Step 4 — PDF reading with GPT-4o vision
  - Convert PDF pages 1 and 2 to images (200 DPI)
  - Page 1: extract petitioner name/address/phone/relationship + decedent name/address/township/DOD
  - Page 2: extract section 3(b) real property value
  - CRITICAL FILTER: if 3(b) = $0, blank, crossed out, or NONE → skip this lead

Step 5 — Save to database
  - leads:    address = decedent address, source='probate', parcel_id=NULL
  - contacts: petitioner name, phone, source='probate'
  - raw_data: file_number, case_url, pdf_url, DOD, real property value, township,
              petitioner address, petitioner relationship

CLOUDFLARE / IP BLOCK
---------------------
websurrogates.nycourts.gov blocks data-center IPs at the WAF layer.
Set WEBSURROGATES_COOKIE_FILE or WEBSURROGATES_COOKIES_JSON to inject
real browser cookies (export with EditThisCookie Chrome extension).
Without cookies, the scraper logs a clear error and saves 0 leads.
NO MOCK DATA IS USED — only real records from the website are saved.

Georgia counties:
  Fulton, Gwinnett, Cobb, DeKalb, Chatham, Clarke — scraped via HTTP.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import io
import json
import logging
import os
import re
import sys
import time
import traceback
from typing import Optional

# ── Guarded third-party imports ───────────────────────────────────────────────
try:
    import requests
    from bs4 import BeautifulSoup
except ImportError as _e:
    print(f"[probate] IMPORT ERROR (requests/bs4): {_e}", flush=True)
    raise

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    _PLAYWRIGHT_AVAILABLE = True
except ImportError as _e:
    print(f"[probate] WARNING: playwright not installed: {_e}", flush=True)
    _PLAYWRIGHT_AVAILABLE = False

try:
    from pdf2image import convert_from_bytes
    _PDF2IMAGE_AVAILABLE = True
except ImportError as _e:
    print(f"[probate] WARNING: pdf2image not installed: {_e}", flush=True)
    _PDF2IMAGE_AVAILABLE = False

try:
    from openai import OpenAI as _OpenAI
    _OPENAI_AVAILABLE = True
except ImportError as _e:
    print(f"[probate] WARNING: openai not installed: {_e}", flush=True)
    _OPENAI_AVAILABLE = False

# ── Project root on sys.path ──────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from database import SessionLocal, Property, Lead, Contact
    _DB_AVAILABLE = True
except Exception as _e:
    print(f"[probate] IMPORT ERROR (database): {_e}", flush=True)
    traceback.print_exc()
    SessionLocal = Property = Lead = Contact = None
    _DB_AVAILABLE = False

try:
    from scrapers.base_scraper import BaseScraper
except Exception:
    class BaseScraper:
        def scrape(self): pass

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [probate] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
BASE_URL      = "https://websurrogates.nycourts.gov"
WELCOME_URL   = f"{BASE_URL}/Home/Welcome/?ReturnUrl=%2FFile%2FFileSearch"
FILE_SEARCH   = f"{BASE_URL}/File/FileSearch"
PROCEEDINGS   = ["ADMINISTRATION PETITION", "PROBATE PETITION"]
REQUEST_DELAY = 3   # seconds between requests
GPT_MODEL     = "gpt-4o"

# Georgia probate court portals
GA_PROBATE_COURTS: dict[str, str] = {
    "Fulton":  "https://www.fultonprobate.org/",
    "Gwinnett": "https://www.gwinnettcourts.com/probate/",
    "Cobb":    "https://www.cobbcounty.org/courts/probate",
    "DeKalb":  "https://www.dekalbcountyga.gov/probate-court",
    "Chatham": "https://www.chathamcounty.org/Offices/Probate-Court",
    "Clarke":  "https://www.accgov.com/probate",
}

# ── GPT-4o prompts ────────────────────────────────────────────────────────────
_PAGE1_PROMPT = """
You are reading page 1 of a New York Surrogate's Court petition form (handwritten or typed).
Extract the following fields exactly as written. Return ONLY a JSON object, no other text.

{
  "petitioner_name": "...",
  "petitioner_street": "...",
  "petitioner_city_town": "...",
  "petitioner_state": "...",
  "petitioner_zip": "...",
  "petitioner_county": "...",
  "petitioner_phone": "...",
  "petitioner_relationship": "...",
  "decedent_name": "...",
  "decedent_street": "...",
  "decedent_city_town": "...",
  "decedent_state": "...",
  "decedent_zip": "...",
  "township": "...",
  "county": "...",
  "date_of_death": "...",
  "place_of_death": "..."
}

Notes:
- Section 1 = petitioner (administrator/executor). Section 2 = decedent.
- Domicile field layout: Street Address | City/Town/Village | State | Zip Code
- Telephone Number is on the right side of the state/zip line in Section 1
- Interest of Petitioner = relationship (Wife, Son, Daughter, etc.)
- Use null for any field that is blank or illegible.
""".strip()

_PAGE2_PROMPT = """
You are reading page 2 of a New York Surrogate's Court petition form.
Find section 3(b): "The estimated gross value of the decedent's real property, in this state".
Look for the dollar amount written after the $ sign on that line.

Return ONLY this JSON object:
{
  "real_property_value_raw": "...",
  "real_property_value_dollars": <number or null>,
  "has_real_property": <true or false>
}

Rules:
- If the amount is $0, blank, crossed out, "NONE", "N/A", or not filled → has_real_property=false, real_property_value_dollars=0
- If there is a real dollar amount (e.g. 50,000 or 500000) → has_real_property=true
- Strip commas and dollar signs when setting real_property_value_dollars
- Return ONLY the JSON, no other text.
""".strip()


# ─────────────────────────────────────────────────────────────────────────────
class ProbateScraper(BaseScraper):
    """
    Scrapes WebSurrogates File Search for Suffolk County probate filings,
    reads petition PDFs with GPT-4o vision, and saves leads to the database.
    No mock data — only real records are saved.
    """

    def __init__(self, cookie_file: Optional[str] = None, days: int = 7):
        self.cookie_file = (
            cookie_file
            or os.getenv("WEBSURROGATES_COOKIE_FILE", "")
        )
        self.days = days
        self._openai: Optional[_OpenAI] = None
        self._session_cookies: list[dict] = []
        self._blocked = False

    # ── Public entry point ────────────────────────────────────────────────────
    def scrape(self) -> list[dict]:
        """Run the full scrape. Returns list of filing dicts that were saved."""
        print("[probate] ============================================================", flush=True)
        print("[probate] STARTING PROBATE SCRAPER — WebSurrogates File Search", flush=True)
        print(f"[probate] Date range: last {self.days} days", flush=True)
        print(f"[probate] Playwright:  {_PLAYWRIGHT_AVAILABLE}", flush=True)
        print(f"[probate] pdf2image:   {_PDF2IMAGE_AVAILABLE}", flush=True)
        print(f"[probate] OpenAI:      {_OPENAI_AVAILABLE}", flush=True)
        print(f"[probate] Database:    {_DB_AVAILABLE}", flush=True)
        print(f"[probate] Cookie file: {self.cookie_file or '(none set)'}", flush=True)

        if not _PLAYWRIGHT_AVAILABLE:
            print("[probate] ERROR: playwright required. Run: pip install playwright && playwright install chromium", flush=True)
            return []

        # Initialise OpenAI
        if _OPENAI_AVAILABLE:
            try:
                self._openai = _OpenAI()
                print("[probate] OpenAI client ready.", flush=True)
            except Exception as exc:
                print(f"[probate] WARNING: OpenAI init failed: {exc}", flush=True)

        # Load cookies
        self._load_cookies()

        saved_filings: list[dict] = []
        total_saved = 0

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
                )
                ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                    "window.chrome={runtime:{}};"
                )
                if self._session_cookies:
                    ctx.add_cookies(self._session_cookies)
                    print(f"[probate] Injected {len(self._session_cookies)} cookies.", flush=True)

                page = ctx.new_page()

                # Step 1: Pass through Welcome page to get session
                if not self._pass_welcome(page):
                    browser.close()
                    print("[probate] Could not pass Welcome page — aborting.", flush=True)
                    return []

                # Step 2: Run File Information Search for each proceeding type
                date_from, date_to = self._date_range()
                print(f"[probate] Search date range: {date_from} → {date_to}", flush=True)

                all_filings: list[dict] = []
                for proceeding in PROCEEDINGS:
                    rows = self._search_filings(page, proceeding, date_from, date_to)
                    print(f"[probate] {proceeding}: {len(rows)} results", flush=True)
                    all_filings.extend(rows)

                print(f"[probate] Total filings to process: {len(all_filings)}", flush=True)

                # Steps 3–5: Process each filing
                for i, filing in enumerate(all_filings, 1):
                    print(f"\n[probate] [{i}/{len(all_filings)}] {filing['file_number']} — {filing['decedent_name']}", flush=True)
                    try:
                        saved = self._process_filing(page, filing)
                        if saved:
                            total_saved += 1
                            saved_filings.append(filing)
                            print(f"[probate]   → LEAD SAVED", flush=True)
                        else:
                            print(f"[probate]   → SKIPPED", flush=True)
                    except Exception as exc:
                        print(f"[probate]   → ERROR: {exc}", flush=True)
                        traceback.print_exc()
                    time.sleep(REQUEST_DELAY)

                browser.close()

        except Exception as exc:
            print(f"[probate] FATAL ERROR: {exc}", flush=True)
            traceback.print_exc()

        # Georgia counties
        print("\n[probate] === Georgia Counties ===", flush=True)
        ga_total = self._scrape_georgia()
        total_saved += ga_total

        print(f"\n[probate] DONE. Total leads saved: {total_saved}", flush=True)
        return saved_filings

    # ── Cookie loading ────────────────────────────────────────────────────────
    def _load_cookies(self):
        cookies_json = os.getenv("WEBSURROGATES_COOKIES_JSON", "")
        if not cookies_json and self.cookie_file and os.path.exists(self.cookie_file):
            try:
                with open(self.cookie_file) as f:
                    cookies_json = f.read()
                print(f"[probate] Loaded cookies from: {self.cookie_file}", flush=True)
            except Exception as exc:
                print(f"[probate] WARNING: Could not read cookie file: {exc}", flush=True)
        if cookies_json:
            try:
                raw = json.loads(cookies_json)
                normalised = []
                for c in raw:
                    entry = {
                        "name":   c.get("name", ""),
                        "value":  c.get("value", ""),
                        "domain": c.get("domain", ".websurrogates.nycourts.gov"),
                        "path":   c.get("path", "/"),
                    }
                    for k in ("httpOnly", "secure", "sameSite"):
                        if k in c:
                            entry[k] = c[k]
                    normalised.append(entry)
                self._session_cookies = normalised
                print(f"[probate] Parsed {len(normalised)} cookies.", flush=True)
            except Exception as exc:
                print(f"[probate] WARNING: Could not parse cookies: {exc}", flush=True)

    # ── Welcome page pass-through ─────────────────────────────────────────────
    def _pass_welcome(self, page) -> bool:
        """Navigate through the Welcome page to get a valid session cookie."""
        print("[probate] Loading Welcome page...", flush=True)
        try:
            page.goto(WELCOME_URL, wait_until="domcontentloaded", timeout=30000)
        except PWTimeout:
            print("[probate] ERROR: Timeout on Welcome page.", flush=True)
            return False
        time.sleep(REQUEST_DELAY)

        if self._is_blocked(page):
            self._log_block()
            return False

        print(f"[probate] Welcome page loaded: {page.title()}", flush=True)

        # Click "Start Search" to get the session cookie
        btn = page.query_selector(
            "button#StartSearchButton, input[value='Start Search'], "
            "button:has-text('Start Search'), a:has-text('Start Search')"
        )
        if btn:
            print("[probate] Clicking 'Start Search'...", flush=True)
            btn.click()
            time.sleep(REQUEST_DELAY)
            if self._is_blocked(page):
                self._log_block()
                return False
            print(f"[probate] After click: {page.title()} | {page.url}", flush=True)
        else:
            print("[probate] 'Start Search' button not found — proceeding directly.", flush=True)

        # Navigate to File Search
        try:
            page.goto(FILE_SEARCH, wait_until="domcontentloaded", timeout=30000)
        except PWTimeout:
            print("[probate] ERROR: Timeout on File Search page.", flush=True)
            return False
        time.sleep(REQUEST_DELAY)

        if self._is_blocked(page):
            self._log_block()
            return False

        print(f"[probate] File Search loaded: {page.title()}", flush=True)
        return True

    # ── File Information Search ───────────────────────────────────────────────
    def _search_filings(
        self, page, proceeding: str, date_from: str, date_to: str
    ) -> list[dict]:
        """Submit the File Information Search form and return result rows."""
        print(f"[probate] Searching: {proceeding}", flush=True)

        try:
            page.goto(FILE_SEARCH, wait_until="domcontentloaded", timeout=30000)
        except PWTimeout:
            print(f"[probate] Timeout navigating to File Search for {proceeding}", flush=True)
            return []
        time.sleep(REQUEST_DELAY)

        if self._is_blocked(page):
            self._log_block()
            return []

        # Select Suffolk County Surrogate's Court
        try:
            court_sel = page.query_selector(
                "select#Court, select[name='Court'], select[id*='ourt']"
            )
            if court_sel:
                opts = court_sel.query_selector_all("option")
                for opt in opts:
                    if "suffolk" in opt.inner_text().lower():
                        court_sel.select_option(value=opt.get_attribute("value"))
                        print(f"[probate]   Court: {opt.inner_text().strip()}", flush=True)
                        break
        except Exception as exc:
            print(f"[probate]   WARNING: court select: {exc}", flush=True)

        time.sleep(0.5)

        # Select proceeding type
        try:
            proc_sel = page.query_selector(
                "select#FileProceeding, select[name='FileProceeding'], "
                "select[id*='roceeding']"
            )
            if proc_sel:
                opts = proc_sel.query_selector_all("option")
                matched = False
                for opt in opts:
                    txt = opt.inner_text().strip().upper()
                    if txt == proceeding.upper():
                        proc_sel.select_option(value=opt.get_attribute("value"))
                        print(f"[probate]   Proceeding: {opt.inner_text().strip()}", flush=True)
                        matched = True
                        break
                if not matched:
                    # Dump available options for debugging
                    avail = [o.inner_text().strip() for o in opts]
                    print(f"[probate]   Available proceedings: {avail}", flush=True)
                    # Try partial match
                    keyword = proceeding.split()[0]
                    for opt in opts:
                        if keyword.upper() in opt.inner_text().upper():
                            proc_sel.select_option(value=opt.get_attribute("value"))
                            print(f"[probate]   Proceeding (partial): {opt.inner_text().strip()}", flush=True)
                            break
        except Exception as exc:
            print(f"[probate]   WARNING: proceeding select: {exc}", flush=True)

        # Fill date range
        try:
            from_inp = page.query_selector(
                "input#FromDate, input[name='FromDate'], input[id*='From']"
            )
            to_inp = page.query_selector(
                "input#ToDate, input[name='ToDate'], input[id*='To']"
            )
            if from_inp:
                from_inp.triple_click()
                from_inp.type(date_from)
                print(f"[probate]   From: {date_from}", flush=True)
            if to_inp:
                to_inp.triple_click()
                to_inp.type(date_to)
                print(f"[probate]   To:   {date_to}", flush=True)
        except Exception as exc:
            print(f"[probate]   WARNING: date fields: {exc}", flush=True)

        # Click the Search button (second one = File Information Search section)
        try:
            btns = page.query_selector_all(
                "input[value='Search'][type='submit'], "
                "button:has-text('Search'), input[type='submit']"
            )
            print(f"[probate]   Found {len(btns)} Search button(s)", flush=True)
            btn = btns[-1] if len(btns) >= 2 else (btns[0] if btns else None)
            if btn:
                btn.click()
                print("[probate]   Clicked Search", flush=True)
            else:
                print("[probate]   ERROR: No Search button found", flush=True)
                return []
        except Exception as exc:
            print(f"[probate]   ERROR clicking Search: {exc}", flush=True)
            return []

        time.sleep(REQUEST_DELAY)

        if self._is_blocked(page):
            self._log_block()
            return []

        print(f"[probate]   Results URL: {page.url}", flush=True)

        # Take screenshot for debugging
        try:
            sshot = f"/tmp/probate_results_{proceeding.replace(' ', '_')}.png"
            page.screenshot(path=sshot)
            print(f"[probate]   Screenshot: {sshot}", flush=True)
        except Exception:
            pass

        return self._parse_results_table(page, proceeding)

    # ── Parse results table ───────────────────────────────────────────────────
    def _parse_results_table(self, page, proceeding: str) -> list[dict]:
        """Parse the FileSearchResults table. Returns list of filing dicts."""
        filings = []
        try:
            html = page.content()
            soup = BeautifulSoup(html, "html.parser")

            # Find table with File #, File Date, File Name, Proceeding, DOD headers
            result_table = None
            for tbl in soup.find_all("table"):
                ths = [th.get_text(strip=True).upper() for th in tbl.find_all("th")]
                if any("FILE" in h for h in ths):
                    result_table = tbl
                    break

            if not result_table:
                body = soup.get_text(separator=" ", strip=True).lower()
                if "no results" in body or "no records" in body or "0 results" in body:
                    print(f"[probate]   No results for {proceeding}", flush=True)
                else:
                    print(f"[probate]   Results table not found for {proceeding}", flush=True)
                    # Save HTML for debugging
                    dbg = f"/tmp/probate_html_{proceeding.replace(' ', '_')}.html"
                    with open(dbg, "w") as f:
                        f.write(html)
                    print(f"[probate]   HTML saved: {dbg}", flush=True)
                return []

            rows = result_table.find_all("tr")[1:]  # skip header
            print(f"[probate]   {len(rows)} result rows", flush=True)

            for row in rows:
                cells = row.find_all("td")
                if len(cells) < 4:
                    continue

                file_cell = cells[0]
                file_link = file_cell.find("a")
                file_number = file_link.get_text(strip=True) if file_link else file_cell.get_text(strip=True)
                file_href   = file_link.get("href", "") if file_link else ""

                file_date  = cells[1].get_text(strip=True) if len(cells) > 1 else ""
                decedent   = cells[2].get_text(strip=True) if len(cells) > 2 else ""
                proc_type  = cells[3].get_text(strip=True) if len(cells) > 3 else proceeding
                dod        = cells[4].get_text(strip=True) if len(cells) > 4 else ""

                if not file_number:
                    continue

                # Build absolute history URL
                if file_href.startswith("http"):
                    history_url = file_href
                elif file_href.startswith("/"):
                    history_url = BASE_URL + file_href
                else:
                    history_url = f"{BASE_URL}/File/FileHistory?fileNumber={file_number}"

                filings.append({
                    "file_number":   file_number,
                    "file_date":     file_date,
                    "decedent_name": decedent,
                    "proceeding":    proc_type,
                    "dod":           dod,
                    "history_url":   history_url,
                })
                print(f"[probate]     {file_number} | {file_date} | {decedent} | DOD:{dod}", flush=True)

        except Exception as exc:
            print(f"[probate]   ERROR parsing results: {exc}", flush=True)
            traceback.print_exc()

        return filings

    # ── Process one filing ────────────────────────────────────────────────────
    def _process_filing(self, page, filing: dict) -> bool:
        """Navigate to File History, download petition PDF, extract data, save lead."""
        file_number = filing["file_number"]

        # Navigate to File History
        print(f"[probate]   Loading File History: {filing['history_url']}", flush=True)
        try:
            page.goto(filing["history_url"], wait_until="domcontentloaded", timeout=30000)
        except PWTimeout:
            print(f"[probate]   Timeout on File History for {file_number}", flush=True)
            return False
        time.sleep(REQUEST_DELAY)

        if self._is_blocked(page):
            self._log_block()
            return False

        html = page.content()
        soup = BeautifulSoup(html, "html.parser")

        # Extract parties
        parties = self._extract_parties(soup)
        print(f"[probate]   Parties: {[(p['name'], p['role']) for p in parties]}", flush=True)

        # Find petition PDF link
        pdf_url = self._find_petition_link(soup, filing["proceeding"])
        if not pdf_url:
            print(f"[probate]   No petition PDF link found — saving without PDF data", flush=True)
            return self._save_no_pdf(filing, parties)

        print(f"[probate]   PDF link: {pdf_url}", flush=True)

        # Download PDF
        pdf_bytes = self._download_pdf(page, pdf_url)
        if not pdf_bytes:
            print(f"[probate]   PDF download failed — saving without PDF data", flush=True)
            return self._save_no_pdf(filing, parties)

        print(f"[probate]   PDF size: {len(pdf_bytes):,} bytes", flush=True)

        # Extract data with GPT-4o
        pdf_data = self._extract_pdf(pdf_bytes, file_number)
        if not pdf_data:
            print(f"[probate]   PDF extraction failed — saving without PDF data", flush=True)
            return self._save_no_pdf(filing, parties)

        # CRITICAL FILTER: skip ONLY when GPT-4o explicitly confirmed $0/NONE AND value=0
        # If has_real_property is missing or ambiguous, save anyway — never lose a real case
        prop_dollars = pdf_data.get("real_property_value_dollars", None)
        has_prop = pdf_data.get("has_real_property", None)  # None = GPT-4o didn't say
        raw_val = pdf_data.get("real_property_value_raw", "")
        print(f"[probate]   Section 3(b): has_real_property={has_prop} | raw='{raw_val}' | dollars={prop_dollars}", flush=True)

        if has_prop is False and (prop_dollars == 0 or prop_dollars is None):
            # GPT-4o explicitly said no real property and gave $0 or blank
            print(f"[probate]   SKIPPED (confirmed no real property): section 3(b) = '{raw_val}'", flush=True)
            return False

        if has_prop is False and prop_dollars:
            # GPT-4o said false but gave a dollar value — trust the value, save it
            print(f"[probate]   WARNING: has_real_property=false but value=${prop_dollars} — saving anyway", flush=True)

        return self._save_lead(filing, parties, pdf_data, pdf_url)

    # ── Extract parties ───────────────────────────────────────────────────────
    def _extract_parties(self, soup: BeautifulSoup) -> list[dict]:
        parties = []
        try:
            for tbl in soup.find_all("table"):
                ths = [th.get_text(strip=True).upper() for th in tbl.find_all("th")]
                if "PARTY" in ths and "ROLE" in ths:
                    for row in tbl.find_all("tr")[1:]:
                        cells = row.find_all("td")
                        if len(cells) >= 2:
                            parties.append({
                                "name": cells[0].get_text(strip=True),
                                "role": cells[1].get_text(strip=True),
                                "dod":  cells[2].get_text(strip=True) if len(cells) > 2 else "",
                            })
                    break
        except Exception as exc:
            print(f"[probate]   WARNING: party extraction: {exc}", flush=True)
        return parties

    # ── Find petition PDF link ────────────────────────────────────────────────
    def _find_petition_link(self, soup: BeautifulSoup, proceeding: str) -> Optional[str]:
        """Find the ADMINISTRATION PETITION or PROBATE PETITION document link."""
        keywords = ["ADMINISTRATION PETITION", "PROBATE PETITION"]
        try:
            for link in soup.find_all("a"):
                txt = link.get_text(strip=True).upper()
                for kw in keywords:
                    if kw in txt:
                        href = link.get("href", "")
                        if href:
                            if href.startswith("http"):
                                return href
                            return BASE_URL + (href if href.startswith("/") else "/" + href)
        except Exception as exc:
            print(f"[probate]   WARNING: PDF link search: {exc}", flush=True)
        return None

    # ── Download PDF ──────────────────────────────────────────────────────────
    def _download_pdf(self, page, pdf_url: str) -> Optional[bytes]:
        time.sleep(REQUEST_DELAY)
        try:
            resp = page.context.request.get(pdf_url, timeout=30000)
            if resp.status == 200:
                body = resp.body()
                if body[:4] == b"%PDF":
                    return body
                print(f"[probate]   WARNING: Response is not a PDF (first 4 bytes: {body[:4]})", flush=True)
            else:
                print(f"[probate]   PDF HTTP {resp.status}", flush=True)
        except Exception as exc:
            print(f"[probate]   Playwright PDF download error: {exc}", flush=True)

        # Fallback: requests with session cookies
        try:
            cookies = {c["name"]: c["value"] for c in page.context.cookies()}
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Referer": BASE_URL,
            }
            r = requests.get(pdf_url, cookies=cookies, headers=headers, timeout=30)
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                return r.content
            print(f"[probate]   requests PDF HTTP {r.status_code}", flush=True)
        except Exception as exc:
            print(f"[probate]   requests PDF download error: {exc}", flush=True)
        return None

    # ── GPT-4o PDF extraction ─────────────────────────────────────────────────
    def _extract_pdf(self, pdf_bytes: bytes, file_number: str) -> Optional[dict]:
        if not _PDF2IMAGE_AVAILABLE:
            print(f"[probate]   pdf2image not available", flush=True)
            return None
        if not self._openai:
            print(f"[probate]   OpenAI not available", flush=True)
            return None
        try:
            images = convert_from_bytes(pdf_bytes, dpi=200, first_page=1, last_page=2)
            print(f"[probate]   PDF converted: {len(images)} page(s)", flush=True)
            if not images:
                return None

            result: dict = {}

            # Page 1
            p1 = self._gpt4o_page(images[0], _PAGE1_PROMPT, f"{file_number} p1")
            if p1:
                result.update(p1)

            # Page 2
            if len(images) >= 2:
                p2 = self._gpt4o_page(images[1], _PAGE2_PROMPT, f"{file_number} p2")
                if p2:
                    result.update(p2)
            else:
                # Single-page PDF — cannot check 3(b), assume real property present
                result["has_real_property"] = True
                result["real_property_value_raw"] = "unknown (single page)"
                result["real_property_value_dollars"] = None

            return result or None

        except Exception as exc:
            print(f"[probate]   PDF extraction error: {exc}", flush=True)
            traceback.print_exc()
            return None

    def _gpt4o_page(self, image, prompt: str, label: str) -> Optional[dict]:
        try:
            buf = io.BytesIO()
            image.save(buf, format="JPEG", quality=90)
            b64 = base64.b64encode(buf.getvalue()).decode()
            print(f"[probate]   GPT-4o: {label} ({len(b64)//1024}KB)...", flush=True)

            resp = self._openai.chat.completions.create(
                model=GPT_MODEL,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/jpeg;base64,{b64}",
                            "detail": "high",
                        }},
                    ],
                }],
                max_tokens=1000,
                temperature=0,
            )
            raw = resp.choices[0].message.content.strip()
            print(f"[probate]   GPT-4o {label}: {raw[:150]}", flush=True)

            # Strip markdown fences
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw).strip()
            return json.loads(raw)

        except json.JSONDecodeError as exc:
            print(f"[probate]   GPT-4o non-JSON for {label}: {exc}", flush=True)
            return None
        except Exception as exc:
            print(f"[probate]   GPT-4o error for {label}: {exc}", flush=True)
            return None

    # ── Save lead with full PDF data ──────────────────────────────────────────
    def _save_lead(self, filing: dict, parties: list, pdf_data: dict, pdf_url: str) -> bool:
        if not _DB_AVAILABLE:
            print("[probate]   DB not available — printing lead summary:", flush=True)
            print(f"[probate]   Decedent: {pdf_data.get('decedent_name', filing['decedent_name'])}", flush=True)
            print(f"[probate]   Address:  {self._build_addr(pdf_data, 'decedent')}", flush=True)
            print(f"[probate]   Petitioner: {pdf_data.get('petitioner_name')} | {pdf_data.get('petitioner_phone')}", flush=True)
            return False

        try:
            db = SessionLocal()
            try:
                # Dedup by file_number — use .like() for SQLite compatibility
                _fn = filing["file_number"]
                existing = db.query(Lead).filter(
                    Lead.source == "probate",
                    Lead.raw_data.like(f"%{_fn}%")
                ).first()
                if existing:
                    print(f"[probate]   DUPLICATE: {_fn} (lead id={existing.id})", flush=True)
                    return False

                decedent_addr = self._build_addr(pdf_data, "decedent")
                petitioner_addr = self._build_addr(pdf_data, "petitioner")

                if not decedent_addr:
                    decedent_addr = f"{filing['decedent_name']} — Suffolk County, NY"

                raw_data = {
                    "file_number":              filing["file_number"],
                    "file_date":                filing["file_date"],
                    "proceeding":               filing["proceeding"],
                    "case_url":                 filing["history_url"],
                    "pdf_url":                  pdf_url,
                    "decedent_name":            pdf_data.get("decedent_name") or filing["decedent_name"],
                    "decedent_address":         decedent_addr,
                    "date_of_death":            pdf_data.get("date_of_death") or filing.get("dod", ""),
                    "township":                 pdf_data.get("township"),
                    "county":                   pdf_data.get("county", "Suffolk"),
                    "place_of_death":           pdf_data.get("place_of_death"),
                    "petitioner_name":          pdf_data.get("petitioner_name"),
                    "petitioner_address":       petitioner_addr,
                    "petitioner_phone":         pdf_data.get("petitioner_phone"),
                    "petitioner_relationship":  pdf_data.get("petitioner_relationship"),
                    "real_property_value":      pdf_data.get("real_property_value_dollars"),
                    "real_property_value_raw":  pdf_data.get("real_property_value_raw"),
                    "parties":                  parties,
                }

                # Try to match existing property record
                matched = None
                if Property and decedent_addr:
                    street = decedent_addr.split(",")[0].strip()[:30]
                    try:
                        matched = db.query(Property).filter(
                            Property.address.ilike(f"%{street}%")
                        ).first()
                    except Exception:
                        pass

                lead = Lead(
                    address=decedent_addr,
                    parcel_id=matched.parcel_id if matched else None,
                    source="probate",
                    raw_data=json.dumps(raw_data),
                    score=self._score(pdf_data),
                    status="new",
                    state="NY",
                    county=pdf_data.get("county", "Suffolk"),
                )
                db.add(lead)
                db.flush()

                # Save petitioner as contact — source='petition_document' marks
                # this as the highest-quality phone (petitioner wrote it themselves)
                petitioner_name = pdf_data.get("petitioner_name")
                petitioner_phone = pdf_data.get("petitioner_phone")
                if petitioner_name:
                    contact = Contact(
                        lead_id=lead.id,
                        owner_name=petitioner_name,
                        phone=petitioner_phone,
                        email=None,
                        source="petition_document",
                    )
                    db.add(contact)
                    print(
                        f"[probate]   CONTACT (petition_document): {petitioner_name} "
                        f"| phone={petitioner_phone or 'none'} "
                        f"| rel={pdf_data.get('petitioner_relationship', '')}",
                        flush=True,
                    )

                db.commit()
                print(f"[probate]   SAVED: lead id={lead.id} | {decedent_addr}", flush=True)
                if petitioner_name:
                    print(f"[probate]   CONTACT: {petitioner_name} ({pdf_data.get('petitioner_relationship')}) {pdf_data.get('petitioner_phone', '')}", flush=True)
                return True

            except Exception as exc:
                db.rollback()
                print(f"[probate]   DB save error: {exc}", flush=True)
                traceback.print_exc()
                return False
            finally:
                db.close()

        except Exception as exc:
            print(f"[probate]   DB session error: {exc}", flush=True)
            return False

    # ── Save lead without PDF data ────────────────────────────────────────────
    def _save_no_pdf(self, filing: dict, parties: list) -> bool:
        """Save a lead using only results-table data when PDF is unavailable."""
        if not _DB_AVAILABLE:
            return False
        try:
            db = SessionLocal()
            try:
                # Dedup by file_number — use .like() for SQLite compatibility
                _fn = filing["file_number"]
                existing = db.query(Lead).filter(
                    Lead.source == "probate",
                    Lead.raw_data.like(f"%{_fn}%")
                ).first()
                if existing:
                    print(f"[probate]   DUPLICATE: {_fn}", flush=True)
                    return False

                decedent = next(
                    (p for p in parties if "DECEDENT" in p.get("role", "").upper()), None
                )
                name = decedent["name"] if decedent else filing["decedent_name"]
                address = f"{name} — Suffolk County, NY (address pending PDF read)"

                raw_data = {
                    "file_number":   filing["file_number"],
                    "file_date":     filing["file_date"],
                    "proceeding":    filing["proceeding"],
                    "case_url":      filing.get("history_url") or filing.get("case_url", ""),
                    "decedent_name": name,
                    "date_of_death": filing.get("dod", ""),
                    "parties":       parties,
                    "note":          "PDF not available or could not be read",
                }

                lead = Lead(
                    address=address,
                    parcel_id=None,
                    source="probate",
                    raw_data=json.dumps(raw_data),
                    score=50.0,
                    status="new",
                    state="NY",
                    county="Suffolk",
                )
                db.add(lead)
                db.commit()
                print(f"[probate]   SAVED (no PDF): lead id={lead.id}", flush=True)
                return True

            except Exception as exc:
                db.rollback()
                print(f"[probate]   DB save error (no PDF): {exc}", flush=True)
                return False
            finally:
                db.close()

        except Exception as exc:
            print(f"[probate]   DB session error: {exc}", flush=True)
            return False

    # ── Georgia scraper ───────────────────────────────────────────────────────
    def _scrape_georgia(self) -> int:
        """Attempt to scrape Georgia county probate courts via HTTP."""
        total = 0
        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        })
        for county, url in GA_PROBATE_COURTS.items():
            print(f"[probate] Georgia/{county}: {url}", flush=True)
            try:
                resp = session.get(url, timeout=15)
                if resp.status_code != 200:
                    print(f"[probate]   HTTP {resp.status_code}", flush=True)
                    time.sleep(REQUEST_DELAY)
                    continue
                soup = BeautifulSoup(resp.text, "html.parser")
                # Log any filing/estate/petition links found for manual review
                links = [
                    a for a in soup.find_all("a", href=True)
                    if any(kw in a.get_text(strip=True).lower()
                           for kw in ["estate", "probate", "filing", "petition", "recent"])
                ]
                if links:
                    print(f"[probate]   Found {len(links)} potential filing links:", flush=True)
                    for lnk in links[:5]:
                        print(f"[probate]     {lnk.get_text(strip=True)[:60]}: {lnk['href']}", flush=True)
                else:
                    print(f"[probate]   No filing links found (portal may require login)", flush=True)
            except Exception as exc:
                print(f"[probate]   ERROR Georgia/{county}: {exc}", flush=True)
            time.sleep(REQUEST_DELAY)
        return total

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _is_blocked(self, page) -> bool:
        title = page.title().lower()
        if "request could not be processed" in title or "just a moment" in title:
            return True
        try:
            body = page.inner_text("body")[:500].lower()
            if "request could not be processed" in body or "disabling your vpn" in body:
                return True
        except Exception:
            pass
        return False

    def _log_block(self):
        if self._blocked:
            return
        self._blocked = True
        print("", flush=True)
        print("[probate] *** IP BLOCK DETECTED ***", flush=True)
        print("[probate] websurrogates.nycourts.gov blocks data-center IPs.", flush=True)
        print("[probate] Fix: export browser cookies and set:", flush=True)
        print("[probate]   WEBSURROGATES_COOKIE_FILE=/path/to/cookies.json", flush=True)
        print("[probate]   or WEBSURROGATES_COOKIES_JSON='[{...}]'", flush=True)
        print("[probate] Use EditThisCookie Chrome extension to export.", flush=True)
        print("[probate] Cookies expire every ~24 hours.", flush=True)
        print("[probate] Saving 0 leads this run.", flush=True)
        print("", flush=True)

    def _date_range(self) -> tuple[str, str]:
        today = datetime.date.today()
        start = today - datetime.timedelta(days=self.days)
        return start.strftime("%m/%d/%Y"), today.strftime("%m/%d/%Y")

    def _build_addr(self, data: dict, prefix: str) -> str:
        parts = [
            data.get(f"{prefix}_street", ""),
            data.get(f"{prefix}_city_town", ""),
            data.get(f"{prefix}_state", ""),
            data.get(f"{prefix}_zip", ""),
        ]
        return ", ".join(p.strip() for p in parts if p and p.strip())

    def _score(self, pdf_data: dict) -> float:
        score = 60.0
        val = pdf_data.get("real_property_value_dollars")
        if val:
            if val >= 500000:   score += 30
            elif val >= 200000: score += 20
            elif val >= 100000: score += 10
        if pdf_data.get("petitioner_phone"):  score += 5
        if pdf_data.get("decedent_street"):   score += 5
        return min(score, 100.0)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Probate court scraper — WebSurrogates")
    parser.add_argument("--cookie-file", default=os.getenv("WEBSURROGATES_COOKIE_FILE", ""),
                        help="Path to JSON cookie file from a real browser session")
    parser.add_argument("--days", type=int, default=7,
                        help="Days back to search (default: 7)")
    parser.add_argument("--georgia-only", action="store_true")
    parser.add_argument("--ny-only", action="store_true")
    args = parser.parse_args()

    scraper = ProbateScraper(cookie_file=args.cookie_file, days=args.days)

    if args.georgia_only:
        scraper._scrape_georgia()
    elif args.ny_only:
        # Run only NY portion
        scraper.scrape()
    else:
        scraper.scrape()


if __name__ == "__main__":
    main()
