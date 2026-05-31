#!/usr/bin/env python3
"""
run_backfill_mac.py
-------------------
Standalone Mac backfill script for NY probate records.

Runs DIRECTLY from your home IP — NO proxy required.
Reads cookies from a cookies.json file in the same folder as this script.

What it does
============
1. Reads cookies.json from the same folder (export with EditThisCookie).
2. Launches a headless Chromium browser and injects the cookies.
3. Navigates to https://websurrogates.nycourts.gov/File/FileSearch
4. Searches Suffolk County and Nassau County Surrogate's Courts.
5. Loops through 24 monthly date ranges (oldest → newest).
6. Searches both ADMINISTRATION PETITION and PROBATE PETITION types.
7. Clicks into each case, opens the petition document.
8. Uses GPT-4o vision to extract:
     - Decedent name, address, date of death
     - Petitioner name, address, phone number, relationship
     - Section 3(b) real property value
9. Skips cases where section 3(b) real property value is $0 / blank / NONE.
10. Saves results to ~/Desktop/backfill_results.csv
11. Saves progress to backfill_progress.json (same folder as cookies.json)
    so the script can resume if interrupted — just run it again.
12. Sends the final CSV to Anthonychamalerealty@gmail.com via SendGrid.

Required environment variables
================================
  OPENAI_API_KEY    — OpenAI API key for GPT-4o vision extraction
  SENDGRID_API_KEY  — SendGrid API key for the final email

Usage
=====
  Set the env vars and run:
    python3 run_backfill_mac.py

  Or use the provided run_mac.sh launcher (just double-click it in Finder
  after making it executable: chmod +x run_mac.sh).

Cookies
=======
  Export your websurrogates.nycourts.gov cookies using the EditThisCookie
  Chrome extension (Export as JSON), then save the file as:
    cookies.json   (in the same folder as this script)

  Cookies expire every ~24 hours — refresh cookies.json daily if you
  need to run the script on multiple days.
"""

from __future__ import annotations

import base64
import csv
import datetime
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Optional

# ── Paths ─────────────────────────────────────────────────────────────────────
_SCRIPT_DIR   = Path(__file__).parent.resolve()
COOKIES_FILE  = _SCRIPT_DIR / "cookies.json"
PROGRESS_FILE = _SCRIPT_DIR / "backfill_progress.json"
DESKTOP       = Path.home() / "Desktop"
CSV_PATH      = DESKTOP / "backfill_results.csv"

# ── Constants ─────────────────────────────────────────────────────────────────
BASE_URL       = "https://websurrogates.nycourts.gov"
FILE_SEARCH    = f"{BASE_URL}/File/FileSearch"
REQUEST_DELAY  = 3        # seconds between page loads
MONTHS_BACK    = 24       # how many months of history to backfill
TARGET_COURTS  = ["Suffolk County Surrogate's Court",
                  "Nassau County Surrogate's Court"]
PROCEEDINGS    = ["ADMINISTRATION PETITION", "PROBATE PETITION"]
RECIPIENT_EMAIL = "Anthonychamalerealty@gmail.com"
GPT_MODEL      = "gpt-4o"

# ── Environment ───────────────────────────────────────────────────────────────
_OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "")
_SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
_OPENAI_AVAILABLE = bool(_OPENAI_API_KEY)

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
# Cookie helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_cookies_from_file(path: Path) -> list[dict]:
    """Load and sanitize cookies from a JSON file exported by EditThisCookie."""
    if not path.exists():
        print(f"[mac] ERROR: cookies.json not found at {path}", flush=True)
        print("[mac] Export your websurrogates.nycourts.gov cookies using EditThisCookie", flush=True)
        print("[mac] and save the file as cookies.json in the same folder as this script.", flush=True)
        sys.exit(1)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        cookies = sanitize_cookies(raw)
        print(f"[mac] Loaded {len(cookies)} cookies from {path.name}", flush=True)
        return cookies
    except Exception as exc:
        print(f"[mac] ERROR reading cookies.json: {exc}", flush=True)
        traceback.print_exc()
        sys.exit(1)


