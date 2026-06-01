"""
probate_georgia.py
------------------
Scrapes real Georgia probate court filings from re:SearchGA
(researchga.tylerhost.net) using Playwright + GPT-4o vision.

EXACT FLOW
==========
Step 1 — Login
  https://researchga.tylerhost.net/CourtRecordsSearch/ui/Home
  Click "Sign in with eFileGA Account"
  Enter RESEARCHGA_EMAIL / RESEARCHGA_PASSWORD

Step 2 — Advanced Search
  https://researchga.tylerhost.net/CourtRecordsSearch/ui/advancedSearch
  Search for: Cases
  Search by: Case Location
  Click "Select" → search for "Probate" → check all 7 target courts → Done
  Click Search

Step 3 — Results
  Extract from every case card: case name, case number, location/court,
  case type, parties, attorney, judge, filed date
  Filter to cases filed in the last 90 days
  Click each case for detail URL

Step 4 — Case detail
  Find "Petition for Letters of Administration" or "Petition for Probate"
  document in the Events/Documents section
  Click the document link → Document Preview popup (FREE, no purchase needed)
  Take a screenshot of the popup
  Send screenshot to GPT-4o vision

Step 5 — Save
  leads:    decedent address, source='probate', state='GA', county=court county
  contacts: petitioner + all heirs (name, address, relationship)
  raw_data: case_number, court, case_url, DOD

Env vars required:
  RESEARCHGA_EMAIL     — re:SearchGA account email
  RESEARCHGA_PASSWORD  — re:SearchGA account password
  OPENAI_API_KEY       — for GPT-4o vision

Rate limit: 3 seconds between requests.
NO mock data — only real records from the website are saved.
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
    print(f"[probate_ga] IMPORT ERROR (requests/bs4): {_e}", flush=True)
    raise

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    _PLAYWRIGHT_AVAILABLE = True
except ImportError as _e:
    print(f"[probate_ga] WARNING: playwright not installed: {_e}", flush=True)
    _PLAYWRIGHT_AVAILABLE = False

try:
    from openai import OpenAI as _OpenAI
    _OPENAI_AVAILABLE = True
except ImportError as _e:
    print(f"[probate_ga] WARNING: openai not installed: {_e}", flush=True)
    _OPENAI_AVAILABLE = False

# ── Project root on sys.path ──────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from database import SessionLocal, Lead, Contact
    _DB_AVAILABLE = True
except Exception as _e:
    print(f"[probate_ga] IMPORT ERROR (database): {_e}", flush=True)
    traceback.print_exc()
    SessionLocal = Lead = Contact = None
    _DB_AVAILABLE = False

try:
    from scrapers.base_scraper import BaseScraper
except Exception:
    class BaseScraper:
        def scrape(self): pass

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [probate_ga] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
BASE_URL         = "https://researchga.tylerhost.net"
HOME_URL         = f"{BASE_URL}/CourtRecordsSearch/ui/Home"
ADV_SEARCH_URL   = f"{BASE_URL}/CourtRecordsSearch/ui/advancedSearch"
REQUEST_DELAY    = 3   # seconds between requests
GPT_MODEL        = "gpt-4o"

# The 7 target courts (as they appear in the location search)
TARGET_COURTS: list[str] = [
    "FULTON PROBATE COURT",
    "GWINNETT COUNTY - PROBATE COURT",
    "GLYNN - PROBATE COURT",
    "FORSYTH COUNTY - PROBATE COURT A",
    "CHATHAM COUNTY - PROBATE COURT",
    "FORSYTH COUNTY - PROBATE COURT",
    "ROCKDALE PROBATE COURT",
]

# Map court name → county for DB tagging
COURT_TO_COUNTY: dict[str, str] = {
    "FULTON PROBATE COURT":              "Fulton",
    "GWINNETT COUNTY - PROBATE COURT":   "Gwinnett",
    "GLYNN - PROBATE COURT":             "Glynn",
    "FORSYTH COUNTY - PROBATE COURT A":  "Forsyth",
    "CHATHAM COUNTY - PROBATE COURT":    "Chatham",
    "FORSYTH COUNTY - PROBATE COURT":    "Forsyth",
    "ROCKDALE PROBATE COURT":            "Rockdale",
}

# Document names to look for (case-insensitive partial match)
PETITION_DOC_KEYWORDS: list[str] = [
    "petition for letters of administration",
    "petition for probate",
    "petition to probate",
    "petition for letters testamentary",
    "petition",
]

# ── GPT-4o prompt ─────────────────────────────────────────────────────────────
_GPT_PROMPT = """
This is a Georgia probate court petition document (may be handwritten or typed).
Extract these exact fields and return ONLY valid JSON — no markdown, no other text.

