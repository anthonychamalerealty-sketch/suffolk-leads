#!/usr/bin/env python3
"""
suffolk_backfill.py
-------------------
Bypasses Cloudflare on websurrogates.nycourts.gov and truepeoplesearch.com
using the curl_cffi library (impersonating Chrome 120).

No Playwright, no browser windows, no proxy required. Runs fast and lightweight.

Required Environment Variables:
  WEBSURROGATES_COOKIES_JSON  — JSON string of exported WebSurrogate cookies
  OPENAI_API_KEY              — OpenAI API key for GPT-4o vision document extraction
  SENDGRID_API_KEY            — SendGrid API key for emailing results

Output:
  - SQLite database: /app/sql_app.db (or DB_PATH env)
  - Desktop export:  ~/Desktop/backfill_results.csv
  - Progress file:   backfill_progress.json (resumes if interrupted)
  - Final CSV sent to Anthonychamalerealty@gmail.com via SendGrid
"""

from __future__ import annotations

import base64
import csv
import datetime
import io
import json
import os
import re
import sqlite3
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Optional

# ── Dependencies ──────────────────────────────────────────────────────────────
try:
    from curl_cffi import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("[backfill] ERROR: Required libraries missing. Run: pip install curl_cffi beautifulsoup4", flush=True)
    sys.exit(1)

# ── Paths ─────────────────────────────────────────────────────────────────────
_SCRIPT_DIR   = Path(__file__).parent.resolve()
PROGRESS_FILE = _SCRIPT_DIR / "backfill_progress.json"
DESKTOP       = Path.home() / "Desktop"
CSV_PATH      = DESKTOP / "backfill_results.csv"
DB_PATH       = os.environ.get("DB_PATH", "/app/sql_app.db")

# ── Constants ─────────────────────────────────────────────────────────────────
BASE_URL        = "https://websurrogates.nycourts.gov"
FILE_SEARCH     = f"{BASE_URL}/File/FileSearch"
MONTHS_BACK     = 24
TARGET_COURTS   = [
    "Suffolk County Surrogate's Court",
    "Nassau County Surrogate's Court",
]
PROCEEDINGS     = ["ADMINISTRATION PETITION", "PROBATE PETITION"]
RECIPIENT_EMAIL = "Anthonychamalerealty@gmail.com"
GPT_MODEL       = "gpt-4o"
REQUEST_DELAY   = 3  # seconds between requests to avoid rate limits

# ── Environment ───────────────────────────────────────────────────────────────
_COOKIES_JSON     = os.environ.get("WEBSURROGATES_COOKIES_JSON", "")
_OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "")
_SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
_OPENAI_AVAILABLE = bool(_OPENAI_API_KEY)

# ── GPT-4o Prompts ────────────────────────────────────────────────────────────
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
# Cookie sanitization
# ─────────────────────────────────────────────────────────────────────────────

def sanitize_cookies(cookies: list[dict]) -> list[dict]:
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