def sanitize_cookies(cookies: list) -> list[dict]:
    """Convert EditThisCookie export to Playwright-compatible cookie dicts."""
    valid_samesite = {"Strict", "Lax", "None"}
    samesite_map = {
        "no_restriction": "None",
        "unspecified":    "Lax",
        "strict":         "Strict",
        "lax":            "Lax",
        "none":           "None",
    }
    cleaned = []
    for cookie in cookies:
        c = {
            "name":     cookie.get("name", ""),
            "value":    cookie.get("value", ""),
            "domain":   cookie.get("domain", ""),
            "path":     cookie.get("path", "/"),
            "secure":   cookie.get("secure", False),
            "httpOnly": cookie.get("httpOnly", False),
        }
        ss = cookie.get("sameSite", "Lax") or "Lax"
        c["sameSite"] = samesite_map.get(ss.lower(), "Lax") if ss not in valid_samesite else ss
        if cookie.get("expirationDate"):
            c["expires"] = int(cookie["expirationDate"])
        cleaned.append(c)
    return cleaned


# ─────────────────────────────────────────────────────────────────────────────
# Progress helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            print(f"[mac] Resuming from saved progress: {PROGRESS_FILE.name}", flush=True)
            return data
        except Exception as exc:
            print(f"[mac] WARNING: Could not load progress file: {exc} — starting fresh.", flush=True)
    return {}