{
  "decedent_name": "",
  "decedent_address": "",
  "date_of_death": "",
  "petitioner_name": "",
  "petitioner_address": "",
  "petitioner_phone": "",
  "petitioner_relationship": "",
  "died_intestate": true,
  "heirs": [
    {"name": "", "address": "", "relationship": ""}
  ]
}

Rules:
- Use null for any field that is blank or illegible.
- petitioner_phone: look for any telephone/phone number field near the petitioner section.
- died_intestate: true if the decedent died without a will, false if a will exists.
- heirs: include all named heirs/distributees with their addresses and relationships.
- Return ONLY the JSON object, nothing else.
""".strip()


# ─────────────────────────────────────────────────────────────────────────────
class GeorgiaProbateScraper(BaseScraper):
    """
    Logs into re:SearchGA, searches 7 Georgia probate courts for recent
    filings, reads petition documents via GPT-4o vision, and saves leads
    and contacts to the database.  No mock data.
    """

    def __init__(self, days: int = 90):
        self.days = days
        self.email    = os.getenv("RESEARCHGA_EMAIL", "")
        self.password = os.getenv("RESEARCHGA_PASSWORD", "")
        self._openai: Optional[_OpenAI] = None
        self._cutoff  = datetime.date.today() - datetime.timedelta(days=days)

    # ── Public entry point ────────────────────────────────────────────────────
    def scrape(self) -> list[dict]:
        print("[probate_ga] ============================================================", flush=True)
        print("[probate_ga] STARTING GEORGIA PROBATE SCRAPER — re:SearchGA", flush=True)
        print(f"[probate_ga] Date cutoff: {self._cutoff} (last {self.days} days)", flush=True)
        print(f"[probate_ga] Playwright:  {_PLAYWRIGHT_AVAILABLE}", flush=True)
        print(f"[probate_ga] OpenAI:      {_OPENAI_AVAILABLE}", flush=True)
        print(f"[probate_ga] Database:    {_DB_AVAILABLE}", flush=True)
        print(f"[probate_ga] Email set:   {bool(self.email)}", flush=True)

        if not _PLAYWRIGHT_AVAILABLE:
            print("[probate_ga] ERROR: playwright required.", flush=True)
            return []

        if not self.email or not self.password:
            print("[probate_ga] ERROR: RESEARCHGA_EMAIL and RESEARCHGA_PASSWORD must be set.", flush=True)
            return []

        if _OPENAI_AVAILABLE:
            try:
                self._openai = _OpenAI()
                print("[probate_ga] OpenAI client ready.", flush=True)
            except Exception as exc:
                print(f"[probate_ga] WARNING: OpenAI init failed: {exc}", flush=True)

        saved: list[dict] = []
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
                    viewport={"width": 1440, "height": 900},
                    locale="en-US",
                    timezone_id="America/New_York",
                )
                ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                    "window.chrome={runtime:{}};"
                )
                page = ctx.new_page()

                # Step 1: Login
                if not self._login(page):
                    browser.close()
                    return []

                # Step 2: Advanced Search
                cases = self._advanced_search(page)
                print(f"[probate_ga] Total cases found: {len(cases)}", flush=True)

                # Steps 3–5: Process each case
                for i, case in enumerate(cases, 1):
                    print(f"\n[probate_ga] [{i}/{len(cases)}] {case.get('case_number', '?')} — {case.get('case_name', '?')}", flush=True)
                    try:
                        n = self._process_case(page, case)
                        total_saved += n
                        if n > 0:
                            saved.append(case)
                    except Exception as exc:
                        print(f"[probate_ga]   ERROR: {exc}", flush=True)
                        traceback.print_exc()
                    time.sleep(REQUEST_DELAY)

                browser.close()

        except Exception as exc:
            print(f"[probate_ga] FATAL ERROR: {exc}", flush=True)
            traceback.print_exc()

        print(f"\n[probate_ga] DONE. Total leads saved: {total_saved}", flush=True)
        return saved

    # ── Login ─────────────────────────────────────────────────────────────────
    def _login(self, page) -> bool:
        print("[probate_ga] Navigating to Home page...", flush=True)
        try:
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
        except PWTimeout:
            print("[probate_ga] ERROR: Timeout on Home page.", flush=True)
            return False
        time.sleep(REQUEST_DELAY)

        print(f"[probate_ga] Home page: {page.title()}", flush=True)
        self._screenshot(page, "/tmp/ga_01_home.png")

        # Click "Sign in with eFileGA Account"
        signin_btn = page.query_selector(
            "button:has-text('Sign in with eFileGA'), "
            "a:has-text('Sign in with eFileGA'), "
            "button:has-text('Sign In'), "
            "[data-testid='signin-btn'], "
            "button:has-text('Login')"
        )
        if not signin_btn:
            # Try any button containing "sign" or "login"
            for btn in page.query_selector_all("button, a"):
                txt = btn.inner_text().lower()
                if "sign in" in txt or "login" in txt or "efile" in txt:
                    signin_btn = btn
                    break

        if signin_btn:
            print(f"[probate_ga] Clicking sign-in button: '{signin_btn.inner_text().strip()[:50]}'", flush=True)
            signin_btn.click()
            time.sleep(REQUEST_DELAY)
        else:
            print("[probate_ga] Sign-in button not found — trying direct navigation to login.", flush=True)
            # Try common Tyler Technologies login URLs
            for login_url in [
                f"{BASE_URL}/CourtRecordsSearch/ui/login",
                "https://login.efiling.tylerhost.net/",
                "https://efile.tylerhost.net/",
            ]:
                try:
                    page.goto(login_url, wait_until="domcontentloaded", timeout=15000)
                    time.sleep(2)
                    if page.query_selector("input[type='email'], input[name='email'], input[id*='email']"):
                        break
                except Exception:
                    continue

        self._screenshot(page, "/tmp/ga_02_login_page.png")
        print(f"[probate_ga] Login page: {page.title()} | {page.url}", flush=True)

        # Fill email
        email_inp = page.query_selector(
            "input[type='email'], input[name='email'], "
            "input[id*='email'], input[placeholder*='email' i], "
            "input[name='username'], input[id*='username']"
        )
        if email_inp:
            email_inp.click()
            email_inp.fill(self.email)
            print(f"[probate_ga] Filled email: {self.email[:20]}…", flush=True)
        else:
            print("[probate_ga] ERROR: Email input not found.", flush=True)
            self._screenshot(page, "/tmp/ga_02b_no_email_input.png")
            return False

        # Fill password
        pass_inp = page.query_selector(
            "input[type='password'], input[name='password'], "
            "input[id*='password']"
        )
        if pass_inp:
            pass_inp.fill(self.password)
            print("[probate_ga] Filled password.", flush=True)
        else:
            print("[probate_ga] ERROR: Password input not found.", flush=True)
            return False

        # Submit
        submit_btn = page.query_selector(
            "button[type='submit'], input[type='submit'], "
            "button:has-text('Sign In'), button:has-text('Login'), "
            "button:has-text('Continue')"
        )
        if submit_btn:
            submit_btn.click()
            print("[probate_ga] Submitted login form.", flush=True)
        else:
            page.keyboard.press("Enter")
            print("[probate_ga] Pressed Enter to submit.", flush=True)

        time.sleep(REQUEST_DELAY + 2)
        self._screenshot(page, "/tmp/ga_03_after_login.png")
        print(f"[probate_ga] After login: {page.title()} | {page.url}", flush=True)

        # Check for login failure
        body_text = ""
        try:
            body_text = page.inner_text("body")[:500].lower()
        except Exception:
            pass
        if "invalid" in body_text or "incorrect" in body_text or "failed" in body_text:
            print("[probate_ga] ERROR: Login failed — invalid credentials.", flush=True)
            return False

        # Navigate to Advanced Search to confirm login worked
        try:
            page.goto(ADV_SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(REQUEST_DELAY)
        except PWTimeout:
            print("[probate_ga] ERROR: Timeout on Advanced Search page.", flush=True)
            return False

        self._screenshot(page, "/tmp/ga_04_adv_search.png")
        print(f"[probate_ga] Advanced Search: {page.title()} | {page.url}", flush=True)

        # If redirected back to login, credentials were wrong
        if "login" in page.url.lower() or "signin" in page.url.lower():
            print("[probate_ga] ERROR: Redirected to login — credentials rejected.", flush=True)
            return False

        print("[probate_ga] Login successful.", flush=True)
        return True

    # ── Advanced Search ───────────────────────────────────────────────────────
    def _advanced_search(self, page) -> list[dict]:
        """Configure and run the Advanced Search, return list of case dicts."""
        print("[probate_ga] Configuring Advanced Search...", flush=True)

        # Ensure we're on the Advanced Search page
        if "advancedSearch" not in page.url:
            try:
                page.goto(ADV_SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
                time.sleep(REQUEST_DELAY)
            except PWTimeout:
                print("[probate_ga] ERROR: Timeout navigating to Advanced Search.", flush=True)
                return []

        self._screenshot(page, "/tmp/ga_04_adv_search.png")

        # Set "Search for" = Cases
        self._select_option_by_text(page, "Cases",
            ["select[id*='searchFor'], select[name*='searchFor']",
             "select[id*='SearchFor'], select[name*='SearchFor']"])

        time.sleep(1)

        # Set "Search by" = Case Location
        self._select_option_by_text(page, "Case Location",
            ["select[id*='searchBy'], select[name*='searchBy']",
             "select[id*='SearchBy'], select[name*='SearchBy']"])

        time.sleep(1)

        # Click "Select" button to open location picker
        select_btn = self._find_button(page, ["Select", "Choose Location", "Add Location"])
        if select_btn:
            select_btn.click()
            print("[probate_ga] Clicked 'Select' for location.", flush=True)
            time.sleep(REQUEST_DELAY)
        else:
            print("[probate_ga] WARNING: 'Select' button not found — trying to proceed.", flush=True)

        self._screenshot(page, "/tmp/ga_05_location_picker.png")

        # Search for "Probate" in the location search box
        loc_search = page.query_selector(
            "input[placeholder*='search' i], input[placeholder*='filter' i], "
            "input[placeholder*='location' i], input[aria-label*='search' i], "
            "dialog input[type='text'], [role='dialog'] input[type='text']"
        )
        if loc_search:
            loc_search.fill("Probate")
            print("[probate_ga] Typed 'Probate' in location search.", flush=True)
            time.sleep(2)
        else:
            print("[probate_ga] WARNING: Location search box not found.", flush=True)

        self._screenshot(page, "/tmp/ga_06_probate_results.png")

        # Check all 7 target courts
        checked = 0
        for court_name in TARGET_COURTS:
            checked += self._check_court(page, court_name)

        print(f"[probate_ga] Checked {checked}/{len(TARGET_COURTS)} courts.", flush=True)
        self._screenshot(page, "/tmp/ga_07_courts_checked.png")

        # Click "Done" or "Apply"
        done_btn = self._find_button(page, ["Done", "Apply", "OK", "Confirm", "Close"])
        if done_btn:
            done_btn.click()
            print("[probate_ga] Clicked 'Done'.", flush=True)
            time.sleep(REQUEST_DELAY)
        else:
            print("[probate_ga] WARNING: 'Done' button not found.", flush=True)
            # Try pressing Escape to close dialog
            page.keyboard.press("Escape")
            time.sleep(1)

        self._screenshot(page, "/tmp/ga_08_after_done.png")

        # Click "Search"
        search_btn = self._find_button(page, ["Search", "Find Cases", "Run Search"])
        if search_btn:
            search_btn.click()
            print("[probate_ga] Clicked 'Search'.", flush=True)
        else:
            # Try submit
            submit = page.query_selector("button[type='submit'], input[type='submit']")
            if submit:
                submit.click()
                print("[probate_ga] Clicked submit.", flush=True)
            else:
                print("[probate_ga] ERROR: Search button not found.", flush=True)
                return []

        time.sleep(REQUEST_DELAY + 2)
        self._screenshot(page, "/tmp/ga_09_results.png")
        print(f"[probate_ga] Results page: {page.title()} | {page.url}", flush=True)

        return self._parse_cases(page)

    # ── Check a court checkbox ────────────────────────────────────────────────
    def _check_court(self, page, court_name: str) -> int:
        """Find and check the checkbox for a given court name. Returns 1 if checked."""
        try:
            # Try label text match
            label = page.query_selector(f"label:has-text('{court_name}')")
            if label:
                cb_id = label.get_attribute("for")
                if cb_id:
                    cb = page.query_selector(f"#{cb_id}")
                    if cb and not cb.is_checked():
                        cb.check()
                        print(f"[probate_ga]   Checked: {court_name}", flush=True)
                        return 1
                else:
                    # Label wraps the checkbox
                    cb = label.query_selector("input[type='checkbox']")
                    if cb and not cb.is_checked():
                        cb.check()
                        print(f"[probate_ga]   Checked: {court_name}", flush=True)
                        return 1

            # Try searching by text in any element
            els = page.query_selector_all(f"[role='option']:has-text('{court_name}'), li:has-text('{court_name}')")
            for el in els:
                cb = el.query_selector("input[type='checkbox']")
                if not cb:
                    # The element itself may be clickable
                    el.click()
                    print(f"[probate_ga]   Clicked: {court_name}", flush=True)
                    return 1
                if not cb.is_checked():
                    cb.check()
                    print(f"[probate_ga]   Checked: {court_name}", flush=True)
                    return 1

            # Partial match fallback
            keyword = court_name.split()[0]  # e.g. "FULTON"
            for el in page.query_selector_all(f"label, li, [role='option']"):
                try:
                    txt = el.inner_text().strip().upper()
                    if keyword in txt and "PROBATE" in txt:
                        cb = el.query_selector("input[type='checkbox']")
                        if cb and not cb.is_checked():
                            cb.check()
                            print(f"[probate_ga]   Checked (partial): {txt[:60]}", flush=True)
                            return 1
                        elif not cb:
                            el.click()
                            print(f"[probate_ga]   Clicked (partial): {txt[:60]}", flush=True)
                            return 1
                except Exception:
                    continue

            print(f"[probate_ga]   NOT FOUND: {court_name}", flush=True)
        except Exception as exc:
            print(f"[probate_ga]   ERROR checking {court_name}: {exc}", flush=True)
        return 0

    # ── Parse case cards from results page ───────────────────────────────────
    def _parse_cases(self, page) -> list[dict]:
        """Parse case cards/rows from the search results page."""
        cases: list[dict] = []
        try:
            html = page.content()
            # Save for debugging
            with open("/tmp/ga_results.html", "w") as f:
                f.write(html)
            print("[probate_ga] Results HTML saved: /tmp/ga_results.html", flush=True)

            soup = BeautifulSoup(html, "html.parser")

            # re:SearchGA renders results as cards with class names like
            # "case-card", "search-result", or as table rows.
            # Try multiple selectors.
            case_elements = (
                soup.find_all(class_=re.compile(r"case.?card|search.?result|result.?item", re.I))
                or soup.find_all("article")
                or soup.find_all(attrs={"data-testid": re.compile(r"case|result", re.I)})
            )

            if not case_elements:
                # Fall back to table rows
                for tbl in soup.find_all("table"):
                    rows = tbl.find_all("tr")[1:]
                    if rows:
                        for row in rows:
                            cells = row.find_all("td")
                            if len(cells) >= 3:
                                case_elements.append(row)
                        break

            print(f"[probate_ga] Found {len(case_elements)} case elements.", flush=True)

            for el in case_elements:
                case = self._extract_case_card(el)
                if not case:
                    continue

                # Filter by date
                filed_date = self._parse_date(case.get("filed_date", ""))
                if filed_date and filed_date < self._cutoff:
                    continue

                # Get case detail URL
                link = el.find("a", href=True)
                if link:
                    href = link["href"]
                    case["case_url"] = (
                        href if href.startswith("http")
                        else BASE_URL + (href if href.startswith("/") else "/" + href)
                    )
                else:
                    case["case_url"] = ""

                cases.append(case)
                print(f"[probate_ga]   {case.get('case_number', '?')} | {case.get('case_name', '?')} | {case.get('filed_date', '?')} | {case.get('court', '?')}", flush=True)

        except Exception as exc:
            print(f"[probate_ga] ERROR parsing results: {exc}", flush=True)
            traceback.print_exc()

        return cases

    def _extract_case_card(self, el) -> Optional[dict]:
        """Extract case fields from a single card/row element."""
        try:
            text = el.get_text(separator="|", strip=True)
            if not text or len(text) < 5:
                return None

            # Try structured extraction first
            case: dict = {}

            # Case number — patterns like "2026-ES-001234" or "2026P001234"
            m = re.search(r"\b(\d{4}[-–]?(?:ES|P|PR|ADM|PROB)[-–]?\d+)\b", text, re.I)
            if m:
                case["case_number"] = m.group(1)

            # Case name — usually "Estate of FIRSTNAME LASTNAME"
            m2 = re.search(r"(Estate of [A-Z][A-Za-z ,.']+)", text)
            if m2:
                case["case_name"] = m2.group(1).strip()

            # Filed date — MM/DD/YYYY or YYYY-MM-DD
            m3 = re.search(r"\b(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})\b", text)
            if m3:
                case["filed_date"] = m3.group(1)

            # Court name
            for court in TARGET_COURTS:
                if court.upper() in text.upper():
                    case["court"] = court
                    case["county"] = COURT_TO_COUNTY.get(court, "")
                    break

            # Parties — names in the element
            party_els = el.find_all(class_=re.compile(r"party|name|person", re.I))
            parties = [p.get_text(strip=True) for p in party_els if p.get_text(strip=True)]
            if parties:
                case["parties"] = parties

            # Fallback: use the full text as case_name if nothing found
            if not case.get("case_number") and not case.get("case_name"):
                return None

            # Defaults
            case.setdefault("case_number", "")
            case.setdefault("case_name", "")
            case.setdefault("filed_date", "")
            case.setdefault("court", "")
            case.setdefault("county", "")
            case.setdefault("parties", [])
            case.setdefault("case_type", "Probate")
            case.setdefault("state", "GA")

            return case

        except Exception as exc:
            print(f"[probate_ga]   WARNING: card extraction: {exc}", flush=True)
            return None

    # ── Process one case ──────────────────────────────────────────────────────
    def _process_case(self, page, case: dict) -> int:
        """Navigate to case detail, find petition doc, screenshot, GPT-4o, save."""
        case_url = case.get("case_url", "")
        if not case_url:
            print("[probate_ga]   No case URL — skipping.", flush=True)
            return 0

        print(f"[probate_ga]   Loading case detail: {case_url}", flush=True)
        try:
            page.goto(case_url, wait_until="domcontentloaded", timeout=30000)
        except PWTimeout:
            print("[probate_ga]   Timeout on case detail.", flush=True)
            return 0
        time.sleep(REQUEST_DELAY)

        self._screenshot(page, f"/tmp/ga_case_{case.get('case_number', 'unknown').replace('/', '_')}.png")

        # Find petition document link
        petition_link = self._find_petition_link(page)
        if not petition_link:
            print("[probate_ga]   No petition document found — saving with case data only.", flush=True)
            return self._save_no_doc(case)

        print(f"[probate_ga]   Petition doc: {petition_link.inner_text().strip()[:60]}", flush=True)

        # Click the document link to open Document Preview popup
        try:
            with page.expect_popup(timeout=10000) as popup_info:
                petition_link.click()
            popup = popup_info.value
            popup.wait_for_load_state("domcontentloaded")
            time.sleep(REQUEST_DELAY)
            print(f"[probate_ga]   Popup: {popup.title()} | {popup.url}", flush=True)
            sshot_path = f"/tmp/ga_petition_{case.get('case_number', 'unknown').replace('/', '_')}.png"
            popup.screenshot(path=sshot_path, full_page=True)
            print(f"[probate_ga]   Screenshot: {sshot_path}", flush=True)
            popup.close()
        except PWTimeout:
            # No popup — try clicking and waiting for new content on same page
            print("[probate_ga]   No popup — trying same-page click.", flush=True)
            try:
                petition_link.click()
                time.sleep(REQUEST_DELAY)
                sshot_path = f"/tmp/ga_petition_{case.get('case_number', 'unknown').replace('/', '_')}.png"
                page.screenshot(path=sshot_path, full_page=True)
                print(f"[probate_ga]   Screenshot (same page): {sshot_path}", flush=True)
            except Exception as exc:
                print(f"[probate_ga]   Click error: {exc}", flush=True)
                return self._save_no_doc(case)
        except Exception as exc:
            print(f"[probate_ga]   Popup error: {exc}", flush=True)
            return self._save_no_doc(case)

        # Send screenshot to GPT-4o
        pdf_data = self._extract_screenshot(sshot_path, case.get("case_number", ""))
        if not pdf_data:
            print("[probate_ga]   GPT-4o extraction failed — saving with case data only.", flush=True)
            return self._save_no_doc(case)

        return self._save_lead(case, pdf_data)

    # ── Find petition document link ───────────────────────────────────────────
    def _find_petition_link(self, page):
        """Find the petition document link on the case detail page."""
        try:
            for link in page.query_selector_all("a"):
                try:
                    txt = link.inner_text().strip().lower()
                    for kw in PETITION_DOC_KEYWORDS:
                        if kw in txt:
                            return link
                except Exception:
                    continue
        except Exception as exc:
            print(f"[probate_ga]   WARNING: petition link search: {exc}", flush=True)
        return None

    # ── GPT-4o screenshot extraction ──────────────────────────────────────────
    def _extract_screenshot(self, sshot_path: str, label: str) -> Optional[dict]:
        if not self._openai:
            print("[probate_ga]   OpenAI not available.", flush=True)
            return None
        try:
            with open(sshot_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            print(f"[probate_ga]   GPT-4o: {label} ({len(b64)//1024}KB)...", flush=True)

            resp = self._openai.chat.completions.create(
                model=GPT_MODEL,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _GPT_PROMPT},
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/png;base64,{b64}",
                            "detail": "high",
                        }},
                    ],
                }],
                max_tokens=1500,
                temperature=0,
            )
            raw = resp.choices[0].message.content.strip()
            print(f"[probate_ga]   GPT-4o {label}: {raw[:200]}", flush=True)

            # Strip markdown fences
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw).strip()
            return json.loads(raw)

        except json.JSONDecodeError as exc:
            print(f"[probate_ga]   GPT-4o non-JSON for {label}: {exc}", flush=True)
            return None
        except Exception as exc:
            print(f"[probate_ga]   GPT-4o error for {label}: {exc}", flush=True)
            return None

    # ── Save lead with GPT-4o data ────────────────────────────────────────────
    def _save_lead(self, case: dict, doc_data: dict) -> int:
        if not _DB_AVAILABLE:
            print("[probate_ga]   DB not available — printing summary:", flush=True)
            print(f"[probate_ga]   Decedent: {doc_data.get('decedent_name')}", flush=True)
            print(f"[probate_ga]   Address:  {doc_data.get('decedent_address')}", flush=True)
            print(f"[probate_ga]   Petitioner: {doc_data.get('petitioner_name')}", flush=True)
            return 0

        try:
            db = SessionLocal()
            try:
                # Dedup by case_number — check for exact JSON key match
                case_num = case.get("case_number", "")
                if case_num:
                    # Use JSON key pattern to avoid false positives from substring matches
                    existing = db.query(Lead).filter(
                        Lead.source == "probate",
                        Lead.raw_data.like(f'%"case_number": "{case_num}"%')
                    ).first()
                    if existing:
                        print(f"[probate_ga]   DUPLICATE: {case_num} (lead id={existing.id}) — skipping.", flush=True)
                        return 0

                decedent_addr = doc_data.get("decedent_address") or ""
                if not decedent_addr:
                    decedent_addr = (
                        f"{doc_data.get('decedent_name') or case.get('case_name', 'Unknown')} — "
                        f"{case.get('county', '')} County, GA"
                    )

                county = case.get("county", "")
                # Try to derive county from court name if not set
                if not county:
                    for court, c in COURT_TO_COUNTY.items():
                        if court.upper() in case.get("court", "").upper():
                            county = c
                            break

                raw_data = {
                    "case_number":            case_num,
                    "case_name":              case.get("case_name", ""),
                    "court":                  case.get("court", ""),
                    "case_url":               case.get("case_url", ""),
                    "filed_date":             case.get("filed_date", ""),
                    "case_type":              case.get("case_type", "Probate"),
                    "state":                  "GA",
                    "county":                 county,
                    "decedent_name":          doc_data.get("decedent_name"),
                    "decedent_address":       decedent_addr,
                    "date_of_death":          doc_data.get("date_of_death"),
                    "petitioner_name":        doc_data.get("petitioner_name"),
                    "petitioner_address":     doc_data.get("petitioner_address"),
                    "petitioner_relationship":doc_data.get("petitioner_relationship"),
                    "died_intestate":         doc_data.get("died_intestate"),
                    "heirs":                  doc_data.get("heirs", []),
                    "parties":                case.get("parties", []),
                }

                lead = Lead(
                    address=decedent_addr,
                    parcel_id=None,
                    source="probate",
                    raw_data=json.dumps(raw_data),
                    score=self._score(doc_data),
                    status="new",
                    state="GA",
                    county=county,
                )
                db.add(lead)
                db.flush()

                contacts_saved = 0

                # Save petitioner as contact — source='petition_document' marks
                # this as the highest-quality phone (petitioner wrote it themselves)
                petitioner_name = doc_data.get("petitioner_name")
                petitioner_phone = doc_data.get("petitioner_phone")
                if petitioner_name:
                    db.add(Contact(
                        lead_id=lead.id,
                        owner_name=petitioner_name,
                        phone=petitioner_phone,
                        email=None,
                        source="petition_document",
                    ))
                    contacts_saved += 1
                    print(
                        f"[probate_ga]   CONTACT (petition_document): {petitioner_name} "
                        f"| phone={petitioner_phone or 'none'} "
                        f"| rel={doc_data.get('petitioner_relationship', '')}",
                        flush=True,
                    )

                # Save all heirs as contacts
                for heir in (doc_data.get("heirs") or []):
                    heir_name = heir.get("name") if isinstance(heir, dict) else str(heir)
                    if heir_name:
                        db.add(Contact(
                            lead_id=lead.id,
                            owner_name=heir_name,
                            phone=None,
                            email=None,
                            source="probate",
                        ))
                        contacts_saved += 1
                        print(f"[probate_ga]   CONTACT (heir): {heir_name} ({heir.get('relationship', '') if isinstance(heir, dict) else ''})", flush=True)

                db.commit()
                print(f"[probate_ga]   SAVED: lead id={lead.id} | {decedent_addr} | {contacts_saved} contact(s)", flush=True)
                return 1

            except Exception as exc:
                db.rollback()
                print(f"[probate_ga]   DB save error: {exc}", flush=True)
                traceback.print_exc()
                return 0
            finally:
                db.close()

        except Exception as exc:
            print(f"[probate_ga]   DB session error: {exc}", flush=True)
            return 0

    # ── Save lead without document data ──────────────────────────────────────
    def _save_no_doc(self, case: dict) -> int:
        """Save a lead using only case-card data when no document is available."""
        if not _DB_AVAILABLE:
            return 0
        try:
            db = SessionLocal()
            try:
                case_num = case.get("case_number", "")
                if case_num:
                    # Use JSON key pattern to avoid false positives from substring matches
                    existing = db.query(Lead).filter(
                        Lead.source == "probate",
                        Lead.raw_data.like(f'%"case_number": "{case_num}"%')
                    ).first()
                    if existing:
                        print(f"[probate_ga]   DUPLICATE (no doc): {case_num} — skipping.", flush=True)
                        return 0

                county = case.get("county", "")
                address = (
                    f"{case.get('case_name', 'Unknown')} — "
                    f"{county} County, GA (address pending document read)"
                )

                raw_data = {
                    "case_number": case_num,
                    "case_name":   case.get("case_name", ""),
                    "court":       case.get("court", ""),
                    "case_url":    case.get("case_url", ""),
                    "filed_date":  case.get("filed_date", ""),
                    "state":       "GA",
                    "county":      county,
                    "parties":     case.get("parties", []),
                    "note":        "Document preview not available",
                }

                lead = Lead(
                    address=address,
                    parcel_id=None,
                    source="probate",
                    raw_data=json.dumps(raw_data),
                    score=40.0,
                    status="new",
                    state="GA",
                    county=county,
                )
                db.add(lead)
                db.commit()
                print(f"[probate_ga]   SAVED (no doc): lead id={lead.id}", flush=True)
                return 1

            except Exception as exc:
                db.rollback()
                print(f"[probate_ga]   DB save error (no doc): {exc}", flush=True)
                return 0
            finally:
                db.close()

        except Exception as exc:
            print(f"[probate_ga]   DB session error: {exc}", flush=True)
            return 0

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _select_option_by_text(self, page, text: str, selectors: list[str]):
        for sel_str in selectors:
            try:
                el = page.query_selector(sel_str)
                if el:
                    opts = el.query_selector_all("option")
                    for opt in opts:
                        if text.lower() in opt.inner_text().lower():
                            el.select_option(value=opt.get_attribute("value"))
                            print(f"[probate_ga]   Selected '{opt.inner_text().strip()}' in {sel_str}", flush=True)
                            return
            except Exception:
                continue
        # Try radio buttons or tabs
        for btn in page.query_selector_all("button, [role='tab'], [role='radio'], label"):
            try:
                if text.lower() in btn.inner_text().lower():
                    btn.click()
                    print(f"[probate_ga]   Clicked '{text}' button/tab.", flush=True)
                    return
            except Exception:
                continue
        print(f"[probate_ga]   WARNING: Could not select '{text}'", flush=True)

    def _find_button(self, page, labels: list[str]):
        for label in labels:
            for sel in [
                f"button:has-text('{label}')",
                f"input[value='{label}']",
                f"[role='button']:has-text('{label}')",
                f"a:has-text('{label}')",
            ]:
                try:
                    el = page.query_selector(sel)
                    if el:
                        return el
                except Exception:
                    continue
        return None

    def _parse_date(self, raw: str) -> Optional[datetime.date]:
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.datetime.strptime(raw.strip(), fmt).date()
            except (ValueError, AttributeError):
                continue
        return None

    def _screenshot(self, page, path: str):
        try:
            page.screenshot(path=path)
            print(f"[probate_ga]   Screenshot: {path}", flush=True)
        except Exception:
            pass

    def _score(self, doc_data: dict) -> float:
        score = 55.0
        if doc_data.get("decedent_address"):  score += 15
        if doc_data.get("petitioner_name"):   score += 10
        if doc_data.get("petitioner_address"):score += 5
        heirs = doc_data.get("heirs") or []
        if heirs:
            score += min(len(heirs) * 3, 15)
        return min(score, 100.0)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Georgia probate scraper — re:SearchGA")
    parser.add_argument("--days", type=int, default=90,
                        help="Days back to search (default: 90)")
    args = parser.parse_args()

    scraper = GeorgiaProbateScraper(days=args.days)
    scraper.scrape()


if __name__ == "__main__":
    main()
