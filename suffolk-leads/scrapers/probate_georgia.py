"""
probate_georgia.py
------------------
Scrapes real Georgia probate court filings from re:SearchGA
(researchga.tylerhost.net) using Playwright.

EXACT FLOW (based on real screenshots of the site)
===================================================
Step 1 — Login
  Home:  https://researchga.tylerhost.net/CourtRecordsSearch/ui/Home
  Click "Sign in with Your eFileGA Account"
  → Redirects to: https://georgia.tylertech.cloud/idp/account/signin?...
  Fill Email field, fill Password field, click "Sign In"
  → Redirects back to: https://researchga.tylerhost.net/CourtRecordsSearch/ui/dashboard

Step 2 — Advanced Search
  Navigate to: https://researchga.tylerhost.net/CourtRecordsSearch/ui/advancedSearch
  "Search for:" radio → Cases (already selected)
  "Search by:" dropdown → select "Case Location"
  Click "Select" button
  → Location modal opens with:
      - Left: text input "Search for Location" + "Search" button + "SELECT ALL"
      - Right: "Selected Location" checkboxes (pre-populated after search)
  Type "Probate" in the search box, click "Search"
  Check all 7 courts that appear in the results list
  Click "Done"
  Click "Search" button on the main form

Step 3 — Results page
  URL: https://researchga.tylerhost.net/CourtRecordsSearch/ui/advancedSearch
  Each case card shows:
    - Case name (blue link, e.g. "GARRISON, MADREENE PALMALEE")
    - Case number (e.g. "26-E-000861")
    - Location (e.g. "Gwinnett County - Probate Court")
    - Case Type (e.g. "Estate*")
    - Parties (comma-separated names)
    - Attorneys
    - Judge
    - Case Filed Date (e.g. "5/29/2026")
  Click each case name link to go to case detail

Step 4 — Case detail
  URL: https://researchga.tylerhost.net/CourtRecordsSearch/ui/case/{uuid}
  Shows:
    - Case name + case number at top
    - Location, Case Category, Case Type, Case Filed Date, Judge, Case Status
    - Parties table: Type | Name | Nickname/Alias | Attorneys
      Types include: Applicant, Decedent, Deceased, Petitioner, HEIR, etc.
    - Events section with document links (most cost money, skip these)
  
  Extract from Parties table:
    - Decedent/Deceased name → decedent_name
    - Applicant/Petitioner name → petitioner_name (person who filed)
  
  NOTE: Documents cost $6.50 each. We do NOT purchase documents.
  All data we need (names, court, date) is FREE on the case detail page.
  Addresses are NOT available on re:SearchGA — we save what we have.

Step 5 — Save
  leads:    address = "{decedent_name} — {county} County, GA"
            source='probate', state='GA', county=court county
  contacts: petitioner (Applicant/Petitioner type) + all heirs
  raw_data: case_number, court, case_url, filed_date, decedent_name,
            petitioner_name, parties list

Env vars required:
  RESEARCHGA_EMAIL     — re:SearchGA / eFileGA account email
  RESEARCHGA_PASSWORD  — re:SearchGA / eFileGA account password
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import time
import traceback
from typing import Optional

# ── Guarded third-party imports ───────────────────────────────────────────────
try:
    from bs4 import BeautifulSoup
except ImportError as _e:
    print(f"[probate_ga] IMPORT ERROR (bs4): {_e}", flush=True)
    raise

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    _PLAYWRIGHT_AVAILABLE = True
except ImportError as _e:
    print(f"[probate_ga] WARNING: playwright not installed: {_e}", flush=True)
    _PLAYWRIGHT_AVAILABLE = False

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

# ── Constants ─────────────────────────────────────────────────────────────────
BASE_URL       = "https://researchga.tylerhost.net"
HOME_URL       = f"{BASE_URL}/CourtRecordsSearch/ui/Home"
DASHBOARD_URL  = f"{BASE_URL}/CourtRecordsSearch/ui/dashboard"
ADV_SEARCH_URL = f"{BASE_URL}/CourtRecordsSearch/ui/advancedSearch"
REQUEST_DELAY  = 2   # seconds between requests

# Exact court names as shown in the Location modal checkboxes
TARGET_COURTS: list[str] = [
    "FULTON PROBATE COURT",
    "GWINNETT COUNTY - PROBATE COURT",
    "GLYNN - PROBATE COURT",
    "FORSYTH COUNTY - PROBATE COURT A",
    "CHATHAM COUNTY - PROBATE COURT",
    "FORSYTH COUNTY - PROBATE COURT",
    "ROCKDALE PROBATE COURT",
]

COURT_TO_COUNTY: dict[str, str] = {
    "FULTON PROBATE COURT":              "Fulton",
    "GWINNETT COUNTY - PROBATE COURT":   "Gwinnett",
    "GLYNN - PROBATE COURT":             "Glynn",
    "FORSYTH COUNTY - PROBATE COURT A":  "Forsyth",
    "CHATHAM COUNTY - PROBATE COURT":    "Chatham",
    "FORSYTH COUNTY - PROBATE COURT":    "Forsyth",
    "ROCKDALE PROBATE COURT":            "Rockdale",
}

# Party types that mean "the person who died"
DECEDENT_TYPES = {"decedent", "deceased", "decedent/deceased"}

# Party types that mean "the person who filed"
PETITIONER_TYPES = {"applicant", "petitioner", "administrator", "executor", "executrix"}


# ─────────────────────────────────────────────────────────────────────────────
class GeorgiaProbateScraper(BaseScraper):
    """
    Logs into re:SearchGA, searches 7 Georgia probate courts for cases filed
    in the last N days, extracts party names from each case detail page,
    and saves leads and contacts to the database.
    """

    def __init__(self, days: int = 90):
        self.days    = days
        self._cutoff = datetime.date.today() - datetime.timedelta(days=days)
        self.email   = os.environ.get("RESEARCHGA_EMAIL", "")
        self.password = os.environ.get("RESEARCHGA_PASSWORD", "")

    def scrape(self) -> list[dict]:
        print("[probate_ga] ============================================================", flush=True)
        print("[probate_ga] STARTING GEORGIA PROBATE SCRAPER — re:SearchGA", flush=True)
        print(f"[probate_ga] Date cutoff : {self._cutoff} (last {self.days} days)", flush=True)
        print(f"[probate_ga] Playwright  : {_PLAYWRIGHT_AVAILABLE}", flush=True)
        print(f"[probate_ga] Database    : {_DB_AVAILABLE}", flush=True)
        print(f"[probate_ga] Email set   : {bool(self.email)}", flush=True)

        if not _PLAYWRIGHT_AVAILABLE:
            print("[probate_ga] ERROR: playwright required.", flush=True)
            return []

        if not self.email or not self.password:
            print("[probate_ga] ERROR: RESEARCHGA_EMAIL and RESEARCHGA_PASSWORD must be set.", flush=True)
            return []

        saved: list[dict] = []
        total_saved = 0

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                ctx = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1440, "height": 900},
                    locale="en-US",
                )
                page = ctx.new_page()

                # Step 1: Login
                if not self._login(page):
                    browser.close()
                    return []

                # Step 2: Advanced Search → get case list
                cases = self._advanced_search(page)
                print(f"[probate_ga] Total cases found within date range: {len(cases)}", flush=True)

                # Steps 3–4: Process each case
                for i, case in enumerate(cases, 1):
                    print(
                        f"\n[probate_ga] [{i}/{len(cases)}] "
                        f"{case.get('case_number', '?')} — {case.get('case_name', '?')}",
                        flush=True,
                    )
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

    # ── Step 1: Login ─────────────────────────────────────────────────────────
    def _login(self, page) -> bool:
        """
        Navigate to Home, click 'Sign in with Your eFileGA Account',
        fill email + password on the Tyler Technologies Odyssey IDP page,
        click 'Sign In', wait for redirect back to dashboard.
        """
        print("[probate_ga] Navigating to Home...", flush=True)
        try:
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
        except PWTimeout:
            print("[probate_ga] ERROR: Timeout on Home page.", flush=True)
            return False
        time.sleep(2)

        # Click the main sign-in button (id="topSignInButton")
        try:
            page.click("#topSignInButton", timeout=10000)
            print("[probate_ga] Clicked #topSignInButton.", flush=True)
        except Exception:
            # Fallback: any button/link containing "eFileGA"
            try:
                page.click("forge-button:has-text('eFileGA')", timeout=5000)
                print("[probate_ga] Clicked eFileGA button (fallback).", flush=True)
            except Exception:
                try:
                    page.click("text=Sign in with Your eFileGA Account", timeout=5000)
                    print("[probate_ga] Clicked sign-in text (fallback 2).", flush=True)
                except Exception as exc:
                    print(f"[probate_ga] ERROR: Could not click sign-in button: {exc}", flush=True)
                    return False

        # Wait for the Tyler Technologies Odyssey IDP login page
        # URL: https://georgia.tylertech.cloud/idp/account/signin?...
        try:
            page.wait_for_url("**/idp/account/signin**", timeout=15000)
            print(f"[probate_ga] On IDP login page: {page.url[:80]}", flush=True)
        except PWTimeout:
            # May already be on the login page or redirected elsewhere
            print(f"[probate_ga] Current URL: {page.url[:80]}", flush=True)
            if "dashboard" in page.url:
                print("[probate_ga] Already logged in — on dashboard.", flush=True)
                return True

        time.sleep(1)

        # Fill Email
        try:
            page.fill("input[name='Email'], input[type='email'], input[id*='email' i]", self.email)
            print(f"[probate_ga] Filled email: {self.email[:25]}...", flush=True)
        except Exception as exc:
            print(f"[probate_ga] ERROR: Could not fill email: {exc}", flush=True)
            self._screenshot(page, "/tmp/ga_login_error.png")
            return False

        # Fill Password
        try:
            page.fill("input[name='Password'], input[type='password']", self.password)
            print("[probate_ga] Filled password.", flush=True)
        except Exception as exc:
            print(f"[probate_ga] ERROR: Could not fill password: {exc}", flush=True)
            return False

        # Click Sign In button
        try:
            page.click("button[type='submit']:has-text('Sign In'), input[type='submit']", timeout=5000)
            print("[probate_ga] Clicked Sign In.", flush=True)
        except Exception:
            page.keyboard.press("Enter")
            print("[probate_ga] Pressed Enter to submit.", flush=True)

        # Wait for redirect back to re:SearchGA dashboard
        try:
            page.wait_for_url("**/CourtRecordsSearch/**", timeout=20000)
            print(f"[probate_ga] Redirected to: {page.url[:80]}", flush=True)
        except PWTimeout:
            print(f"[probate_ga] WARNING: Timeout waiting for dashboard. URL: {page.url[:80]}", flush=True)

        time.sleep(2)

        # Confirm we are NOT on a login page
        if "signin" in page.url.lower() or "login" in page.url.lower():
            print("[probate_ga] ERROR: Still on login page — credentials rejected.", flush=True)
            self._screenshot(page, "/tmp/ga_login_failed.png")
            return False

        print("[probate_ga] Login successful.", flush=True)
        return True

    # ── Step 2: Advanced Search ───────────────────────────────────────────────
    def _advanced_search(self, page) -> list[dict]:
        """
        Navigate to Advanced Search, configure Case Location search for all
        7 probate courts, run search, parse and return case list.
        """
        print("[probate_ga] Navigating to Advanced Search...", flush=True)
        try:
            page.goto(ADV_SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
        except PWTimeout:
            print("[probate_ga] ERROR: Timeout on Advanced Search.", flush=True)
            return []
        time.sleep(REQUEST_DELAY)

        self._screenshot(page, "/tmp/ga_adv_search.png")

        # ── "Search for:" = Cases (radio button) ──────────────────────────
        # The radio for "Cases" is already selected by default, but ensure it
        try:
            page.click("input[type='radio'][value*='Case' i], label:has-text('Cases') input[type='radio']", timeout=3000)
            print("[probate_ga] Selected 'Cases' radio.", flush=True)
        except Exception:
            print("[probate_ga] 'Cases' radio not found or already selected.", flush=True)
        time.sleep(0.5)

        # ── "Search by:" = Case Location (dropdown) ───────────────────────
        # The dropdown is a <select> or a custom forge-select
        try:
            # Try native <select>
            page.select_option("select", label="Case Location")
            print("[probate_ga] Selected 'Case Location' from <select>.", flush=True)
        except Exception:
            # Try clicking a dropdown and selecting the option
            try:
                page.click("[aria-label*='Search by' i], [id*='searchBy' i], [class*='search-by' i]", timeout=3000)
                time.sleep(0.5)
                page.click("text=Case Location", timeout=3000)
                print("[probate_ga] Selected 'Case Location' from custom dropdown.", flush=True)
            except Exception as exc:
                print(f"[probate_ga] WARNING: Could not set 'Case Location': {exc}", flush=True)
        time.sleep(0.5)

        # ── Click "Select" button to open Location modal ───────────────────
        try:
            page.click("button:has-text('Select')", timeout=5000)
            print("[probate_ga] Clicked 'Select' button.", flush=True)
        except Exception as exc:
            print(f"[probate_ga] ERROR: Could not click Select: {exc}", flush=True)
            return []
        time.sleep(REQUEST_DELAY)

        self._screenshot(page, "/tmp/ga_location_modal.png")

        # ── In the Location modal: type "Probate" and click Search ─────────
        # The modal has: input "Search for Location" + "Search" button
        try:
            page.fill("input[placeholder*='Search for Location' i], input[placeholder*='location' i]", "Probate")
            print("[probate_ga] Typed 'Probate' in location search.", flush=True)
        except Exception:
            # Try any visible text input inside the modal
            try:
                modal_input = page.query_selector("dialog input[type='text'], [role='dialog'] input[type='text']")
                if modal_input:
                    modal_input.fill("Probate")
                    print("[probate_ga] Typed 'Probate' in modal input (fallback).", flush=True)
            except Exception as exc:
                print(f"[probate_ga] WARNING: Could not fill location search: {exc}", flush=True)

        # Click the "Search" button inside the modal
        try:
            # There are two Search buttons — the one inside the modal dialog
            page.click("dialog button:has-text('Search'), [role='dialog'] button:has-text('Search')", timeout=5000)
            print("[probate_ga] Clicked modal Search button.", flush=True)
        except Exception:
            # Fallback: click any visible button with text "Search" near the input
            try:
                buttons = page.query_selector_all("button:has-text('Search')")
                for btn in buttons:
                    if btn.is_visible():
                        btn.click()
                        print("[probate_ga] Clicked Search button (fallback).", flush=True)
                        break
            except Exception as exc:
                print(f"[probate_ga] WARNING: Could not click modal Search: {exc}", flush=True)
        time.sleep(REQUEST_DELAY)

        self._screenshot(page, "/tmp/ga_location_results.png")

        # ── Check all 7 target courts ──────────────────────────────────────
        # Courts appear as checkboxes in the "Selected Location" right panel
        # OR as items in the left search results list
        checked = 0
        for court_name in TARGET_COURTS:
            checked += self._check_court_in_modal(page, court_name)
        print(f"[probate_ga] Checked {checked}/{len(TARGET_COURTS)} courts.", flush=True)

        self._screenshot(page, "/tmp/ga_courts_checked.png")

        # ── Click "Done" button ────────────────────────────────────────────
        try:
            page.click("button:has-text('Done')", timeout=5000)
            print("[probate_ga] Clicked 'Done'.", flush=True)
        except Exception as exc:
            print(f"[probate_ga] WARNING: Could not click Done: {exc}", flush=True)
            page.keyboard.press("Escape")
        time.sleep(REQUEST_DELAY)

        self._screenshot(page, "/tmp/ga_after_done.png")

        # ── Click "Search" on the main Advanced Search form ────────────────
        # This is the Search button at the bottom right of the form
        try:
            # The main Search button (not inside the modal)
            page.click("button.search-button, button:has-text('Search')", timeout=5000)
            print("[probate_ga] Clicked main Search button.", flush=True)
        except Exception as exc:
            print(f"[probate_ga] ERROR: Could not click main Search: {exc}", flush=True)
            return []

        # Wait for results to load
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            time.sleep(4)

        self._screenshot(page, "/tmp/ga_results.png")
        print(f"[probate_ga] Results page: {page.url[:80]}", flush=True)

        return self._parse_results_page(page)

    # ── Check a court checkbox in the Location modal ──────────────────────────
    def _check_court_in_modal(self, page, court_name: str) -> int:
        """
        Find and check the checkbox for a given court name in the Location modal.
        The court names appear as labels with checkboxes in the right panel.
        Returns 1 if checked, 0 if not found.
        """
        try:
            # The checkboxes in the "Selected Location" right panel
            # Each item is: <input type="checkbox"> <label>COURT NAME</label>
            # or a <label> containing the checkbox
            
            # Strategy 1: find label by exact text (case-insensitive)
            court_upper = court_name.upper()
            
            # Look for any label or span containing the court name
            for el in page.query_selector_all("label, span, div[role='checkbox'], li"):
                try:
                    txt = el.inner_text().strip().upper()
                    if txt == court_upper or court_upper in txt:
                        # Find the associated checkbox
                        # Check if this element IS a checkbox container
                        cb = el.query_selector("input[type='checkbox']")
                        if cb:
                            if not cb.is_checked():
                                cb.check()
                            print(f"[probate_ga]   ✓ Checked: {court_name}", flush=True)
                            return 1
                        # Or click the element itself if it acts as a checkbox
                        el.click()
                        print(f"[probate_ga]   ✓ Clicked: {court_name}", flush=True)
                        return 1
                except Exception:
                    continue

            # Strategy 2: look for the court name as text and find nearby checkbox
            try:
                locator = page.get_by_text(court_name, exact=False)
                if locator.count() > 0:
                    el = locator.first
                    # Try to find a checkbox in the parent
                    parent = el.evaluate_handle("el => el.closest('label, li, div')")
                    if parent:
                        parent_el = parent.as_element()
                        if parent_el:
                            cb = parent_el.query_selector("input[type='checkbox']")
                            if cb:
                                if not cb.is_checked():
                                    cb.check()
                                print(f"[probate_ga]   ✓ Checked (locator): {court_name}", flush=True)
                                return 1
                            parent_el.click()
                            print(f"[probate_ga]   ✓ Clicked (locator): {court_name}", flush=True)
                            return 1
            except Exception:
                pass

            print(f"[probate_ga]   ✗ NOT FOUND: {court_name}", flush=True)
        except Exception as exc:
            print(f"[probate_ga]   ERROR checking {court_name}: {exc}", flush=True)
        return 0

    # ── Parse results page ────────────────────────────────────────────────────
    def _parse_results_page(self, page) -> list[dict]:
        """
        Parse case cards from the search results page.
        
        Each card structure (from screenshots):
          <case-name-link>GARRISON, MADREENE PALMALEE</case-name-link>
          26-E-000861
          Location: Gwinnett County - Probate Court
          Case Type: Estate*
          Parties: GARRISON, KAVIN, GARRISON, MADREENE PALMALEE, EMERSON, KECIA, and 2 more
          Attorneys: ...
          Judge: Christopher Ballar
          Case Filed Date: 5/29/2026
        """
        cases: list[dict] = []
        
        # Save HTML for debugging
        try:
            html = page.content()
            with open("/tmp/ga_results.html", "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            pass

        # Use Playwright to find case links directly — most reliable approach
        # Case detail URLs are: /CourtRecordsSearch/ui/case/{uuid}
        try:
            page.wait_for_selector(
                "a[href*='/case/']",
                timeout=10000,
            )
        except Exception:
            print("[probate_ga] No case links found on results page.", flush=True)
            return cases

        # Get all case links
        case_links = page.query_selector_all("a[href*='/CourtRecordsSearch/ui/case/']")
        print(f"[probate_ga] Found {len(case_links)} case links.", flush=True)

        seen_urls: set[str] = set()

        for link in case_links:
            try:
                href = link.get_attribute("href") or ""
                if not href or href in seen_urls:
                    continue
                seen_urls.add(href)

                case_url = href if href.startswith("http") else BASE_URL + href

                # Get the case name from the link text
                case_name = link.inner_text().strip()

                # Get the parent card element to extract other fields
                card_text = ""
                try:
                    # Walk up to the card container
                    card = link.evaluate_handle(
                        "el => el.closest('[class*=\"case\"], [class*=\"result\"], article, section, li, .card') || el.parentElement.parentElement"
                    )
                    if card:
                        card_el = card.as_element()
                        if card_el:
                            card_text = card_el.inner_text()
                except Exception:
                    pass

                # Extract case number — patterns: 26-E-000861, PRO23575, 26PC-E151
                case_number = ""
                m = re.search(
                    r"\b(\d{2,4}[-–]?(?:E|PC|ES|P|PR|ADM|PROB)[-–]?\w+|\w+\d{4,})\b",
                    card_text or case_name,
                    re.I,
                )
                if m:
                    case_number = m.group(1)

                # Extract filed date — MM/DD/YYYY
                filed_date = ""
                m2 = re.search(r"\b(\d{1,2}/\d{1,2}/\d{4})\b", card_text)
                if m2:
                    filed_date = m2.group(1)

                # Date filter
                fd = self._parse_date(filed_date)
                if fd and fd < self._cutoff:
                    continue

                # Extract court/location
                court = ""
                county = ""
                for tc in TARGET_COURTS:
                    if tc.upper() in card_text.upper():
                        court = tc
                        county = COURT_TO_COUNTY.get(tc, "")
                        break
                if not court:
                    # Try to find "Location\n{court name}" pattern
                    m3 = re.search(r"Location\s*\n?\s*([^\n]+(?:Probate|Court)[^\n]*)", card_text, re.I)
                    if m3:
                        court = m3.group(1).strip()
                        for tc, c in COURT_TO_COUNTY.items():
                            if tc.upper() in court.upper():
                                county = c
                                break

                # Extract parties
                parties = []
                m4 = re.search(r"Parties\s*\n?\s*([^\n]+)", card_text, re.I)
                if m4:
                    raw_parties = m4.group(1)
                    # Remove "and N more" suffix
                    raw_parties = re.sub(r",?\s*and \d+ more.*$", "", raw_parties, flags=re.I)
                    parties = [p.strip() for p in raw_parties.split(",") if p.strip()]

                case = {
                    "case_name":   case_name,
                    "case_number": case_number,
                    "filed_date":  filed_date,
                    "court":       court,
                    "county":      county,
                    "parties":     parties,
                    "case_type":   "Probate",
                    "state":       "GA",
                    "case_url":    case_url,
                }
                cases.append(case)
                print(
                    f"[probate_ga]   CASE: {case_number} | {case_name} | "
                    f"{filed_date} | {court or 'court?'}",
                    flush=True,
                )

            except Exception as exc:
                print(f"[probate_ga]   WARNING (link parse): {exc}", flush=True)
                continue

        return cases

    # ── Step 3: Process one case ──────────────────────────────────────────────
    def _process_case(self, page, case: dict) -> int:
        """
        Navigate to case detail page, extract Parties table,
        identify decedent and petitioner, save to DB.
        """
        case_url = case.get("case_url", "")
        if not case_url:
            print("[probate_ga]   No case URL — skipping.", flush=True)
            return 0

        try:
            page.goto(case_url, wait_until="domcontentloaded", timeout=30000)
        except PWTimeout:
            print("[probate_ga]   Timeout on case detail.", flush=True)
            return 0
        time.sleep(REQUEST_DELAY)

        safe_num = re.sub(r"[^a-zA-Z0-9_-]", "_", case.get("case_number", "unknown"))
        self._screenshot(page, f"/tmp/ga_case_{safe_num}.png")

        # ── Extract data from the case detail page ─────────────────────────
        try:
            html = page.content()
            soup = BeautifulSoup(html, "html.parser")
        except Exception as exc:
            print(f"[probate_ga]   ERROR reading case page: {exc}", flush=True)
            return 0

        # ── Enrich case metadata ───────────────────────────────────────────
        # Case number from page heading (more reliable than results card)
        if not case.get("case_number"):
            h = soup.find(string=re.compile(r"\b\d{2,4}[-–]?[A-Z][-–]?\d+\b"))
            if h:
                m = re.search(r"\b(\d{2,4}[-–]?[A-Z][-–]?\d+)\b", h)
                if m:
                    case["case_number"] = m.group(1)

        # Filed date
        if not case.get("filed_date"):
            m2 = re.search(r"\b(\d{1,2}/\d{1,2}/\d{4})\b", html)
            if m2:
                case["filed_date"] = m2.group(1)

        # Court / county
        if not case.get("court"):
            for tc in TARGET_COURTS:
                if tc.upper() in html.upper():
                    case["court"] = tc
                    case["county"] = COURT_TO_COUNTY.get(tc, "")
                    break

        # ── Parse Parties table ────────────────────────────────────────────
        # Structure from screenshots:
        #   <table> or similar with columns: Type | Name | Nickname/Alias | Attorneys
        #   Types: Applicant, Decedent, Deceased, Petitioner, HEIR, etc.
        parties_data = self._parse_parties_table(soup, page)
        
        decedent_name  = parties_data.get("decedent_name", "")
        petitioner_name = parties_data.get("petitioner_name", "")
        heirs           = parties_data.get("heirs", [])

        print(f"[probate_ga]   Decedent:   {decedent_name or '(not found)'}", flush=True)
        print(f"[probate_ga]   Petitioner: {petitioner_name or '(not found)'}", flush=True)
        print(f"[probate_ga]   Heirs:      {len(heirs)}", flush=True)

        if not decedent_name and not petitioner_name:
            print("[probate_ga]   No party data found — saving with case data only.", flush=True)

        # Build doc_data dict (no GPT-4o needed — all data is on the page)
        doc_data = {
            "decedent_name":           decedent_name,
            "decedent_address":        None,  # Not available on re:SearchGA
            "date_of_death":           None,  # Not available without paid document
            "petitioner_name":         petitioner_name,
            "petitioner_address":      None,
            "petitioner_phone":        None,
            "petitioner_relationship": "Applicant/Petitioner",
            "heirs":                   [{"name": h, "relationship": "Heir"} for h in heirs],
        }

        return self._save_lead(case, doc_data)

    # ── Parse Parties table ───────────────────────────────────────────────────
    def _parse_parties_table(self, soup: BeautifulSoup, page) -> dict:
        """
        Extract decedent name, petitioner name, and heirs from the Parties table.
        
        Table structure (from screenshots):
          Type        | Name                    | Nickname/Alias | Attorneys
          ------------|-------------------------|----------------|----------
          Applicant   | GARRISON, KAVIN         |                |
          Decedent    | GARRISON, MADREENE...   |                |
          HEIR        | EMERSON, KECIA          |                |
          HEIR        | GARRISON, BRIAN         |                |
          Petitioner  | Aldridge, Mary Ann      |                | PATRICK, STACY L
          Deceased    | ADAMS, HELEN T.         |                |
        """
        result = {
            "decedent_name": "",
            "petitioner_name": "",
            "heirs": [],
        }

        # Strategy 1: Parse the Parties table from HTML
        # Look for a table with "Type" and "Name" columns
        parties_table = None
        
        # Find the section with "Parties" heading
        for heading in soup.find_all(["h2", "h3", "h4", "div", "span"]):
            if heading.get_text(strip=True).lower().startswith("parties"):
                # Find the next table
                tbl = heading.find_next("table")
                if tbl:
                    parties_table = tbl
                    break

        if not parties_table:
            # Try any table on the page
            for tbl in soup.find_all("table"):
                headers = [th.get_text(strip=True).lower() for th in tbl.find_all("th")]
                if "type" in headers and "name" in headers:
                    parties_table = tbl
                    break

        if parties_table:
            rows = parties_table.find_all("tr")
            # Find column indices
            header_row = rows[0] if rows else None
            type_idx = 0
            name_idx = 1
            if header_row:
                headers = [th.get_text(strip=True).lower() for th in header_row.find_all(["th", "td"])]
                if "type" in headers:
                    type_idx = headers.index("type")
                if "name" in headers:
                    name_idx = headers.index("name")

            for row in rows[1:]:
                cells = row.find_all(["td", "th"])
                if len(cells) <= max(type_idx, name_idx):
                    continue
                party_type = cells[type_idx].get_text(strip=True).lower()
                party_name = cells[name_idx].get_text(strip=True)
                # Remove dropdown arrow characters
                party_name = re.sub(r"[▼▲►◄→←↓↑]", "", party_name).strip()

                if not party_name:
                    continue

                if party_type in DECEDENT_TYPES:
                    result["decedent_name"] = party_name
                elif party_type in PETITIONER_TYPES:
                    if not result["petitioner_name"]:
                        result["petitioner_name"] = party_name
                elif "heir" in party_type:
                    result["heirs"].append(party_name)

        # Strategy 2: Use Playwright to read the rendered table directly
        if not result["decedent_name"] and not result["petitioner_name"]:
            try:
                rows = page.query_selector_all("table tr")
                for row in rows:
                    cells = row.query_selector_all("td, th")
                    if len(cells) >= 2:
                        party_type = cells[0].inner_text().strip().lower()
                        party_name = cells[1].inner_text().strip()
                        party_name = re.sub(r"[▼▲►◄→←↓↑]", "", party_name).strip()
                        if not party_name or party_type == "type":
                            continue
                        if party_type in DECEDENT_TYPES:
                            result["decedent_name"] = party_name
                        elif party_type in PETITIONER_TYPES:
                            if not result["petitioner_name"]:
                                result["petitioner_name"] = party_name
                        elif "heir" in party_type:
                            result["heirs"].append(party_name)
            except Exception as exc:
                print(f"[probate_ga]   WARNING (Playwright table parse): {exc}", flush=True)

        return result

    # ── Save lead ─────────────────────────────────────────────────────────────
    def _save_lead(self, case: dict, doc_data: dict) -> int:
        if not _DB_AVAILABLE:
            print("[probate_ga]   DB not available — would save:", flush=True)
            print(f"[probate_ga]     Decedent:   {doc_data.get('decedent_name')}", flush=True)
            print(f"[probate_ga]     Petitioner: {doc_data.get('petitioner_name')}", flush=True)
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
                        print(f"[probate_ga]   DUPLICATE: {case_num} — skipping.", flush=True)
                        return 0

                county = case.get("county", "")
                if not county:
                    for court, c in COURT_TO_COUNTY.items():
                        if court.upper() in case.get("court", "").upper():
                            county = c
                            break

                # Build address from decedent name + county
                # (street address not available on re:SearchGA without paid document)
                decedent_name = doc_data.get("decedent_name") or case.get("case_name", "Unknown")
                address = f"{decedent_name} — {county} County, GA"

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
                    "decedent_address":        doc_data.get("decedent_address"),
                    "date_of_death":           doc_data.get("date_of_death"),
                    "petitioner_name":         doc_data.get("petitioner_name"),
                    "petitioner_address":      doc_data.get("petitioner_address"),
                    "petitioner_phone":        doc_data.get("petitioner_phone"),
                    "petitioner_relationship": doc_data.get("petitioner_relationship"),
                    "heirs":                   doc_data.get("heirs", []),
                    "parties":                 case.get("parties", []),
                }

                lead = Lead(
                    address=address,
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

                petitioner_name = doc_data.get("petitioner_name")
                if petitioner_name:
                    db.add(Contact(
                        lead_id=lead.id,
                        owner_name=petitioner_name,
                        phone=doc_data.get("petitioner_phone"),
                        email=None,
                        source="probate",
                    ))
                    contacts_saved += 1
                    print(f"[probate_ga]   CONTACT (petitioner): {petitioner_name}", flush=True)

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
                        print(f"[probate_ga]   CONTACT (heir): {heir_name}", flush=True)

                db.commit()
                print(
                    f"[probate_ga]   SAVED: lead id={lead.id} | {address} | "
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

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _parse_date(self, raw: str) -> Optional[datetime.date]:
        if not raw:
            return None
        for fmt in ["%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"]:
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
        score = 55.0
        if doc_data.get("decedent_name"):
            score += 15
        if doc_data.get("petitioner_name"):
            score += 10
        if doc_data.get("petitioner_phone"):
            score += 10
        if doc_data.get("heirs"):
            score += 5
        if doc_data.get("date_of_death"):
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
