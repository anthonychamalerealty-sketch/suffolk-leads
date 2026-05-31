"""
backfill_probate_ny.py
----------------------
Historical backfill script for NY probate records (Suffolk and Nassau Counties).
Scrapes up to 2 years of probate data from WebSurrogates in 30-day blocks.
Uses GPT-4o vision to read petition PDFs and extracts real property values.
Enriches phone numbers from petition documents, falling back to TruePeopleSearch.
Saves progress to backfill_progress.json and emails final CSV to Anthony.
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime
import io
import json
import logging
import os
import re
import sys
import time
import traceback
from typing import Optional, List, Dict, Any

# Ensure project root is in sys.path
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Guarded imports
try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("[backfill] ERROR: requests and beautifulsoup4 are required.", flush=True)
    raise

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    print("[backfill] ERROR: playwright is required.", flush=True)
    _PLAYWRIGHT_AVAILABLE = False

# OpenAI API key (used via requests — no openai SDK needed)
_OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
_OPENAI_AVAILABLE = bool(_OPENAI_API_KEY)
if not _OPENAI_AVAILABLE:
    print("[backfill] WARNING: OPENAI_API_KEY not set — GPT-4o extraction disabled.", flush=True)

import sqlite3
DB_PATH = os.environ.get('DB_PATH', '/app/sql_app.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _ensure_db_tables():
    """Create leads and contacts tables if they don't exist yet."""
    try:
        conn = get_db()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT,
                parcel_id TEXT,
                source TEXT,
                raw_data TEXT,
                score REAL,
                created_at TEXT,
                status TEXT DEFAULT 'new',
                state TEXT DEFAULT 'NY',
                county TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER,
                owner_name TEXT,
                phone TEXT,
                email TEXT,
                source TEXT
            )
        """)
        conn.commit()
        conn.close()
        print(f"[backfill] SQLite DB ready at {DB_PATH}", flush=True)
        return True
    except Exception as exc:
        print(f"[backfill] DB setup warning: {exc}", flush=True)
        return False

_DB_AVAILABLE = _ensure_db_tables()

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [backfill] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Constants
BASE_URL = "https://websurrogates.nycourts.gov"
WELCOME_URL = f"{BASE_URL}/Home/Welcome/?ReturnUrl=%2FFile%2FFileSearch"
FILE_SEARCH = f"{BASE_URL}/File/FileSearch"
REQUEST_DELAY = 3
GPT_MODEL = "gpt-4o"
PROGRESS_FILE = "backfill_progress.json"

# GPT-4o prompts (identical to probate.py for consistency)
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

_PHONE_RE = re.compile(r"\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}")


# ── Helper Functions ──────────────────────────────────────────────────────────

def _extract_phones_from_html(html: str) -> list[str]:
    """Return a deduplicated list of US phone numbers found in html."""
    raw = _PHONE_RE.findall(html)
    seen = set()
    phones = []
    for p in raw:
        digits = re.sub(r"\D", "", p)
        if len(digits) == 10 and digits not in seen:
            seen.add(digits)
            phones.append(f"({digits[:3]}) {digits[3:6]}-{digits[6:]}")
    return phones


def scrape_truepeoplesearch_sync(name: str, address: str) -> list[str]:
    """Search TruePeopleSearch.com synchronously for a phone number."""
    import urllib.parse
    import random
    
    if not _PLAYWRIGHT_AVAILABLE:
        print("[enrich] Playwright not available for TruePeopleSearch fallback", flush=True)
        return []

    name_parts = name.strip().split()
    first_name = name_parts[0] if name_parts else ""
    last_name = name_parts[-1] if len(name_parts) > 1 else ""
    
    addr_clean = address.strip()
    parts = [p.strip() for p in addr_clean.split(",")]
    if len(parts) >= 3:
        city_state_zip = f"{parts[-2]} {parts[-1]}"
    elif len(parts) == 2:
        city_state_zip = parts[-1]
    else:
        tokens = addr_clean.split()
        city_state_zip = " ".join(tokens[-3:]) if len(tokens) >= 3 else addr_clean
        
    search_url = (
        "https://www.truepeoplesearch.com/results"
        f"?name={urllib.parse.quote(f'{first_name} {last_name}')}"
        f"&citystatezip={urllib.parse.quote(city_state_zip)}"
    )
    print(f"[enrich] TruePeopleSearch URL: {search_url}", flush=True)
    
    phones = []
    proxy_list = get_proxy_list()
    proxies_to_try = proxy_list if proxy_list else [None]
    for _proxy_idx, _proxy_cfg in enumerate(proxies_to_try):
        _proxy_label = _proxy_cfg['server'] if _proxy_cfg else 'direct'
        print(f"[enrich] TruePeopleSearch attempt {_proxy_idx + 1}/{len(proxies_to_try)} via {_proxy_label}", flush=True)
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, proxy=_proxy_cfg)
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    viewport={"width": random.randint(1280, 1920), "height": random.randint(768, 1080)},
                    locale="en-US",
                    timezone_id="America/New_York",
                    ignore_https_errors=True,
                )
                context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                page = context.new_page()
            
                try:
                    page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(random.uniform(2.0, 4.0))
                    content = page.content()

                    if any(sig in content.lower() for sig in ["captcha", "recaptcha", "cloudflare", "just a moment", "verify you are human"]):
                        print(f"[enrich] CAPTCHA/block detected via {_proxy_label} — trying next proxy", flush=True)
                        browser.close()
                        continue

                    # Try to click the first profile
                    try:
                        profile_link = page.query_selector("a[href*='/find/person/'], a[href*='/results/'], .card-summary a")
                        if profile_link:
                            href = profile_link.get_attribute("href")
                            if href and not href.startswith("http"):
                                href = "https://www.truepeoplesearch.com" + href
                            print(f"[enrich] TruePeopleSearch: clicking profile {href}", flush=True)
                            page.goto(href, wait_until="domcontentloaded", timeout=30000)
                            time.sleep(random.uniform(3.0, 5.0))
                            content = page.content()
                    except Exception as _link_err:
                        print(f"[enrich] TruePeopleSearch profile click failed: {_link_err}", flush=True)

                    # Extract phones
                    phones = _extract_phones_from_html(content)
                    print(f"[enrich] TruePeopleSearch: {len(phones)} phone(s) found for '{name}'", flush=True)

                except Exception as exc:
                    print(f"[enrich] TruePeopleSearch page error via {_proxy_label}: {exc}", flush=True)
                finally:
                    browser.close()

                break  # success — stop trying more proxies

        except Exception as exc:
            print(f"[enrich] Playwright launch error via {_proxy_label}: {exc}", flush=True)

    return phones


def _build_addr(data: dict, prefix: str) -> str:
    parts = [
        data.get(f"{prefix}_street", ""),
        data.get(f"{prefix}_city_town", ""),
        data.get(f"{prefix}_state", ""),
        data.get(f"{prefix}_zip", ""),
    ]
    return ", ".join(p.strip() for p in parts if p and p.strip())


def _score(pdf_data: dict) -> float:
    score = 60.0
    val = pdf_data.get("real_property_value_dollars")
    if val:
        if val >= 500000:   score += 30
        elif val >= 200000: score += 20
        elif val >= 100000: score += 10
    if pdf_data.get("petitioner_phone"):  score += 5
    if pdf_data.get("decedent_street"):   score += 5
    return min(score, 100.0)


# ── Progress Management ───────────────────────────────────────────────────────

def load_progress() -> dict:
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r") as f:
                return json.load(f)
        except Exception as exc:
            print(f"[backfill] Warning: failed to load progress file: {exc}", flush=True)
    return {
        "completed_searches": [],
        "processed_files": {},
        "saved_leads": []
    }


def save_progress(progress: dict):
    try:
        with open(PROGRESS_FILE, "w") as f:
            json.dump(progress, f, indent=2)
    except Exception as exc:
        print(f"[backfill] Error saving progress: {exc}", flush=True)


# ── Email Delivery ────────────────────────────────────────────────────────────

def send_email_with_sendgrid(
    to_email: str,
    subject: str,
    html_content: str,
    attachment_path: str = None,
    attachment_filename: str = None,
) -> bool:
    api_key = os.environ.get("SENDGRID_API_KEY", "")
    if not api_key:
        print("[EMAIL] SENDGRID_API_KEY is not set — cannot send email.", flush=True)
        return False

    print(f"[EMAIL] Preparing email to: {to_email}", flush=True)
    try:
        import base64
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import (
            Mail, Attachment, FileContent, FileName, FileType, Disposition,
        )
    except ImportError as exc:
        print(f"[EMAIL] sendgrid package not installed: {exc}", flush=True)
        return False

    try:
        message = Mail(
            from_email="Anthonychamalerealty@gmail.com",
            to_emails=to_email,
            subject=subject,
            html_content=html_content,
        )
    except Exception as exc:
        print(f"[EMAIL] Failed to create Mail object: {exc}", flush=True)
        return False

    if attachment_path and attachment_filename:
        if not os.path.isfile(attachment_path):
            print(f"[EMAIL] Attachment file not found: {attachment_path}", flush=True)
        else:
            try:
                with open(attachment_path, "rb") as f:
                    encoded = base64.b64encode(f.read()).decode()
                message.attachment = Attachment(
                    FileContent(encoded),
                    FileName(attachment_filename),
                    FileType("text/csv"),
                    Disposition("attachment"),
                )
                print(f"[EMAIL] Attached: {attachment_filename}", flush=True)
            except Exception as exc:
                print(f"[EMAIL] Failed to attach file: {exc}", flush=True)

    try:
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        print(f"[EMAIL] SendGrid response status: {response.status_code}", flush=True)
        return response.status_code in (200, 202)
    except Exception as exc:
        print(f"[EMAIL] SendGrid API error: {exc}", flush=True)
        return False



def get_proxy_list():
    """Return a list of proxy config dicts from PROXY_LIST env var.

    PROXY_LIST format: comma-separated entries of IP:PORT:USER:PASS
    e.g. "1.2.3.4:8080:user1:pass1,5.6.7.8:3128:user2:pass2"

    Falls back to single PROXY_HOST/PROXY_PORT/PROXY_USERNAME/PROXY_PASSWORD
    env vars if PROXY_LIST is not set.  Returns an empty list if no proxy
    is configured (browser will connect directly).
    """
    raw = os.environ.get('PROXY_LIST', '').strip()
    proxies = []
    if raw:
        for entry in raw.split(','):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split(':')
            if len(parts) >= 2:
                host, port = parts[0], parts[1]
                user = parts[2] if len(parts) > 2 else None
                pwd  = parts[3] if len(parts) > 3 else None
                cfg = {'server': f'http://{host}:{port}'}
                if user:
                    cfg['username'] = user
                if pwd:
                    cfg['password'] = pwd
                proxies.append(cfg)
    if not proxies:
        # Fall back to single-proxy env vars
        host = os.environ.get('PROXY_HOST')
        port = os.environ.get('PROXY_PORT')
        if host and port:
            cfg = {'server': f'http://{host}:{port}'}
            user = os.environ.get('PROXY_USERNAME')
            pwd  = os.environ.get('PROXY_PASSWORD')
            if user:
                cfg['username'] = user
            if pwd:
                cfg['password'] = pwd
            proxies.append(cfg)
    return proxies


def sanitize_cookies(cookies):
    valid_samesite = {'Strict', 'Lax', 'None'}
    cleaned = []
    for cookie in cookies:
        c = {
            'name': cookie.get('name', ''),
            'value': cookie.get('value', ''),
            'domain': cookie.get('domain', ''),
            'path': cookie.get('path', '/'),
            'secure': cookie.get('secure', False),
            'httpOnly': cookie.get('httpOnly', False),
        }
        samesite = cookie.get('sameSite', 'Lax')
        samesite_map = {
            'no_restriction': 'None',
            'unspecified': 'Lax',
            'strict': 'Strict',
            'lax': 'Lax',
            'none': 'None',
        }
        c['sameSite'] = samesite_map.get(samesite.lower(), 'Lax') if samesite else 'Lax'
        if cookie.get('expirationDate'):
            c['expires'] = int(cookie['expirationDate'])
        cleaned.append(c)
    return cleaned



# ── Scraper Class ─────────────────────────────────────────────────────────────

class NYBackfillScraper:
    def __init__(self, cookies_json: str, limit: Optional[int] = None):
        self.cookies_json = cookies_json
        self.limit = limit
        self._session_cookies: list[dict] = []
        self._blocked = False
        self.processed_this_run = 0
        self._parse_cookies()

    def _parse_cookies(self):
        if self.cookies_json:
            try:
                raw = json.loads(self.cookies_json)
                self._session_cookies = sanitize_cookies(raw)
                print(f"[backfill] Parsed {len(self._session_cookies)} cookies.", flush=True)
            except Exception as exc:
                print(f"[backfill] WARNING: Could not parse cookies: {exc}", flush=True)

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

    def _pass_welcome(self, page) -> bool:
        print("[backfill] Loading Welcome page...", flush=True)
        try:
            page.goto(WELCOME_URL, wait_until="domcontentloaded", timeout=30000)
        except PWTimeout:
            print("[backfill] ERROR: Timeout on Welcome page.", flush=True)
            return False
        time.sleep(REQUEST_DELAY)

        if self._is_blocked(page):
            print("[backfill] Blocked on Welcome page.", flush=True)
            return False

        btn = page.query_selector("button#StartSearchButton, input[value='Start Search'], button:has-text('Start Search')")
        if btn:
            btn.click()
            time.sleep(REQUEST_DELAY)
            if self._is_blocked(page):
                return False
        
        try:
            page.goto(FILE_SEARCH, wait_until="domcontentloaded", timeout=30000)
        except PWTimeout:
            print("[backfill] ERROR: Timeout on File Search page.", flush=True)
            return False
        time.sleep(REQUEST_DELAY)

        if self._is_blocked(page):
            return False

        return True

    def _search_filings(self, page, court_name: str, proceeding: str, date_from: str, date_to: str) -> list[dict]:
        print(f"[backfill] Searching: Court={court_name} | Proc={proceeding} | Range={date_from} → {date_to}", flush=True)
        try:
            page.goto(FILE_SEARCH, wait_until="domcontentloaded", timeout=30000)
        except PWTimeout:
            print(f"[backfill] Timeout navigating to File Search", flush=True)
            return []
        time.sleep(REQUEST_DELAY)

        if self._is_blocked(page):
            print("[backfill] Blocked on File Search page.", flush=True)
            return []

        # Select Court
        try:
            court_sel = page.query_selector("select#Court, select[name='Court']")
            if court_sel:
                opts = court_sel.query_selector_all("option")
                for opt in opts:
                    if court_name.lower() in opt.inner_text().lower():
                        court_sel.select_option(value=opt.get_attribute("value"))
                        print(f"[backfill] Selected Court: {opt.inner_text().strip()}", flush=True)
                        break
        except Exception as exc:
            print(f"[backfill] WARNING: Court selection failed: {exc}", flush=True)

        time.sleep(0.5)

        # Select Proceeding
        try:
            proc_sel = page.query_selector("select#FileProceeding, select[name='FileProceeding']")
            if proc_sel:
                opts = proc_sel.query_selector_all("option")
                for opt in opts:
                    if opt.inner_text().strip().upper() == proceeding.upper():
                        proc_sel.select_option(value=opt.get_attribute("value"))
                        print(f"[backfill] Selected Proceeding: {opt.inner_text().strip()}", flush=True)
                        break
        except Exception as exc:
            print(f"[backfill] WARNING: Proceeding selection failed: {exc}", flush=True)

        # Fill Dates
        try:
            from_inp = page.query_selector("input#FromDate, input[name='FromDate']")
            to_inp = page.query_selector("input#ToDate, input[name='ToDate']")
            if from_inp:
                from_inp.triple_click()
                from_inp.type(date_from)
            if to_inp:
                to_inp.triple_click()
                to_inp.type(date_to)
        except Exception as exc:
            print(f"[backfill] WARNING: Date entry failed: {exc}", flush=True)

        # Click Search
        try:
            btns = page.query_selector_all("input[value='Search'][type='submit'], button:has-text('Search')")
            btn = btns[-1] if len(btns) >= 2 else (btns[0] if btns else None)
            if btn:
                btn.click()
            else:
                print("[backfill] ERROR: Search button not found", flush=True)
                return []
        except Exception as exc:
            print(f"[backfill] ERROR clicking search: {exc}", flush=True)
            return []

        time.sleep(REQUEST_DELAY)
        if self._is_blocked(page):
            return []

        return self._parse_results_table(page, proceeding)

    def _parse_results_table(self, page, proceeding: str) -> list[dict]:
        filings = []
        try:
            html = page.content()
            soup = BeautifulSoup(html, "html.parser")

            result_table = None
            for tbl in soup.find_all("table"):
                ths = [th.get_text(strip=True).upper() for th in tbl.find_all("th")]
                if any("FILE" in h for h in ths):
                    result_table = tbl
                    break

            if not result_table:
                return []

            rows = result_table.find_all("tr")[1:]
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
        except Exception as exc:
            print(f"[backfill] ERROR parsing results table: {exc}", flush=True)
        return filings

    def _process_filing(self, page, filing: dict, county: str) -> Optional[dict]:
        file_number = filing["file_number"]
        print(f"[backfill] Processing filing: {file_number}", flush=True)

        try:
            page.goto(filing["history_url"], wait_until="domcontentloaded", timeout=30000)
        except PWTimeout:
            print(f"[backfill] Timeout on File History for {file_number}", flush=True)
            return None
        time.sleep(REQUEST_DELAY)

        if self._is_blocked(page):
            return None

        html = page.content()
        soup = BeautifulSoup(html, "html.parser")

        # Extract parties
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
            print(f"[backfill] Party extraction warning: {exc}", flush=True)

        # Find petition PDF link
        pdf_url = None
        try:
            for link in soup.find_all("a"):
                txt = link.get_text(strip=True).upper()
                if "ADMINISTRATION PETITION" in txt or "PROBATE PETITION" in txt:
                    href = link.get("href", "")
                    if href:
                        pdf_url = href if href.startswith("http") else BASE_URL + (href if href.startswith("/") else "/" + href)
                        break
        except Exception as exc:
            print(f"[backfill] PDF link search warning: {exc}", flush=True)

        if not pdf_url:
            print(f"[backfill] No petition document link found for {file_number}", flush=True)
            return None

        # Screenshot the document preview page and extract data with GPT-4o
        pdf_data = self._extract_document(page, pdf_url, file_number)
        if not pdf_data:
            print(f"[backfill] Document extraction failed for {file_number}", flush=True)
            return None

        # Section 3(b) Filter: skip cases where real property value is zero/NONE/blank
        prop_dollars = pdf_data.get("real_property_value_dollars", None)
        has_prop = pdf_data.get("has_real_property", None)
        raw_val = pdf_data.get("real_property_value_raw", "")

        if has_prop is False and (prop_dollars == 0 or prop_dollars is None):
            print(f"[backfill] SKIPPED: section 3(b) real property is zero/NONE for {file_number}", flush=True)
            return None

        # Phone Enrichment: document first, then TruePeopleSearch
        petitioner_name = pdf_data.get("petitioner_name")
        petitioner_address = _build_addr(pdf_data, "petitioner")
        phone = pdf_data.get("petitioner_phone")
        phone_source = ""

        if phone:
            phone_source = "petition_document"
            print(f"[backfill] Phone found in petition document: {phone}", flush=True)
        elif petitioner_name and petitioner_address:
            print(f"[backfill] No petition phone. Falling back to TruePeopleSearch for '{petitioner_name}'", flush=True)
            tps_phones = scrape_truepeoplesearch_sync(petitioner_name, petitioner_address)
            if tps_phones:
                phone = tps_phones[0]
                phone_source = "truepeoplesearch"
                print(f"[backfill] Phone found via TruePeopleSearch fallback: {phone}", flush=True)
            else:
                print(f"[backfill] TruePeopleSearch returned 0 phones", flush=True)

        # Build Lead Data
        decedent_addr = _build_addr(pdf_data, "decedent")
        if not decedent_addr:
            decedent_addr = f"{filing['decedent_name']} — {county} County, NY"

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
            "county":                   county,
            "place_of_death":           pdf_data.get("place_of_death"),
            "petitioner_name":          petitioner_name,
            "petitioner_address":       petitioner_address,
            "petitioner_phone":         phone,
            "petitioner_relationship":  pdf_data.get("petitioner_relationship"),
            "real_property_value":      prop_dollars,
            "real_property_value_raw":  raw_val,
            "parties":                  parties,
        }

        lead_dict = {
            "address": decedent_addr,
            "state": "NY",
            "county": county,
            "owner_name": petitioner_name or "Unknown",
            "phone": phone or "",
            "phone_source": phone_source,
            "email": "",
            "source": "probate",
            "score": _score(pdf_data),
            "created_at": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "new",
            "parcel_id": "",
            "raw_data": raw_data
        }

        # Save to Database (raw sqlite3)
        if _DB_AVAILABLE:
            try:
                conn = get_db()
                _fn = filing["file_number"]
                # Dedup check
                row = conn.execute(
                    "SELECT id FROM leads WHERE source='probate' AND raw_data LIKE ?",
                    (f"%{_fn}%",)
                ).fetchone()
                if row:
                    print(f"[backfill] DB Duplicate: {_fn} already exists (lead id={row['id']})", flush=True)
                else:
                    cur = conn.execute(
                        """
                        INSERT INTO leads (address, parcel_id, source, raw_data, score,
                                          created_at, status, state, county)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            decedent_addr, None, "probate", json.dumps(raw_data),
                            lead_dict["score"], lead_dict["created_at"], "new", "NY", county,
                        )
                    )
                    lead_id = cur.lastrowid
                    if petitioner_name:
                        conn.execute(
                            """
                            INSERT INTO contacts (lead_id, owner_name, phone, email, source)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (lead_id, petitioner_name, phone, None,
                             phone_source or "petition_document")
                        )
                    conn.commit()
                    print(f"[backfill] Saved to DB: lead id={lead_id}", flush=True)
                conn.close()
            except Exception as exc:
                print(f"[backfill] DB Save Error: {exc}", flush=True)
                traceback.print_exc()

        self.processed_this_run += 1
        return lead_dict

    def _screenshot_document_page(self, page, doc_url: str, label: str) -> Optional[str]:
        """Navigate to a document preview URL, take a full-page screenshot,
        and return it as a base64-encoded JPEG string."""
        try:
            page.goto(doc_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(REQUEST_DELAY)
            png_bytes = page.screenshot(full_page=True)
            b64 = base64.b64encode(png_bytes).decode()
            print(f"[backfill] Screenshot captured for {label} ({len(png_bytes)} bytes)", flush=True)
            return b64
        except Exception as exc:
            print(f"[backfill] Screenshot error for {label}: {exc}", flush=True)
            return None

    def _gpt4o_screenshot(self, b64_image: str, prompt: str, label: str) -> Optional[dict]:
        """Send a base64 screenshot to GPT-4o vision via the OpenAI REST API
        using the requests library (no openai SDK required)."""
        if not _OPENAI_AVAILABLE:
            print(f"[backfill] OpenAI not available for {label}", flush=True)
            return None
        try:
            payload = {
                "model": GPT_MODEL,
                "max_tokens": 1000,
                "temperature": 0,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/png;base64,{b64_image}",
                            "detail": "high",
                        }},
                    ],
                }],
            }
            headers = {
                "Authorization": f"Bearer {_OPENAI_API_KEY}",
                "Content-Type": "application/json",
            }
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=60,
            )
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw).strip()
            return json.loads(raw)
        except Exception as exc:
            print(f"[backfill] GPT-4o error for {label}: {exc}", flush=True)
            return None

    def _extract_document(self, page, doc_url: str, file_number: str) -> Optional[dict]:
        """Take a screenshot of the document preview page and send it to
        GPT-4o vision.  Returns the merged extraction dict or None."""
        if not _OPENAI_AVAILABLE:
            print(f"[backfill] GPT-4o extraction skipped — OPENAI_API_KEY not set", flush=True)
            return None

        b64 = self._screenshot_document_page(page, doc_url, file_number)
        if not b64:
            return None

        result = {}
        # Page 1 fields
        p1 = self._gpt4o_screenshot(b64, _PAGE1_PROMPT, f"{file_number} p1")
        if p1:
            result.update(p1)

        # Page 2 fields (same screenshot — full-page capture includes both pages)
        p2 = self._gpt4o_screenshot(b64, _PAGE2_PROMPT, f"{file_number} p2")
        if p2:
            result.update(p2)

        if not result:
            result["has_real_property"] = True
            result["real_property_value_raw"] = "unknown (screenshot extraction failed)"
            result["real_property_value_dollars"] = None

        return result or None


# ── CSV Export Helper ────────────────────────────────────────────────────────

def generate_csv(leads, filepath):
    import csv
    try:
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            if not leads:
                return False
            writer = csv.DictWriter(f, fieldnames=leads[0].keys())
            writer.writeheader()
            writer.writerows(leads)
        return True
    except Exception as e:
        print(f'CSV error: {e}', flush=True)
        return False


# ── Main Backfill Orchestration ───────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="NY Probate Historical Backfill Job")
    parser.add_argument("--months", type=int, default=24, help="Months back to backfill (default: 24)")
    parser.add_argument("--counties", default="Suffolk,Nassau", help="Counties to process (comma-separated)")
    parser.add_argument("--proceedings", default="ADMINISTRATION PETITION,PROBATE PETITION", help="Proceedings to process (comma-separated)")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of leads to save this run")
    parser.add_argument("--email", default="Anthonychamalerealty@gmail.com", help="Recipient email for final CSV")
    args = parser.parse_args()

    print("[backfill] ============================================================", flush=True)
    print("[backfill] STARTING NY PROBATE HISTORICAL BACKFILL", flush=True)
    print(f"[backfill] Target: {args.months} months | Counties: {args.counties} | Proceedings: {args.proceedings}", flush=True)
    print("[backfill] ============================================================", flush=True)

    cookies_json = os.getenv("WEBSURROGATES_COOKIES_JSON", "")
    if not cookies_json:
        print("[backfill] ERROR: WEBSURROGATES_COOKIES_JSON is not set! Cloudflare bypass will fail.", flush=True)
        sys.exit(1)
    print("[backfill] REMINDER: WebSurrogates cookies expire daily. "
          "Please refresh WEBSURROGATES_COOKIES_JSON in Railway variables daily.", flush=True)

    # Load progress
    progress = load_progress()
    completed_searches = progress.setdefault("completed_searches", [])
    processed_files = progress.setdefault("processed_files", {})
    saved_leads = progress.setdefault("saved_leads", [])

    print(f"[backfill] Loaded progress: {len(completed_searches)} searches completed, {len(processed_files)} files processed, {len(saved_leads)} leads saved.", flush=True)

    # Generate 30-day blocks
    today = datetime.date.today()
    blocks = []
    curr_end = today
    for _ in range(args.months):
        curr_start = curr_end - datetime.timedelta(days=29)
        blocks.append((curr_start, curr_end))
        curr_end = curr_start - datetime.timedelta(days=1)

    # Reverse to process chronologically (oldest first)
    blocks.reverse()

    target_counties = [c.strip() for c in args.counties.split(",")]
    target_proceedings = [p.strip() for p in args.proceedings.split(",")]

    scraper = NYBackfillScraper(cookies_json=cookies_json, limit=args.limit)

    # Run Playwright search loop
    proxy_list = get_proxy_list()
    # Always append None as a last-resort direct (no-proxy) attempt
    proxies_to_try = proxy_list + [None] if proxy_list else [None]
    print(f"[backfill] Proxy pool: {len(proxy_list)} proxy(ies) + 1 direct fallback.", flush=True)

    browser = None
    page = None
    _pw_instance = None

    for _proxy_idx, _proxy_cfg in enumerate(proxies_to_try):
        _proxy_label = _proxy_cfg['server'] if _proxy_cfg else 'direct (no proxy)'
        print(f"[backfill] Attempt {_proxy_idx + 1}/{len(proxies_to_try)}: {_proxy_label}", flush=True)
        # Wait 10 seconds between attempts to avoid hammering the site
        if _proxy_idx > 0:
            print(f"[backfill] Waiting 10s before next attempt...", flush=True)
            time.sleep(10)
        try:
            _pw_instance = sync_playwright().start()
            browser = _pw_instance.chromium.launch(
                headless=True,
                proxy=_proxy_cfg,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"],
            )
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 900},
                locale="en-US",
                timezone_id="America/New_York",
                ignore_https_errors=True,
            )
            ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
            if scraper._session_cookies:
                ctx.add_cookies(scraper._session_cookies)
            page = ctx.new_page()
            # websurrogates.nycourts.gov is a public site — no login needed.
            # Navigate directly to File Search; cookies handle Cloudflare bypass.
            # Use a 60s timeout to avoid prematurely flagging slow proxies as blocked.
            print(f"[backfill] Navigating directly to File Search via {_proxy_label}", flush=True)
            page.goto(FILE_SEARCH, wait_until="domcontentloaded", timeout=60000)
            time.sleep(REQUEST_DELAY)
            if scraper._is_blocked(page):
                print(f"[backfill] Blocked via {_proxy_label} — trying next.", flush=True)
                browser.close()
                _pw_instance.stop()
                browser = None
                page = None
                continue
            print(f"[backfill] Connected successfully via {_proxy_label}.", flush=True)
            break  # good connection — proceed with scrape
        except Exception as _proxy_err:
            print(f"[backfill] {_proxy_label} failed: {_proxy_err}", flush=True)
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            if _pw_instance:
                try:
                    _pw_instance.stop()
                except Exception:
                    pass
            browser = None
            page = None

    if browser is None or page is None:
        print("[backfill] All proxies and direct connection failed or are blocked. Exiting.", flush=True)
        sys.exit(1)

    try:
        # Loop over search space
        for county in target_counties:
            for proceeding in target_proceedings:
                for idx, (start_date, end_date) in enumerate(blocks, 1):

                    # Limit check
                    if args.limit and scraper.processed_this_run >= args.limit:
                        print(f"[backfill] Hit runtime limit of {args.limit} leads. Stopping.", flush=True)
                        break

                    start_str = start_date.strftime("%m/%d/%Y")
                    end_str = end_date.strftime("%m/%d/%Y")

                    search_key = f"{county}|{proceeding}|{start_str}|{end_str}"
                    if search_key in completed_searches:
                        print(f"[backfill] Skipping completed search block: {search_key}", flush=True)
                        continue

                    print(f"\n[backfill] Running Block {idx}/{len(blocks)}: {search_key}", flush=True)

                    filings = scraper._search_filings(page, county, proceeding, start_str, end_str)
                    print(f"[backfill] Found {len(filings)} filing(s) in this block.", flush=True)

                    for filing in filings:
                        # Limit check
                        if args.limit and scraper.processed_this_run >= args.limit:
                            break

                        file_num = filing["file_number"]
                        if file_num in processed_files:
                            print(f"[backfill] Filing {file_num} already processed. Skipping.", flush=True)
                            continue

                        try:
                            lead_dict = scraper._process_filing(page, filing, county)
                            if lead_dict:
                                saved_leads.append(lead_dict)
                                processed_files[file_num] = {"status": "saved", "decedent": filing["decedent_name"]}
                                print(f"[backfill] Lead saved successfully for {file_num}", flush=True)
                            else:
                                processed_files[file_num] = {"status": "skipped_or_failed"}
                        except Exception as exc:
                            print(f"[backfill] Error processing filing {file_num}: {exc}", flush=True)
                            traceback.print_exc()
                            processed_files[file_num] = {"status": "error", "error": str(exc)}

                        # Save progress every 10 processed filings (saved/skipped/error)
                        if len(processed_files) % 10 == 0:
                            print("[backfill] Saving incremental progress...", flush=True)
                            save_progress(progress)

                    # Mark block completed
                    completed_searches.append(search_key)
                    save_progress(progress)

        browser.close()
        if _pw_instance:
            _pw_instance.stop()

    except Exception as exc:
        print(f"[backfill] FATAL CRASH: {exc}", flush=True)
        traceback.print_exc()

    # Final Progress Save
    save_progress(progress)

    # Export to CSV and Email
    print(f"\n[backfill] Backfill complete. Exporting {len(saved_leads)} lead(s) to CSV...", flush=True)
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    csv_filename = f"backfill-ny-probate-{date_str}.csv"
    csv_path = os.path.join(os.getcwd(), csv_filename)
    
    if generate_csv(saved_leads, csv_path):
        print(f"[backfill] CSV successfully written to {csv_path}", flush=True)
        
        # Build Email
        subject = f"Historical NY Probate Backfill Complete — {len(saved_leads)} Leads Captured"
        html_body = f"""<!DOCTYPE html>
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333; padding: 20px;">
            <h2 style="color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 10px;">NY Probate Historical Backfill Job</h2>
            <p>Hello Anthony,</p>
            <p>The historical backfill job for <strong>Suffolk County</strong> and <strong>Nassau County</strong> Surrogate's Courts has completed successfully.</p>
            <p><strong>Job Details:</strong></p>
            <ul>
                <li><strong>Date Range:</strong> Last 24 months (consecutive 30-day blocks)</li>
                <li><strong>Proceeding Types:</strong> ADMINISTRATION PETITION, PROBATE PETITION</li>
                <li><strong>Total Leads Saved:</strong> {len(saved_leads)}</li>
                <li><strong>Filter Applied:</strong> Skipped cases where section 3b real property value is $0 or NONE</li>
                <li><strong>Enrichment:</strong> Tier 1 (Petition PDF phone) with Tier 2 fallback (TruePeopleSearch)</li>
            </ul>
            <p>All leads have been saved to the database and will appear on your Next.js dashboard. The full historical export is attached to this email.</p>
            <hr style="border: none; border-top: 1px solid #eee; margin-top: 20px;">
            <p style="font-size: 12px; color: #7f8c8d;">Automatically generated by the Suffolk Leads pipeline.</p>
        </body>
        </html>"""

        print(f"[backfill] Sending final email to {args.email}...", flush=True)
        email_sent = send_email_with_sendgrid(
            to_email=args.email,
            subject=subject,
            html_content=html_body,
            attachment_path=csv_path,
            attachment_filename=csv_filename
        )
        if email_sent:
            print("[backfill] Final email sent successfully.", flush=True)
        else:
            print("[backfill] ERROR: Final email failed to send.", flush=True)
            
        # Clean up temporary CSV file
        try:
            if os.path.exists(csv_path):
                os.remove(csv_path)
                print("[backfill] Cleaned up temporary CSV file.", flush=True)
        except Exception as exc:
            print(f"[backfill] Warning: failed to delete temporary CSV: {exc}", flush=True)
    else:
        print("[backfill] ERROR: Failed to generate CSV. Email not sent.", flush=True)

    print("[backfill] JOB FINISHED.", flush=True)


if __name__ == "__main__":
    main()