def save_progress(progress: dict):
    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(progress, f, indent=2)
    except Exception as exc:
        print(f"[mac] WARNING: Could not save progress: {exc}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# CSV export
# ─────────────────────────────────────────────────────────────────────────────

def write_csv(leads: list[dict], path: Path) -> bool:
    """Write leads to a CSV file.  Returns True on success."""
    try:
        if not leads:
            print("[mac] No leads to write.", flush=True)
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        # Flatten raw_data dict into top-level columns
        flat_leads = []
        for lead in leads:
            row = {k: v for k, v in lead.items() if k != "raw_data"}
            raw = lead.get("raw_data", {})
            if isinstance(raw, dict):
                for rk, rv in raw.items():
                    if rk not in row:
                        row[rk] = rv
            flat_leads.append(row)
        # Collect all field names preserving order
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
        print(f"[mac] CSV written: {path} ({len(flat_leads)} rows)", flush=True)
        return True
    except Exception as exc:
        print(f"[mac] CSV error: {exc}", flush=True)
        traceback.print_exc()
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Email
# ─────────────────────────────────────────────────────────────────────────────

def send_email(to_email: str, subject: str, html_body: str,
               attachment_path: Optional[Path] = None,
               attachment_filename: Optional[str] = None) -> bool:
    """Send an email via SendGrid with an optional CSV attachment."""
    api_key = _SENDGRID_API_KEY
    if not api_key:
        print("[mac] WARNING: SENDGRID_API_KEY not set — skipping email.", flush=True)
        return False
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import (
            Mail, Attachment, FileContent, FileName, FileType, Disposition,
        )
    except ImportError:
        print("[mac] WARNING: sendgrid package not installed — run: pip3 install sendgrid", flush=True)
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
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        if response.status_code in (200, 202):
            print(f"[mac] Email sent to {to_email} (status {response.status_code})", flush=True)
            return True
        else:
            print(f"[mac] SendGrid unexpected status {response.status_code}: {response.body}", flush=True)
            return False
    except Exception as exc:
        print(f"[mac] Email error: {exc}", flush=True)
        traceback.print_exc()
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Scraper class
# ─────────────────────────────────────────────────────────────────────────────

class MacBackfillScraper:
    """Runs the WebSurrogates backfill directly from a Mac with no proxy."""

    def __init__(self, session_cookies: list[dict]):
        self._session_cookies = session_cookies

    # ── Block detection ───────────────────────────────────────────────────────
    def _is_blocked(self, page) -> bool:
        title = page.title().lower()
        if "request could not be processed" in title or "just a moment" in title:
            return True
        try:
            body = page.inner_text("body")[:600].lower()
            if "request could not be processed" in body or "disabling your vpn" in body or "access denied" in body:
                return True
        except Exception:
            pass
        return False

    # ── Search filings ────────────────────────────────────────────────────────
    def search_filings(self, page, court_name: str, proceeding: str,
                       date_from: str, date_to: str) -> list[dict]:
        print(f"[mac] Searching: {court_name} | {proceeding} | {date_from} → {date_to}", flush=True)
        try:
            page.goto(FILE_SEARCH, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            print(f"[mac] Timeout navigating to File Search: {exc}", flush=True)
            return []
        time.sleep(REQUEST_DELAY)

        if self._is_blocked(page):
            print("[mac] BLOCKED on File Search — cookies may be expired.", flush=True)
            return []

        # Select Court
        try:
            court_sel = page.query_selector("select#Court, select[name='Court']")
            if court_sel:
                opts = court_sel.query_selector_all("option")
                for opt in opts:
                    if court_name.lower() in opt.inner_text().lower():
                        court_sel.select_option(value=opt.get_attribute("value"))
                        print(f"[mac]   Court: {opt.inner_text().strip()}", flush=True)
                        break
        except Exception as exc:
            print(f"[mac] WARNING: Court selection failed: {exc}", flush=True)

        time.sleep(0.5)

        # Select Proceeding
        try:
            proc_sel = page.query_selector("select#FileProceeding, select[name='FileProceeding']")
            if proc_sel:
                opts = proc_sel.query_selector_all("option")
                for opt in opts:
                    if opt.inner_text().strip().upper() == proceeding.upper():
                        proc_sel.select_option(value=opt.get_attribute("value"))
                        print(f"[mac]   Proceeding: {opt.inner_text().strip()}", flush=True)
                        break
        except Exception as exc:
            print(f"[mac] WARNING: Proceeding selection failed: {exc}", flush=True)

        # Fill dates
        try:
            from_inp = page.query_selector("input#FromDate, input[name='FromDate']")
            to_inp   = page.query_selector("input#ToDate,   input[name='ToDate']")
            if from_inp:
                from_inp.triple_click()
                from_inp.type(date_from)
            if to_inp:
                to_inp.triple_click()
                to_inp.type(date_to)
        except Exception as exc:
            print(f"[mac] WARNING: Date entry failed: {exc}", flush=True)

        # Click Search
        try:
            btns = page.query_selector_all("input[value='Search'][type='submit'], button:has-text('Search')")
            btn = btns[-1] if len(btns) >= 2 else (btns[0] if btns else None)
            if btn:
                btn.click()
            else:
                print("[mac] ERROR: Search button not found", flush=True)
                return []
        except Exception as exc:
            print(f"[mac] ERROR clicking Search: {exc}", flush=True)
            return []

        time.sleep(REQUEST_DELAY)
        if self._is_blocked(page):
            print("[mac] BLOCKED after search — cookies may be expired.", flush=True)
            return []

        return self._parse_results_table(page, proceeding)

    def _parse_results_table(self, page, proceeding: str) -> list[dict]:
        from bs4 import BeautifulSoup
        filings = []
        try:
            soup = BeautifulSoup(page.content(), "html.parser")
            result_table = None
            for tbl in soup.find_all("table"):
                ths = [th.get_text(strip=True).upper() for th in tbl.find_all("th")]
                if any("FILE" in h for h in ths):
                    result_table = tbl
                    break
            if not result_table:
                print("[mac]   No results table found (0 results for this block).", flush=True)
                return []
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
        except Exception as exc:
            print(f"[mac] ERROR parsing results table: {exc}", flush=True)
            traceback.print_exc()
        print(f"[mac]   Found {len(filings)} filing(s).", flush=True)
        return filings

    # ── Process a single filing ───────────────────────────────────────────────
    def process_filing(self, page, filing: dict, county: str) -> Optional[dict]:
        from bs4 import BeautifulSoup
        file_number = filing["file_number"]
        print(f"[mac] Processing {file_number}: {filing['decedent_name']}", flush=True)

        try:
            page.goto(filing["history_url"], wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            print(f"[mac] Timeout on File History for {file_number}: {exc}", flush=True)
            return None
        time.sleep(REQUEST_DELAY)

        if self._is_blocked(page):
            print(f"[mac] BLOCKED on File History for {file_number}", flush=True)
            return None

        soup = BeautifulSoup(page.content(), "html.parser")

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
            print(f"[mac] Party extraction warning for {file_number}: {exc}", flush=True)

        # Find petition document link
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
            print(f"[mac] PDF link search warning for {file_number}: {exc}", flush=True)

        if not pdf_url:
            print(f"[mac] No petition document link found for {file_number} — skipping.", flush=True)
            return None

        # Screenshot + GPT-4o extraction
        pdf_data = self._extract_document(page, pdf_url, file_number)
        if not pdf_data:
            print(f"[mac] Document extraction failed for {file_number} — skipping.", flush=True)
            return None

        # Section 3(b) filter
        prop_dollars = pdf_data.get("real_property_value_dollars")
        has_prop     = pdf_data.get("has_real_property")
        raw_val      = pdf_data.get("real_property_value_raw", "")
        if has_prop is False and (prop_dollars == 0 or prop_dollars is None):
            print(f"[mac] SKIPPED {file_number}: section 3(b) real property is $0 / NONE.", flush=True)
            return None

        # Build addresses
        def _addr(prefix: str) -> str:
            parts = [
                pdf_data.get(f"{prefix}_street", ""),
                pdf_data.get(f"{prefix}_city_town", ""),
                pdf_data.get(f"{prefix}_state", ""),
                pdf_data.get(f"{prefix}_zip", ""),
            ]
            return ", ".join(p.strip() for p in parts if p and p.strip())

        petitioner_name    = pdf_data.get("petitioner_name", "")
        petitioner_address = _addr("petitioner")
        decedent_address   = _addr("decedent") or f"{filing['decedent_name']} — {county} County, NY"
        phone              = pdf_data.get("petitioner_phone", "")

        raw_data = {
            "file_number":             filing["file_number"],
            "file_date":               filing["file_date"],
            "proceeding":              filing["proceeding"],
            "case_url":                filing["history_url"],
            "pdf_url":                 pdf_url,
            "decedent_name":           pdf_data.get("decedent_name") or filing["decedent_name"],
            "decedent_address":        decedent_address,
            "date_of_death":           pdf_data.get("date_of_death") or filing.get("dod", ""),
            "township":                pdf_data.get("township", ""),
            "county":                  county,
            "place_of_death":          pdf_data.get("place_of_death", ""),
            "petitioner_name":         petitioner_name,
            "petitioner_address":      petitioner_address,
            "petitioner_phone":        phone,
            "petitioner_relationship": pdf_data.get("petitioner_relationship", ""),
            "real_property_value":     prop_dollars,
            "real_property_value_raw": raw_val,
            "parties":                 json.dumps(parties),
        }

        lead = {
            "address":    decedent_address,
            "state":      "NY",
            "county":     county,
            "owner_name": petitioner_name or "Unknown",
            "phone":      phone or "",
            "email":      "",
            "source":     "probate",
            "score":      self._score(pdf_data),
            "created_at": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "status":     "new",
            "raw_data":   raw_data,
        }
        print(f"[mac] LEAD SAVED: {file_number} | {decedent_address} | phone={phone or '(none)'}", flush=True)
        return lead

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

    # ── Document screenshot + GPT-4o extraction ───────────────────────────────
    def _screenshot_document_page(self, page, doc_url: str, label: str) -> Optional[str]:
        try:
            page.goto(doc_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(REQUEST_DELAY)
            png_bytes = page.screenshot(full_page=True)
            b64 = base64.b64encode(png_bytes).decode()
            print(f"[mac] Screenshot captured for {label} ({len(png_bytes):,} bytes)", flush=True)
            return b64
        except Exception as exc:
            print(f"[mac] Screenshot error for {label}: {exc}", flush=True)
            return None

    def _gpt4o_screenshot(self, b64_image: str, prompt: str, label: str) -> Optional[dict]:
        import requests as _requests
        if not _OPENAI_AVAILABLE:
            print(f"[mac] OpenAI not available for {label} — OPENAI_API_KEY not set.", flush=True)
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
            print(f"[mac] GPT-4o error for {label}: {exc}", flush=True)
            return None

    def _extract_document(self, page, doc_url: str, file_number: str) -> Optional[dict]:
        if not _OPENAI_AVAILABLE:
            print(f"[mac] Skipping GPT-4o extraction — OPENAI_API_KEY not set.", flush=True)
            return None
        b64 = self._screenshot_document_page(page, doc_url, file_number)
        if not b64:
            return None
        result: dict = {}
        p1 = self._gpt4o_screenshot(b64, _PAGE1_PROMPT, f"{file_number} p1")
        if p1:
            result.update(p1)
        p2 = self._gpt4o_screenshot(b64, _PAGE2_PROMPT, f"{file_number} p2")
        if p2:
            result.update(p2)
        if not result:
            # If extraction failed, do not skip — flag for manual review
            result["has_real_property"] = True
            result["real_property_value_raw"] = "unknown (screenshot extraction failed)"
            result["real_property_value_dollars"] = None
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 70, flush=True)
    print("  NY PROBATE BACKFILL — MAC LOCAL RUNNER", flush=True)
    print(f"  Script dir : {_SCRIPT_DIR}", flush=True)
    print(f"  Cookies    : {COOKIES_FILE}", flush=True)
    print(f"  Progress   : {PROGRESS_FILE}", flush=True)
    print(f"  Output CSV : {CSV_PATH}", flush=True)
    print(f"  OpenAI     : {'✓ ready' if _OPENAI_AVAILABLE else '✗ OPENAI_API_KEY not set'}", flush=True)
    print(f"  SendGrid   : {'✓ ready' if _SENDGRID_API_KEY else '✗ SENDGRID_API_KEY not set'}", flush=True)
    print("=" * 70, flush=True)

    if not _OPENAI_AVAILABLE:
        print("[mac] ERROR: OPENAI_API_KEY is required for GPT-4o document extraction.", flush=True)
        sys.exit(1)

    # Load cookies
    session_cookies = load_cookies_from_file(COOKIES_FILE)

    # Load / initialise progress
    progress          = load_progress()
    completed_searches = progress.setdefault("completed_searches", [])
    processed_files   = progress.setdefault("processed_files", {})
    saved_leads       = progress.setdefault("saved_leads", [])

    print(f"[mac] Progress: {len(completed_searches)} searches done, "
          f"{len(processed_files)} files processed, "
          f"{len(saved_leads)} leads saved so far.", flush=True)

    # Build 24 monthly date blocks (oldest → newest)
    today = datetime.date.today()
    blocks: list[tuple[datetime.date, datetime.date]] = []
    curr_end = today
    for _ in range(MONTHS_BACK):
        curr_start = curr_end - datetime.timedelta(days=29)
        blocks.append((curr_start, curr_end))
        curr_end = curr_start - datetime.timedelta(days=1)
    blocks.reverse()

    print(f"[mac] Date blocks: {len(blocks)} (covering ~{MONTHS_BACK} months)", flush=True)

    # Import Playwright
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print("[mac] ERROR: playwright not installed.", flush=True)
        print("[mac] Run: pip3 install playwright && playwright install chromium", flush=True)
        sys.exit(1)

    scraper = MacBackfillScraper(session_cookies)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"],
            )
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
                locale="en-US",
                timezone_id="America/New_York",
                ignore_https_errors=True,
            )
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            if session_cookies:
                ctx.add_cookies(session_cookies)
                print(f"[mac] Injected {len(session_cookies)} cookies.", flush=True)

            page = ctx.new_page()

            # Navigate directly to File Search (public site — no login needed)
            print("[mac] Navigating to File Search...", flush=True)
            page.goto(FILE_SEARCH, wait_until="domcontentloaded", timeout=60000)
            time.sleep(REQUEST_DELAY)

            if scraper._is_blocked(page):
                print("[mac] ERROR: Blocked on File Search. Cookies may be expired.", flush=True)
                print("[mac] Please refresh cookies.json and try again.", flush=True)
                browser.close()
                sys.exit(1)

            print("[mac] Connected to WebSurrogates File Search. Starting backfill...", flush=True)

            # Main loop
            total_blocks = len(TARGET_COURTS) * len(PROCEEDINGS) * len(blocks)
            block_num = 0

            for court in TARGET_COURTS:
                county = "Suffolk" if "Suffolk" in court else "Nassau"
                for proceeding in PROCEEDINGS:
                    for start_date, end_date in blocks:
                        start_str = start_date.strftime("%m/%d/%Y")
                        end_str   = end_date.strftime("%m/%d/%Y")
                        search_key = f"{court}|{proceeding}|{start_str}|{end_str}"
                        block_num += 1

                        if search_key in completed_searches:
                            print(f"[mac] Skipping completed block ({block_num}/{total_blocks}): {search_key}", flush=True)
                            continue

                        print(f"\n[mac] Block {block_num}/{total_blocks}: {search_key}", flush=True)

                        filings = scraper.search_filings(page, court, proceeding, start_str, end_str)

                        for filing in filings:
                            file_num = filing["file_number"]
                            if file_num in processed_files:
                                print(f"[mac] Already processed {file_num} — skipping.", flush=True)
                                continue
                            try:
                                lead = scraper.process_filing(page, filing, county)
                                if lead:
                                    saved_leads.append(lead)
                                    processed_files[file_num] = {"status": "saved"}
                                else:
                                    processed_files[file_num] = {"status": "skipped"}
                            except Exception as exc:
                                print(f"[mac] ERROR processing {file_num}: {exc}", flush=True)
                                traceback.print_exc()
                                processed_files[file_num] = {"status": "error", "error": str(exc)}

                            # Save progress every 5 filings
                            if len(processed_files) % 5 == 0:
                                save_progress(progress)

                        completed_searches.append(search_key)
                        save_progress(progress)

            browser.close()

    except KeyboardInterrupt:
        print("\n[mac] Interrupted by user. Progress saved.", flush=True)
        save_progress(progress)
    except Exception as exc:
        print(f"[mac] FATAL ERROR: {exc}", flush=True)
        traceback.print_exc()
        save_progress(progress)

    # Final save
    save_progress(progress)

    # Write CSV
    print(f"\n[mac] Backfill complete. {len(saved_leads)} lead(s) saved.", flush=True)
    csv_ok = write_csv(saved_leads, CSV_PATH)

    # Send email
    if csv_ok:
        date_str = datetime.datetime.now().strftime("%Y-%m-%d")
        subject  = f"NY Probate Backfill Complete — {len(saved_leads)} Leads — {date_str}"
        html_body = f"""<!DOCTYPE html>
<html>
<body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333; padding: 20px;">
  <h2 style="color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 10px;">
    NY Probate Historical Backfill — Mac Local Run
  </h2>
  <p>Hello Anthony,</p>
  <p>The local Mac backfill job has completed.</p>
  <table style="border-collapse: collapse; width: 100%; max-width: 500px;">
    <tr><td style="padding: 6px; font-weight: bold;">Date range covered:</td>
        <td style="padding: 6px;">Last {MONTHS_BACK} months</td></tr>
    <tr style="background:#f9f9f9"><td style="padding: 6px; font-weight: bold;">Courts searched:</td>
        <td style="padding: 6px;">Suffolk County, Nassau County</td></tr>
    <tr><td style="padding: 6px; font-weight: bold;">Proceeding types:</td>
        <td style="padding: 6px;">Administration Petition, Probate Petition</td></tr>
    <tr style="background:#f9f9f9"><td style="padding: 6px; font-weight: bold;">Total leads saved:</td>
        <td style="padding: 6px;"><strong>{len(saved_leads)}</strong></td></tr>
    <tr><td style="padding: 6px; font-weight: bold;">Filter applied:</td>
        <td style="padding: 6px;">Skipped cases where section 3(b) real property = $0 or NONE</td></tr>
  </table>
  <p>The full export is attached as a CSV file.</p>
  <hr style="border: none; border-top: 1px solid #eee; margin-top: 20px;">
  <p style="font-size: 12px; color: #7f8c8d;">
    Generated by run_backfill_mac.py on {datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
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
        print("[mac] CSV not generated — email not sent.", flush=True)

    print(f"\n[mac] Results saved to: {CSV_PATH}", flush=True)
    print("[mac] JOB FINISHED.", flush=True)


if __name__ == "__main__":
    main()
