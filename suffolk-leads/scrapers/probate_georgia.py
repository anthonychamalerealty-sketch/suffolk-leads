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
        self.days     = days
        self._cutoff  = datetime.date.today() - datetime.timedelta(days=days)
        self.email    = os.environ.get("RESEARCHGA_EMAIL", "")
        self.password = os.environ.get("RESEARCHGA_PASSWORD", "")
        self._openai  = None

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

        # ── Try to click "Sign in" / "Login" button ────────────────────────
        # re:SearchGA uses a React SPA — the button text varies by version.
        # We try several selectors in priority order.
        signin_clicked = False
        for selector in [
            "button:has-text('Sign in with eFileGA')",
            "a:has-text('Sign in with eFileGA')",
            "button:has-text('Sign In')",
            "button:has-text('Login')",
            "a:has-text('Sign In')",
            "a:has-text('Login')",
            "[data-testid='signin-btn']",
        ]:
            try:
                el = page.query_selector(selector)
                if el and el.is_visible():
                    el.click()
                    print(f"[probate_ga] Clicked sign-in: '{el.inner_text().strip()[:60]}'", flush=True)
                    signin_clicked = True
                    time.sleep(REQUEST_DELAY)
                    break
            except Exception:
                continue

        if not signin_clicked:
            # Scan all visible buttons/links for sign-in text
            for el in page.query_selector_all("button, a"):
                try:
                    txt = el.inner_text().lower()
                    if any(kw in txt for kw in ["sign in", "login", "efile"]):
                        el.click()
                        print(f"[probate_ga] Clicked sign-in (scan): '{txt[:60]}'", flush=True)
                        signin_clicked = True
                        time.sleep(REQUEST_DELAY)
                        break
                except Exception:
                    continue

        if not signin_clicked:
            # Navigate directly to the login page
            print("[probate_ga] Sign-in button not found — navigating directly to login.", flush=True)
            for login_url in [
                f"{BASE_URL}/CourtRecordsSearch/ui/login",
                "https://login.efiling.tylerhost.net/",
            ]:
                try:
                    page.goto(login_url, wait_until="domcontentloaded", timeout=15000)
                    time.sleep(2)
                    if page.query_selector("input[type='email'], input[name='email']"):
                        break
                except Exception:
                    continue

        self._screenshot(page, "/tmp/ga_02_login_page.png")
        print(f"[probate_ga] Login page: {page.title()} | {page.url}", flush=True)

        # ── Fill email ─────────────────────────────────────────────────────
        email_inp = None
        for sel in [
            "input[type='email']",
            "input[name='email']",
            "input[id*='email' i]",
            "input[placeholder*='email' i]",
            "input[name='username']",
            "input[id*='username' i]",
            "input[autocomplete='username']",
        ]:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    email_inp = el
                    break
            except Exception:
                continue

        if email_inp:
            email_inp.click()
            email_inp.fill(self.email)
            print(f"[probate_ga] Filled email: {self.email[:20]}…", flush=True)
        else:
            print("[probate_ga] ERROR: Email input not found.", flush=True)
            self._screenshot(page, "/tmp/ga_02b_no_email_input.png")
            return False

        # ── Fill password ──────────────────────────────────────────────────
        pass_inp = None
        for sel in [
            "input[type='password']",
            "input[name='password']",
            "input[id*='password' i]",
            "input[autocomplete='current-password']",
        ]:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    pass_inp = el
                    break
            except Exception:
                continue

        if pass_inp:
            pass_inp.fill(self.password)
            print("[probate_ga] Filled password.", flush=True)
        else:
            print("[probate_ga] ERROR: Password input not found.", flush=True)
            return False

        # ── Submit ─────────────────────────────────────────────────────────
        submit_btn = None
        for sel in [
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Sign In')",
            "button:has-text('Login')",
            "button:has-text('Continue')",
            "button:has-text('Next')",
        ]:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    submit_btn = el
                    break
            except Exception:
                continue

        if submit_btn:
            submit_btn.click()
            print("[probate_ga] Submitted login form.", flush=True)
        else:
            page.keyboard.press("Enter")
            print("[probate_ga] Pressed Enter to submit.", flush=True)

        time.sleep(REQUEST_DELAY + 2)
        self._screenshot(page, "/tmp/ga_03_after_login.png")
        print(f"[probate_ga] After login: {page.title()} | {page.url}", flush=True)

        # ── Check for login failure ────────────────────────────────────────
        try:
            body_text = page.inner_text("body")[:500].lower()
        except Exception:
            body_text = ""
        if any(kw in body_text for kw in ["invalid", "incorrect", "failed", "wrong password"]):
            print("[probate_ga] ERROR: Login failed — invalid credentials.", flush=True)
            return False

        # ── Navigate to Advanced Search to confirm login worked ────────────
        try:
            page.goto(ADV_SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(REQUEST_DELAY)
        except PWTimeout:
            print("[probate_ga] ERROR: Timeout on Advanced Search page.", flush=True)
            return False

        self._screenshot(page, "/tmp/ga_04_adv_search.png")
        print(f"[probate_ga] Advanced Search: {page.title()} | {page.url}", flush=True)

        if "login" in page.url.lower() or "signin" in page.url.lower():
            print("[probate_ga] ERROR: Redirected to login — credentials rejected.", flush=True)
            return False

        print("[probate_ga] Login successful.", flush=True)
        return True

    # ── Advanced Search ───────────────────────────────────────────────────────
    def _advanced_search(self, page) -> list[dict]:
        """Configure and run the Advanced Search, return list of case dicts."""
        print("[probate_ga] Configuring Advanced Search...", flush=True)

        if "advancedSearch" not in page.url:
            try:
                page.goto(ADV_SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
                time.sleep(REQUEST_DELAY)
            except PWTimeout:
                print("[probate_ga] ERROR: Timeout navigating to Advanced Search.", flush=True)
                return []

        self._screenshot(page, "/tmp/ga_04_adv_search.png")

        # ── Set "Search for" = Cases ───────────────────────────────────────
        self._select_option_by_text(page, "Cases", [
            "select[id*='searchFor' i]",
            "select[name*='searchFor' i]",
            "select[id*='SearchFor']",
        ])
        time.sleep(1)

        # ── Set "Search by" = Case Location ───────────────────────────────
        self._select_option_by_text(page, "Case Location", [
            "select[id*='searchBy' i]",
            "select[name*='searchBy' i]",
            "select[id*='SearchBy']",
        ])
        time.sleep(1)
        self._screenshot(page, "/tmp/ga_04b_search_by_set.png")

        # ── Click "Select" button to open location picker ─────────────────
        select_btn = self._find_button(page, ["Select", "Choose Location", "Add Location"])
        if select_btn:
            select_btn.click()
            print("[probate_ga] Clicked 'Select' for location.", flush=True)
            time.sleep(REQUEST_DELAY)
        else:
            print("[probate_ga] WARNING: 'Select' button not found — trying to proceed.", flush=True)

        self._screenshot(page, "/tmp/ga_05_location_picker.png")

        # ── Search for "Probate" in the location search box ───────────────
        # The modal/dialog search box — try multiple selectors
        loc_search = None
        for sel in [
            "[role='dialog'] input[type='text']",
            "dialog input[type='text']",
            ".modal input[type='text']",
            "input[placeholder*='search' i]",
            "input[placeholder*='filter' i]",
            "input[aria-label*='search' i]",
            "input[aria-label*='location' i]",
        ]:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    loc_search = el
                    break
            except Exception:
                continue

        if loc_search:
            loc_search.click()
            loc_search.fill("Probate")
            print("[probate_ga] Typed 'Probate' in location search.", flush=True)
            time.sleep(2)
        else:
            print("[probate_ga] WARNING: Location search box not found.", flush=True)

        self._screenshot(page, "/tmp/ga_06_probate_results.png")

        # ── Check all 7 target courts ──────────────────────────────────────
        checked = 0
        for court_name in TARGET_COURTS:
            checked += self._check_court(page, court_name)

        print(f"[probate_ga] Checked {checked}/{len(TARGET_COURTS)} courts.", flush=True)
        self._screenshot(page, "/tmp/ga_07_courts_checked.png")

        # ── Click "Done" or "Apply" ────────────────────────────────────────
        done_btn = self._find_button(page, ["Done", "Apply", "OK", "Confirm", "Close"])
        if done_btn:
            done_btn.click()
            print("[probate_ga] Clicked 'Done'.", flush=True)
            time.sleep(REQUEST_DELAY)
        else:
            print("[probate_ga] WARNING: 'Done' button not found — pressing Escape.", flush=True)
            page.keyboard.press("Escape")
            time.sleep(1)

        self._screenshot(page, "/tmp/ga_08_after_done.png")

        # ── Click "Search" ─────────────────────────────────────────────────
        search_btn = self._find_button(page, ["Search", "Find Cases", "Run Search"])
        if search_btn:
            search_btn.click()
            print("[probate_ga] Clicked 'Search'.", flush=True)
        else:
            submit = page.query_selector("button[type='submit'], input[type='submit']")
            if submit:
                submit.click()
                print("[probate_ga] Clicked submit.", flush=True)
            else:
                print("[probate_ga] ERROR: Search button not found.", flush=True)
                return []

        # Wait for results — re:SearchGA is a React SPA, results load async
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            time.sleep(REQUEST_DELAY + 2)

        self._screenshot(page, "/tmp/ga_09_results.png")
        print(f"[probate_ga] Results page: {page.title()} | {page.url}", flush=True)

        return self._parse_cases(page)

    # ── Check a court checkbox ────────────────────────────────────────────────
    def _check_court(self, page, court_name: str) -> int:
        """Find and check the checkbox for a given court name. Returns 1 if checked."""
        try:
            # Strategy 1: exact label text
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
                    cb = label.query_selector("input[type='checkbox']")
                    if cb and not cb.is_checked():
                        cb.check()
                        print(f"[probate_ga]   Checked: {court_name}", flush=True)
                        return 1

            # Strategy 2: role=option or list item
            for el in page.query_selector_all(
                f"[role='option']:has-text('{court_name}'), "
                f"li:has-text('{court_name}'), "
                f"[role='listitem']:has-text('{court_name}')"
            ):
                cb = el.query_selector("input[type='checkbox']")
                if cb:
                    if not cb.is_checked():
                        cb.check()
                        print(f"[probate_ga]   Checked (option): {court_name}", flush=True)
                        return 1
                else:
                    el.click()
                    print(f"[probate_ga]   Clicked (option): {court_name}", flush=True)
                    return 1

            # Strategy 3: partial keyword match (first word + PROBATE)
            keyword = court_name.split()[0].upper()
            for el in page.query_selector_all("label, li, [role='option'], [role='listitem']"):
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
            with open("/tmp/ga_results.html", "w") as f:
                f.write(html)
            print("[probate_ga] Results HTML saved: /tmp/ga_results.html", flush=True)

            soup = BeautifulSoup(html, "html.parser")

            # re:SearchGA renders results as React cards. Try multiple selectors.
            # The actual class names vary by deployment version.
            case_elements = []

            # Priority 1: elements with data attributes indicating case results
            for attr in ["data-case-id", "data-result-id", "data-id"]:
                els = soup.find_all(attrs={attr: True})
                if els:
                    case_elements = els
                    print(f"[probate_ga] Found {len(els)} case elements via [{attr}].", flush=True)
                    break

            # Priority 2: class name patterns
            if not case_elements:
                for pattern in [
                    r"case.?card", r"search.?result", r"result.?item",
                    r"case.?row", r"case.?list.?item",
                ]:
                    els = soup.find_all(class_=re.compile(pattern, re.I))
                    if els:
                        case_elements = els
                        print(f"[probate_ga] Found {len(els)} case elements via class '{pattern}'.", flush=True)
                        break

            # Priority 3: article tags
            if not case_elements:
                els = soup.find_all("article")
                if els:
                    case_elements = els
                    print(f"[probate_ga] Found {len(els)} case elements via <article>.", flush=True)

            # Priority 4: table rows
            if not case_elements:
                for tbl in soup.find_all("table"):
                    rows = tbl.find_all("tr")[1:]
                    if len(rows) >= 1:
                        case_elements = rows
                        print(f"[probate_ga] Found {len(rows)} case elements via <table>.", flush=True)
                        break

            # Priority 5: use Playwright to get clickable case links directly
            if not case_elements:
                print("[probate_ga] Falling back to Playwright link extraction.", flush=True)
                return self._parse_cases_playwright(page)

            print(f"[probate_ga] Parsing {len(case_elements)} case elements...", flush=True)

            for el in case_elements:
                case = self._extract_case_card(el)
                if not case:
                    continue

                # Filter by date
                filed_date = self._parse_date(case.get("filed_date", ""))
                if filed_date and filed_date < self._cutoff:
                    continue

                # Get case detail URL from the first link in the element
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
                print(
                    f"[probate_ga]   {case.get('case_number', '?')} | "
                    f"{case.get('case_name', '?')} | "
                    f"{case.get('filed_date', '?')} | "
                    f"{case.get('court', '?')}",
                    flush=True,
                )

        except Exception as exc:
            print(f"[probate_ga] ERROR parsing results: {exc}", flush=True)
            traceback.print_exc()

        return cases

    def _parse_cases_playwright(self, page) -> list[dict]:
        """
        Fallback: use Playwright to directly enumerate case links on the results
        page, since BeautifulSoup may miss dynamically rendered React content.
        """
        cases: list[dict] = []
        try:
            # Wait for at least one case link to appear
            page.wait_for_selector(
                "a[href*='CaseDetail'], a[href*='caseDetail'], "
                "a[href*='case/'], a[href*='Case/']",
                timeout=10000,
            )
        except Exception:
            print("[probate_ga] No case links found on results page.", flush=True)
            return cases

        links = page.query_selector_all(
            "a[href*='CaseDetail'], a[href*='caseDetail'], "
            "a[href*='case/'], a[href*='Case/']"
        )
        print(f"[probate_ga] Playwright found {len(links)} case links.", flush=True)

        seen_urls: set[str] = set()
        for link in links:
            try:
                href = link.get_attribute("href") or ""
                if not href or href in seen_urls:
                    continue
                seen_urls.add(href)

                case_url = href if href.startswith("http") else BASE_URL + href
                # Try to extract case number and name from the link text or parent
                txt = ""
                try:
                    txt = link.inner_text().strip()
                except Exception:
                    pass
                if not txt:
                    try:
                        parent = link.evaluate_handle("el => el.closest('[class]')")
                        txt = parent.as_element().inner_text().strip() if parent else ""
                    except Exception:
                        pass

                case_num = ""
                m = re.search(r"\b(\d{4}[-–]?(?:ES|P|PR|ADM|PROB)[-–]?\d+)\b", txt, re.I)
                if m:
                    case_num = m.group(1)

                case_name = ""
                m2 = re.search(r"(Estate of [A-Z][A-Za-z ,.']+)", txt)
                if m2:
                    case_name = m2.group(1).strip()

                filed_date = ""
                m3 = re.search(r"\b(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})\b", txt)
                if m3:
                    filed_date = m3.group(1)

                # Date filter
                fd = self._parse_date(filed_date)
                if fd and fd < self._cutoff:
                    continue

                cases.append({
                    "case_number": case_num,
                    "case_name":   case_name,
                    "filed_date":  filed_date,
                    "court":       "",
                    "county":      "",
                    "parties":     [],
                    "case_type":   "Probate",
                    "state":       "GA",
                    "case_url":    case_url,
                })
                print(f"[probate_ga]   (pw) {case_num} | {case_name} | {case_url}", flush=True)
            except Exception as exc:
                print(f"[probate_ga]   WARNING (pw link): {exc}", flush=True)
                continue

        return cases

    def _extract_case_card(self, el) -> Optional[dict]:
        """Extract case fields from a single card/row element."""
        try:
            text = el.get_text(separator="|", strip=True)
            if not text or len(text) < 5:
                return None

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

            # Parties
            party_els = el.find_all(class_=re.compile(r"party|name|person", re.I))
            parties = [p.get_text(strip=True) for p in party_els if p.get_text(strip=True)]
            if parties:
                case["parties"] = parties

            if not case.get("case_number") and not case.get("case_name"):
                return None

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

        safe_num = case.get("case_number", "unknown").replace("/", "_")
        self._screenshot(page, f"/tmp/ga_case_{safe_num}.png")

        # ── Enrich case data from the detail page ─────────────────────────
        try:
            detail_html = page.content()
            detail_soup = BeautifulSoup(detail_html, "html.parser")

            # Try to find court name if not already set
            if not case.get("court"):
                for court in TARGET_COURTS:
                    if court.upper() in detail_html.upper():
                        case["court"] = court
                        case["county"] = COURT_TO_COUNTY.get(court, "")
                        break

            # Try to find filed date if not already set
            if not case.get("filed_date"):
                m = re.search(r"\b(\d{1,2}/\d{1,2}/\d{4})\b", detail_html)
                if m:
                    case["filed_date"] = m.group(1)

            # Try to find case name if not already set
            if not case.get("case_name"):
                m2 = re.search(r"Estate of ([A-Z][A-Za-z ,.']+)", detail_html)
                if m2:
                    case["case_name"] = "Estate of " + m2.group(1).strip()
        except Exception:
            pass

        # ── Find petition document link ────────────────────────────────────
        petition_link = self._find_petition_link(page)
        if not petition_link:
            print("[probate_ga]   No petition document found — saving with case data only.", flush=True)
            return self._save_no_doc(case)

        print(f"[probate_ga]   Petition doc: {petition_link.inner_text().strip()[:60]}", flush=True)

        # ── Click the document link → Document Preview popup ──────────────
        sshot_path = f"/tmp/ga_petition_{safe_num}.png"
        popup_success = False

        try:
            with page.expect_popup(timeout=12000) as popup_info:
                petition_link.click()
            popup = popup_info.value
            try:
                popup.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            time.sleep(REQUEST_DELAY)
            print(f"[probate_ga]   Popup: {popup.title()} | {popup.url}", flush=True)
            popup.screenshot(path=sshot_path, full_page=True)
            print(f"[probate_ga]   Screenshot: {sshot_path}", flush=True)
            popup.close()
            popup_success = True
        except PWTimeout:
            print("[probate_ga]   No popup — trying same-page click.", flush=True)
        except Exception as exc:
            print(f"[probate_ga]   Popup error: {exc}", flush=True)

        if not popup_success:
            try:
                petition_link.click()
                time.sleep(REQUEST_DELAY)
                page.screenshot(path=sshot_path, full_page=True)
                print(f"[probate_ga]   Screenshot (same page): {sshot_path}", flush=True)
                popup_success = True
            except Exception as exc:
                print(f"[probate_ga]   Click error: {exc}", flush=True)
                return self._save_no_doc(case)

        # ── Send screenshot to GPT-4o ──────────────────────────────────────
        pdf_data = self._extract_screenshot(sshot_path, case.get("case_number", ""))
        if not pdf_data:
            print("[probate_ga]   GPT-4o extraction failed — saving with case data only.", flush=True)
            return self._save_no_doc(case)

        return self._save_lead(case, pdf_data)

    # ── Find petition document link ───────────────────────────────────────────
    def _find_petition_link(self, page):
        """Find the petition document link on the case detail page."""
        try:
            # First pass: exact keyword match in link text
            for link in page.query_selector_all("a"):
                try:
                    txt = link.inner_text().strip().lower()
                    for kw in PETITION_DOC_KEYWORDS:
                        if kw in txt:
                            return link
                except Exception:
                    continue

            # Second pass: look for document links in Events/Documents section
            for section_sel in [
                "[id*='event' i]", "[class*='event' i]",
                "[id*='document' i]", "[class*='document' i]",
                "section", "div[role='region']",
            ]:
                try:
                    section = page.query_selector(section_sel)
                    if section:
                        for link in section.query_selector_all("a"):
                            txt = link.inner_text().strip().lower()
                            if "petition" in txt:
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
                # Dedup by case_number
                case_num = case.get("case_number", "")
                if case_num:
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
                if not county:
                    for court, c in COURT_TO_COUNTY.items():
                        if court.upper() in case.get("court", "").upper():
                            county = c
                            break

                raw_data = {
                    "case_number":             case_num,
                    "case_name":               case.get("case_name", ""),
                    "court":                   case.get("court", ""),
                    "case_url":                case.get("case_url", ""),
                    "filed_date":              case.get("filed_date", ""),
                    "case_type":               case.get("case_type", "Probate"),
                    "state":                   "GA",
                    "county":                  county,
                    "decedent_name":           doc_data.get("decedent_name"),
                    "decedent_address":        decedent_addr,
                    "date_of_death":           doc_data.get("date_of_death"),
                    "petitioner_name":         doc_data.get("petitioner_name"),
                    "petitioner_address":      doc_data.get("petitioner_address"),
                    "petitioner_relationship": doc_data.get("petitioner_relationship"),
                    "died_intestate":          doc_data.get("died_intestate"),
                    "heirs":                   doc_data.get("heirs", []),
                    "parties":                 case.get("parties", []),
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

                petitioner_name  = doc_data.get("petitioner_name")
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
                        f"[probate_ga]   CONTACT (petitioner): {petitioner_name} "
                        f"| phone={petitioner_phone or 'none'} "
                        f"| rel={doc_data.get('petitioner_relationship', '')}",
                        flush=True,
                    )

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
                        rel = heir.get("relationship", "") if isinstance(heir, dict) else ""
                        print(f"[probate_ga]   CONTACT (heir): {heir_name} ({rel})", flush=True)

                db.commit()
                print(
                    f"[probate_ga]   SAVED: lead id={lead.id} | {decedent_addr} | "
                    f"{contacts_saved} contact(s)",
                    flush=True,
                )
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
                    el.select_option(label=text)
                    print(f"[probate_ga] Selected '{text}' in {sel_str}", flush=True)
                    return
            except Exception:
                continue
        # Fallback: find any <select> and try to select by visible text
        for sel in page.query_selector_all("select"):
            try:
                sel.select_option(label=text)
                print(f"[probate_ga] Selected '{text}' in generic <select>", flush=True)
                return
            except Exception:
                continue
        print(f"[probate_ga] WARNING: Could not select '{text}'", flush=True)

    def _find_button(self, page, labels: list[str]):
        for label in labels:
            for sel in [
                f"button:has-text('{label}')",
                f"a:has-text('{label}')",
                f"[role='button']:has-text('{label}')",
                f"input[value='{label}']",
            ]:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        return el
                except Exception:
                    continue
        return None

    def _parse_date(self, raw: str) -> Optional[datetime.date]:
        if not raw:
            return None
        for fmt in ["%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%d/%m/%Y"]:
            try:
                return datetime.datetime.strptime(raw.strip(), fmt).date()
            except ValueError:
                continue
        return None

    def _screenshot(self, page, path: str):
        try:
            page.screenshot(path=path, full_page=True)
            print(f"[probate_ga] Screenshot: {path}", flush=True)
        except Exception as exc:
            print(f"[probate_ga] Screenshot error: {exc}", flush=True)

    def _score(self, doc_data: dict) -> float:
        score = 60.0
        addr = doc_data.get("decedent_address") or ""
        if addr and len(addr) > 10:
            score += 15
        if doc_data.get("petitioner_phone"):
            score += 10
        if doc_data.get("heirs"):
            score += 5
        if doc_data.get("date_of_death"):
            score += 5
        if doc_data.get("petitioner_name"):
            score += 5
        return min(score, 100.0)


# ── CLI entry point ───────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Georgia Probate Scraper — re:SearchGA")
    parser.add_argument("--days", type=int, default=90,
                        help="How many days back to search (default: 90)")
    args = parser.parse_args()

    scraper = GeorgiaProbateScraper(days=args.days)
    results = scraper.scrape()
    print(f"\n[probate_ga] Returned {len(results)} leads.", flush=True)


if __name__ == "__main__":
    main()