# ─────────────────────────────────────────────────────────────────────────────
# Database setup
# ─────────────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_db_tables():
    """Ensure the leads and contacts tables exist in the SQLite database."""
    try:
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        with get_db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    address TEXT,
                    state TEXT,
                    county TEXT,
                    owner_name TEXT,
                    phone TEXT,
                    email TEXT,
                    source TEXT,
                    score REAL,
                    created_at TEXT,
                    status TEXT,
                    raw_data TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS contacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lead_id INTEGER,
                    first_name TEXT,
                    last_name TEXT,
                    phone TEXT,
                    email TEXT,
                    type TEXT,
                    created_at TEXT,
                    FOREIGN KEY(lead_id) REFERENCES leads(id)
                )
            """)
            conn.commit()
        print(f"[backfill] SQLite database initialized at: {DB_PATH}", flush=True)
        return True
    except Exception as exc:
        print(f"[backfill] SQLite init error: {exc}", flush=True)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Progress helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            print(f"[backfill] Resuming from saved progress: {PROGRESS_FILE.name}", flush=True)
            return data
        except Exception as exc:
            print(f"[backfill] WARNING: Could not load progress file: {exc} — starting fresh.", flush=True)
    return {}


def save_progress(progress: dict):
    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(progress, f, indent=2)
    except Exception as exc:
        print(f"[backfill] WARNING: Could not save progress: {exc}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# CSV export
# ─────────────────────────────────────────────────────────────────────────────

def write_csv(leads: list[dict], path: Path) -> bool:
    try:
        if not leads:
            print("[backfill] No leads to write.", flush=True)
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        flat_leads = []
        for lead in leads:
            row = {k: v for k, v in lead.items() if k != "raw_data"}
            raw = lead.get("raw_data", {})
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except Exception:
                    raw = {}
            if isinstance(raw, dict):
                for rk, rv in raw.items():
                    if rk not in row:
                        row[rk] = rv
            flat_leads.append(row)
        fieldnames: list[str] = []
        seen: set[str] = set()
        for row in flat_leads:
            for k in row:
                if k not in seen:
                    fieldnames.append(k)
                    seen.add(k)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(flat_leads)
        print(f"[backfill] CSV written: {path} ({len(flat_leads)} rows)", flush=True)
        return True
    except Exception as exc:
        print(f"[backfill] CSV error: {exc}", flush=True)
        traceback.print_exc()
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Email
# ─────────────────────────────────────────────────────────────────────────────

def send_email(to_email: str, subject: str, html_body: str,
               attachment_path: Optional[Path] = None,
               attachment_filename: Optional[str] = None) -> bool:
    if not _SENDGRID_API_KEY:
        print("[backfill] WARNING: SENDGRID_API_KEY not set — skipping email.", flush=True)
        return False
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import (
            Mail, Attachment, FileContent, FileName, FileType, Disposition,
        )
    except ImportError:
        print("[backfill] WARNING: sendgrid not installed — run: pip install sendgrid", flush=True)
        return False
    try:
        message = Mail(
            from_email="Anthonychamalerealty@gmail.com",
            to_emails=to_email,
            subject=subject,
            html_content=html_body,
        )
        if attachment_path and attachment_filename and attachment_path.exists():
            with open(attachment_path, "rb") as f:
                encoded = base64.b64encode(f.read()).decode()
            message.attachment = Attachment(
                FileContent(encoded),
                FileName(attachment_filename),
                FileType("text/csv"),
                Disposition("attachment"),
            )
        sg = SendGridAPIClient(_SENDGRID_API_KEY)
        response = sg.send(message)
        if response.status_code in (200, 202):
            print(f"[backfill] Email sent to {to_email} (status {response.status_code})", flush=True)
            return True
        else:
            print(f"[backfill] SendGrid unexpected status {response.status_code}: {response.body}", flush=True)
            return False
    except Exception as exc:
        print(f"[backfill] Email error: {exc}", flush=True)
        traceback.print_exc()
        return False


# ─────────────────────────────────────────────────────────────────────────────
# TruePeopleSearch integration
# ─────────────────────────────────────────────────────────────────────────────

def search_truepeoplesearch(name: str, city_state_zip: str) -> list[str]:
    """Search TruePeopleSearch for mobile numbers using curl_cffi."""
    print(f"[backfill]   Searching TruePeopleSearch: name='{name}', location='{city_state_zip}'", flush=True)
    phones = []
    try:
        parsed_name = name.replace(" ", "+")
        parsed_loc  = city_state_zip.replace(" ", "+")
        url = f"https://www.truepeoplesearch.com/results?name={parsed_name}&citystatezip={parsed_loc}"
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.truepeoplesearch.com/",
        }
        
        time.sleep(REQUEST_DELAY)
        resp = requests.get(url, headers=headers, impersonate="chrome120", timeout=30)
        
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            # Extract phone numbers
            # Pattern 1: look for phone links
            for link in soup.find_all("a", href=re.compile(r"^tel:")):
                num = link.get_text(strip=True)
                if num and num not in phones:
                    phones.append(num)
            # Pattern 2: look for divs/spans with class containing 'phone'
            for el in soup.find_all(class_=re.compile(r"phone", re.I)):
                txt = el.get_text(strip=True)
                # Match typical US phone patterns
                matches = re.findall(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", txt)
                for m in matches:
                    if m not in phones:
                        phones.append(m)
            print(f"[backfill]     TruePeopleSearch found {len(phones)} phone(s): {phones}", flush=True)
        else:
            print(f"[backfill]     TruePeopleSearch request failed (status {resp.status_code})", flush=True)
    except Exception as exc:
        print(f"[backfill]     TruePeopleSearch error: {exc}", flush=True)
    return phones


# ─────────────────────────────────────────────────────────────────────────────
# WebSurrogates Scraper
# ─────────────────────────────────────────────────────────────────────────────

class SuffolkBackfillScraper:
    def __init__(self, cookies_json: str):
        self.session = requests.Session(impersonate="chrome120")
        self.cookies_json = cookies_json
        self._load_cookies()

    def _load_cookies(self):
        if not self.cookies_json:
            print("[backfill] WARNING: WEBSURROGATES_COOKIES_JSON not set.", flush=True)
            return
        try:
            raw = json.loads(self.cookies_json)
            cleaned = sanitize_cookies(raw)
            for c in cleaned:
                self.session.cookies.set(
                    c["name"],
                    c["value"],
                    domain=c["domain"],
                    path=c["path"]
                )
            print(f"[backfill] Loaded {len(cleaned)} cookies into curl_cffi session.", flush=True)
        except Exception as exc:
            print(f"[backfill] Cookie load error: {exc}", flush=True)

    def _is_blocked(self, html: str) -> bool:
        soup = BeautifulSoup(html, "html.parser")
        title = (soup.title.string or "").lower() if soup.title else ""
        if "request could not be processed" in title or "just a moment" in title:
            return True
        body = soup.get_text()[:600].lower()
        if any(s in body for s in [
            "request could not be processed",
            "disabling your vpn",
            "access denied",
            "verify you are human",
        ]):
            return True
        return False

    def search_filings(self, court_name: str, proceeding: str,
                       date_from: str, date_to: str) -> list[dict]:
        print(f"[backfill] Searching: {court_name} | {proceeding} | {date_from} → {date_to}", flush=True)
        
        # 1. Hit homepage first to establish session
        try:
            time.sleep(REQUEST_DELAY)
            self.session.get(BASE_URL, timeout=30)
        except Exception as exc:
            print(f"[backfill] Homepage warning: {exc}", flush=True)

        # 2. Get search form to retrieve RequestVerificationToken if present
        token = ""
        try:
            time.sleep(REQUEST_DELAY)
            resp = self.session.get(FILE_SEARCH, timeout=30)
            if self._is_blocked(resp.text):
                print("[backfill] BLOCKED by Cloudflare on File Search form.", flush=True)
                return []
            soup = BeautifulSoup(resp.text, "html.parser")
            tok_input = soup.find("input", {"name": "__RequestVerificationToken"})
            if tok_input:
                token = tok_input.get("value", "")
        except Exception as exc:
            print(f"[backfill] Search form warning: {exc}", flush=True)

        # 3. Resolve Court and Proceeding select option values
        # (Usually "114" for Suffolk, "110" for Nassau, etc.)
        court_val = "114" if "Suffolk" in court_name else "110"
        proc_val = "22" if "ADMINISTRATION" in proceeding.upper() else "21"

        # 4. Perform POST Search
        payload = {
            "__RequestVerificationToken": token,
            "Court": court_val,
            "FileProceeding": proc_val,
            "FromDate": date_from,
            "ToDate": date_to,
            "FileNumber": "",
            "DecLastName": "",
            "DecFirstName": "",
        }
        
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": FILE_SEARCH,
        }

        try:
            time.sleep(REQUEST_DELAY)
            resp = self.session.post(FILE_SEARCH, data=payload, headers=headers, timeout=30)
            if self._is_blocked(resp.text):
                print("[backfill] BLOCKED by Cloudflare on POST Search.", flush=True)
                return []
            return self._parse_results(resp.text, proceeding)
        except Exception as exc:
            print(f"[backfill] POST Search error: {exc}", flush=True)
            return []

    def _parse_results(self, html: str, proceeding: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        result_table = None
        for tbl in soup.find_all("table"):
            ths = [th.get_text(strip=True).upper() for th in tbl.find_all("th")]
            if any("FILE" in h for h in ths):
                result_table = tbl
                break
        if not result_table:
            print("[backfill]   No results table found.", flush=True)
            return []
        
        filings = []
        rows = result_table.find_all("tr")[1:]
        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 4:
                continue
            file_cell   = cells[0]
            file_link   = file_cell.find("a")
            file_number = file_link.get_text(strip=True) if file_link else file_cell.get_text(strip=True)
            file_href   = file_link.get("href", "") if file_link else ""
            file_date   = cells[1].get_text(strip=True) if len(cells) > 1 else ""
            decedent    = cells[2].get_text(strip=True) if len(cells) > 2 else ""
            proc_type   = cells[3].get_text(strip=True) if len(cells) > 3 else proceeding
            dod         = cells[4].get_text(strip=True) if len(cells) > 4 else ""
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
        print(f"[backfill]   Found {len(filings)} filings.", flush=True)
        return filings

    def process_filing(self, filing: dict, county: str) -> Optional[dict]:
        file_number = filing["file_number"]
        print(f"[backfill] Processing {file_number}: {filing['decedent_name']}", flush=True)

        try:
            time.sleep(REQUEST_DELAY)
            resp = self.session.get(filing["history_url"], timeout=30)
            if self._is_blocked(resp.text):
                print(f"[backfill] BLOCKED on File History for {file_number}", flush=True)
                return None
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception as exc:
            print(f"[backfill] File History load error: {exc}", flush=True)
            return None

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
            print(f"[backfill] Party extraction warning for {file_number}: {exc}", flush=True)

        # Find petition document link
        pdf_url = None
        try:
            for link in soup.find_all("a"):
                txt = link.get_text(strip=True).upper()
                if "ADMINISTRATION PETITION" in txt or "PROBATE PETITION" in txt:
                    href = link.get("href", "")
                    if href:
                        pdf_url = (
                            href if href.startswith("http")
                            else BASE_URL + (href if href.startswith("/") else "/" + href)
                        )
                        break
        except Exception as exc:
            print(f"[backfill] PDF link search warning for {file_number}: {exc}", flush=True)

        if not pdf_url:
            print(f"[backfill] No petition document link found for {file_number} — skipping.", flush=True)
            return None

        # Download petition document and run GPT-4o vision
        pdf_data = self._extract_document(pdf_url, file_number)
        if not pdf_data:
            print(f"[backfill] Document extraction failed for {file_number} — skipping.", flush=True)
            return None

        # Section 3(b) real property filter
        prop_dollars = pdf_data.get("real_property_value_dollars")
        has_prop     = pdf_data.get("has_real_property")
        raw_val      = pdf_data.get("real_property_value_raw", "")
        if has_prop is False and (prop_dollars == 0 or prop_dollars is None):
            print(f"[backfill] SKIPPED {file_number}: section 3(b) real property is $0 / NONE.", flush=True)
            return None

        # Build addresses
        def _addr(prefix: str) -> str:
            parts = [
                pdf_data.get(f"{prefix}_street") or "",
                pdf_data.get(f"{prefix}_city_town") or "",
                pdf_data.get(f"{prefix}_state") or "",
                pdf_data.get(f"{prefix}_zip") or "",
            ]
            return ", ".join(p.strip() for p in parts if p.strip())

        petitioner_name    = pdf_data.get("petitioner_name") or ""
        petitioner_address = _addr("petitioner")
        decedent_address   = _addr("decedent") or f"{filing['decedent_name']} — {county} County, NY"
        phone              = pdf_data.get("petitioner_phone") or ""

        # Search TruePeopleSearch as additional enrichment
        tps_phones = []
        if petitioner_name:
            tps_phones = search_truepeoplesearch(petitioner_name, petitioner_address or f"{county}, NY")
        
        # Merge phones
        all_phones = [phone] if phone else []
        for tp in tps_phones:
            if tp not in all_phones:
                all_phones.append(tp)
        merged_phone_str = ", ".join(all_phones)

        raw_data = {
            "file_number":             filing["file_number"],
            "file_date":               filing["file_date"],
            "proceeding":              filing["proceeding"],
            "case_url":                filing["history_url"],
            "pdf_url":                 pdf_url,
            "decedent_name":           pdf_data.get("decedent_name") or filing["decedent_name"],
            "decedent_address":        decedent_address,
            "date_of_death":           pdf_data.get("date_of_death") or filing.get("dod", ""),
            "township":                pdf_data.get("township") or "",
            "county":                  county,
            "place_of_death":          pdf_data.get("place_of_death") or "",
            "petitioner_name":         petitioner_name,
            "petitioner_address":      petitioner_address,
            "petitioner_phone":        phone,
            "petitioner_relationship": pdf_data.get("petitioner_relationship") or "",
            "real_property_value":     prop_dollars,
            "real_property_value_raw": raw_val,
            "parties":                 json.dumps(parties),
            "truepeoplesearch_phones": tps_phones,
        }

        lead = {
            "address":    decedent_address,
            "state":      "NY",
            "county":     county,
            "owner_name": petitioner_name or "Unknown",
            "phone":      merged_phone_str,
            "email":      "",
            "source":     "probate",
            "score":      self._score(pdf_data, tps_phones),
            "created_at": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "status":     "new",
            "raw_data":   json.dumps(raw_data),
        }
        
        # Return lead + individual contacts
        contacts = []
        if petitioner_name:
            # Main petitioner contact
            parts = petitioner_name.split(" ", 1)
            fn = parts[0]
            ln = parts[1] if len(parts) > 1 else ""
            contacts.append({
                "first_name": fn,
                "last_name":  ln,
                "phone":      phone,
                "email":      "",
                "type":       pdf_data.get("petitioner_relationship") or "Petitioner",
            })
            # Additional TruePeopleSearch phone contacts
            for i, tp_phone in enumerate(tps_phones):
                contacts.append({
                    "first_name": fn,
                    "last_name":  ln,
                    "phone":      tp_phone,
                    "email":      "",
                    "type":       f"TruePeopleSearch Phone {i+1}",
                })

        print(
            f"[backfill]   LEAD SAVED: {file_number} | {decedent_address} | "
            f"phones={merged_phone_str or '(none)'} | prop=${prop_dollars}",
            flush=True,
        )
        return {"lead": lead, "contacts": contacts}

    def _score(self, pdf_data: dict, tps_phones: list[str]) -> float:
        score = 60.0
        val = pdf_data.get("real_property_value_dollars")
        if val:
            if val >= 500000:   score += 30
            elif val >= 200000: score += 20
            elif val >= 100000: score += 10
        if pdf_data.get("petitioner_phone"):  score += 5
        if tps_phones:                        score += 5
        if pdf_data.get("decedent_street"):   score += 5
        return min(score, 100.0)

    def _extract_document(self, doc_url: str, file_number: str) -> Optional[dict]:
        """Download document and run GPT-4o vision."""
        if not _OPENAI_AVAILABLE:
            print("[backfill] Skipping GPT-4o extraction — OPENAI_API_KEY not set.", flush=True)
            return None
        try:
            time.sleep(REQUEST_DELAY)
            resp = self.session.get(doc_url, timeout=30)
            if self._is_blocked(resp.text):
                print(f"[backfill] BLOCKED by Cloudflare on document {file_number}", flush=True)
                return None
            
            # Since we are not using pdf2image to keep this lightweight, we convert
            # the PDF bytes directly to base64 if it's an image-only PDF, or we use
            # pdf2image if installed. Let's try converting the PDF pages to PNG.
            try:
                from pdf2image import convert_from_bytes
                images = convert_from_bytes(resp.content, first_page=1, last_page=2)
            except Exception as exc:
                print(f"[backfill] WARNING: pdf2image conversion failed: {exc}. Please make sure poppler is installed.", flush=True)
                return None

            if not images:
                print(f"[backfill] No pages extracted from PDF {file_number}.", flush=True)
                return None

            result = {}
            # Page 1
            p1_bytes = io_bytes = None
            try:
                io_bytes = io.BytesIO()
                images[0].save(io_bytes, format="PNG")
                p1_b64 = base64.b64encode(io_bytes.getvalue()).decode()
                p1 = self._gpt4o_screenshot(p1_b64, _PAGE1_PROMPT, f"{file_number} p1")
                if p1: result.update(p1)
            except Exception as exc:
                print(f"[backfill] Page 1 extraction error: {exc}", flush=True)

            # Page 2
            if len(images) > 1:
                try:
                    io_bytes = io.BytesIO()
                    images[1].save(io_bytes, format="PNG")
                    p2_b64 = base64.b64encode(io_bytes.getvalue()).decode()
                    p2 = self._gpt4o_screenshot(p2_b64, _PAGE2_PROMPT, f"{file_number} p2")
                    if p2: result.update(p2)
                except Exception as exc:
                    print(f"[backfill] Page 2 extraction error: {exc}", flush=True)

            if not result:
                # Fallback
                result["has_real_property"] = True
                result["real_property_value_raw"] = "unknown (screenshot extraction failed)"
                result["real_property_value_dollars"] = None

            return result
        except Exception as exc:
            print(f"[backfill] PDF download/extract error for {file_number}: {exc}", flush=True)
            return None

    def _gpt4o_screenshot(self, b64_image: str, prompt: str, label: str) -> Optional[dict]:
        import requests as _requests
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
            resp = _requests.post(
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


# ─────────────────────────────────────────────────────────────────────────────
# Main Loop
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 70, flush=True)
    print("  NY PROBATE BACKFILL — curl_cffi LIGHTWEIGHT RUNNER", flush=True)
    print(f"  SQLite DB     : {DB_PATH}", flush=True)
    print(f"  Progress file : {PROGRESS_FILE}", flush=True)
    print(f"  Output CSV    : {CSV_PATH}", flush=True)
    print(f"  OpenAI        : {'✓ ready' if _OPENAI_AVAILABLE else '✗ OPENAI_API_KEY not set'}", flush=True)
    print(f"  SendGrid      : {'✓ ready' if _SENDGRID_API_KEY else '✗ SENDGRID_API_KEY not set'}", flush=True)
    print("=" * 70, flush=True)

    if not _OPENAI_AVAILABLE:
        print("[backfill] ERROR: OPENAI_API_KEY is required.", flush=True)
        sys.exit(1)
    if not _COOKIES_JSON:
        print("[backfill] ERROR: WEBSURROGATES_COOKIES_JSON environment variable is required.", flush=True)
        sys.exit(1)

    # Initialize DB
    if not ensure_db_tables():
        sys.exit(1)

    # Load progress
    progress           = load_progress()
    completed_searches = progress.setdefault("completed_searches", [])
    processed_files    = progress.setdefault("processed_files", {})
    saved_leads        = progress.setdefault("saved_leads", [])

    print(
        f"[backfill] Progress: {len(completed_searches)} searches done, "
        f"{len(processed_files)} files processed, "
        f"{len(saved_leads)} leads saved so far.",
        flush=True,
    )

    # Build 24 monthly date blocks (oldest → newest)
    today = datetime.date.today()
    blocks: list[tuple[datetime.date, datetime.date]] = []
    curr_end = today
    for _ in range(MONTHS_BACK):
        curr_start = curr_end - datetime.timedelta(days=29)
        blocks.append((curr_start, curr_end))
        curr_end = curr_start - datetime.timedelta(days=1)
    blocks.reverse()

    scraper = SuffolkBackfillScraper(_COOKIES_JSON)

    total_blocks = len(TARGET_COURTS) * len(PROCEEDINGS) * len(blocks)
    block_num = 0

    try:
        for court in TARGET_COURTS:
            county = "Suffolk" if "Suffolk" in court else "Nassau"
            for proceeding in PROCEEDINGS:
                for start_date, end_date in blocks:
                    start_str  = start_date.strftime("%m/%d/%Y")
                    end_str    = end_date.strftime("%m/%d/%Y")
                    search_key = f"{court}|{proceeding}|{start_str}|{end_str}"
                    block_num += 1

                    if search_key in completed_searches:
                        print(f"[backfill] Skipping completed block ({block_num}/{total_blocks}): {search_key}", flush=True)
                        continue

                    print(f"\n[backfill] Block {block_num}/{total_blocks}: {search_key}", flush=True)

                    filings = scraper.search_filings(court, proceeding, start_str, end_str)

                    for filing in filings:
                        file_num = filing["file_number"]
                        
                        # 1. Skip duplicates using DB file number check
                        with get_db() as conn:
                            existing = conn.execute(
                                "SELECT id FROM leads WHERE raw_data LIKE ?",
                                (f'%"file_number": "{file_num}"%',)
                            ).fetchone()
                        if existing:
                            print(f"[backfill] Already in DB: {file_num} — skipping.", flush=True)
                            processed_files[file_num] = {"status": "skipped_duplicate"}
                            continue

                        if file_num in processed_files:
                            print(f"[backfill] Already processed this run: {file_num} — skipping.", flush=True)
                            continue

                        try:
                            res = scraper.process_filing(filing, county)
                            if res:
                                lead = res["lead"]
                                contacts = res["contacts"]

                                # Save to DB
                                with get_db() as conn:
                                    cur = conn.cursor()
                                    cur.execute("""
                                        INSERT INTO leads (
                                            address, state, county, owner_name, phone, email,
                                            source, score, created_at, status, raw_data
                                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                    """, (
                                        lead["address"], lead["state"], lead["county"],
                                        lead["owner_name"], lead["phone"], lead["email"],
                                        lead["source"], lead["score"], lead["created_at"],
                                        lead["status"], lead["raw_data"]
                                    ))
                                    lead_id = cur.lastrowid
                                    
                                    for c in contacts:
                                        conn.execute("""
                                            INSERT INTO contacts (
                                                lead_id, first_name, last_name, phone, email, type, created_at
                                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                                        """, (
                                            lead_id, c["first_name"], c["last_name"],
                                            c["phone"], c["email"], c["type"], lead["created_at"]
                                        ))
                                    conn.commit()

                                saved_leads.append(lead)
                                processed_files[file_num] = {"status": "saved"}
                            else:
                                processed_files[file_num] = {"status": "skipped"}
                        except Exception as exc:
                            print(f"[backfill] ERROR processing {file_num}: {exc}", flush=True)
                            traceback.print_exc()
                            processed_files[file_num] = {"status": "error", "error": str(exc)}

                        # Save progress every 10 leads
                        if len(saved_leads) % 10 == 0:
                            save_progress(progress)

                    completed_searches.append(search_key)
                    save_progress(progress)

    except KeyboardInterrupt:
        print("\n[backfill] Interrupted by user. Saving progress...", flush=True)
        save_progress(progress)
    except Exception as exc:
        print(f"[backfill] FATAL ERROR: {exc}", flush=True)
        traceback.print_exc()
        save_progress(progress)

    # Final save
    save_progress(progress)

    # Write CSV
    print(f"\n[backfill] Backfill complete. {len(saved_leads)} lead(s) saved.", flush=True)
    csv_ok = write_csv(saved_leads, CSV_PATH)

    # Send email
    if csv_ok:
        date_str  = datetime.datetime.now().strftime("%Y-%m-%d")
        subject   = f"NY Probate Backfill Complete — {len(saved_leads)} Leads — {date_str}"
        html_body = f"""<!DOCTYPE html>
<html>
<body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333; padding: 20px;">
  <h2 style="color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 10px;">
    NY Probate Historical Backfill — curl_cffi Run
  </h2>
  <p>Hello Anthony,</p>
  <p>The curl_cffi backfill job has completed successfully.</p>
  <table style="border-collapse: collapse; width: 100%; max-width: 520px;">
    <tr><td style="padding:6px;font-weight:bold;">Date range:</td>
        <td style="padding:6px;">Last {MONTHS_BACK} months</td></tr>
    <tr style="background:#f9f9f9">
        <td style="padding:6px;font-weight:bold;">Courts:</td>
        <td style="padding:6px;">Suffolk County, Nassau County</td></tr>
    <tr><td style="padding:6px;font-weight:bold;">Proceeding types:</td>
        <td style="padding:6px;">Administration Petition, Probate Petition</td></tr>
    <tr style="background:#f9f9f9">
        <td style="padding:6px;font-weight:bold;">Total leads saved:</td>
        <td style="padding:6px;"><strong>{len(saved_leads)}</strong></td></tr>
    <tr><td style="padding:6px;font-weight:bold;">Filter:</td>
        <td style="padding:6px;">Skipped cases where section 3(b) real property = $0 or NONE</td></tr>
  </table>
  <p>The full export is attached as a CSV file.</p>
  <hr style="border:none;border-top:1px solid #eee;margin-top:20px;">
  <p style="font-size:12px;color:#7f8c8d;">
    Generated by suffolk_backfill.py on {datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
  </p>
</body>
</html>"""
        send_email(
            to_email=RECIPIENT_EMAIL,
            subject=subject,
            html_body=html_body,
            attachment_path=CSV_PATH,
            attachment_filename=f"ny_probate_backfill_{date_str}.csv",
        )
    else:
        print("[backfill] CSV not generated — email not sent.", flush=True)

    print(f"\n[backfill] Results saved to: {CSV_PATH}", flush=True)
    print("[backfill] JOB FINISHED.", flush=True)


if __name__ == "__main__":
    main()
