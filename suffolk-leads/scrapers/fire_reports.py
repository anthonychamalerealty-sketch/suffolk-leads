"""
fire_reports.py
---------------
Scrapes REAL residential structure fire incidents from:

NEW YORK — Suffolk County
  1. Suffolk County FRES (Fire, Rescue & Emergency Services) news feed
  2. Patch.com — individual Suffolk County town pages
  3. Newsday.com — Long Island fire news search
  4. FALLBACK: Facebook mbasic pages for Brookhaven, Huntington, Babylon,
     Islip, Smithtown FDs (only used when primary sources return 0 incidents)

GEORGIA — Six counties
  5. Atlanta Fire Rescue, Gwinnett, Cobb, DeKalb, Chatham, Athens-Clarke FDs

STRICT FILTERS (applied before saving):
  - Incident text must mention a RESIDENTIAL fire keyword
  - Address must start with a digit (has a street number)
  - Exclude: press releases, brush fires, car fires, commercial fires,
    announcements, training, awards, hiring notices

All incidents are saved directly to the leads table.
parcel_id=NULL when no matching property record exists.
No mock data is used unless --mock is passed on the CLI.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import sys
import time
import traceback as _tb
from typing import Optional

# ── Guarded third-party imports ───────────────────────────────────────────────
try:
    import requests
    from bs4 import BeautifulSoup
except ImportError as _e:
    print(f"[fire_reports] IMPORT ERROR (requests/bs4): {_e}", flush=True)
    raise

# ── Project root on sys.path ──────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from database import SessionLocal, Property, Lead
    _DB_AVAILABLE = True
except Exception as _e:
    print(f"[fire_reports] IMPORT ERROR (database): {_e}", flush=True)
    _DB_AVAILABLE = False
    SessionLocal = Property = Lead = None  # type: ignore

try:
    from scrapers.base_scraper import BaseScraper
except ImportError:
    class BaseScraper:  # type: ignore
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("FireReportsScraper")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
REQUEST_DELAY = 2  # seconds between HTTP requests

# Keywords that confirm a RESIDENTIAL structure fire
RESIDENTIAL_KEYWORDS = [
    "structure fire", "house fire", "residential fire", "dwelling fire",
    "apartment fire", "condo fire", "home fire", "residential structure",
    "single family", "multi-family", "townhouse fire", "row house fire",
]

# Keywords that EXCLUDE a result
EXCLUDE_KEYWORDS = [
    "brush fire", "car fire", "vehicle fire", "wildfire", "grass fire",
    "commercial fire", "warehouse fire", "industrial fire",
    "press release", "announcement", "fundraiser", "award", "promotion",
    "training", "drill", "ceremony", "hiring", "recruitment", "job opening",
    "fire prevention", "smoke detector", "safety tip",
]

STREET_SUFFIXES = (
    "Street", "St", "Avenue", "Ave", "Road", "Rd", "Boulevard", "Blvd",
    "Drive", "Dr", "Lane", "Ln", "Way", "Court", "Ct", "Place", "Pl",
    "Terrace", "Ter", "Circle", "Cir", "Highway", "Hwy", "Parkway", "Pkwy",
    "Trail", "Trl", "Loop", "Run",
)

ADDRESS_RE = re.compile(
    r"\b(\d{1,5}\s+[A-Za-z][a-zA-Z]+(?:\s+[A-Za-z][a-zA-Z]+)*\s+"
    r"(?:" + "|".join(STREET_SUFFIXES) + r")\.?)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Source definitions
# ---------------------------------------------------------------------------

SUFFOLK_FRES_URL = (
    "https://www.suffolkcountyny.gov/Departments/FireRescueEmergencyServices"
)

PATCH_SOURCES = [
    ("Babylon",      "https://patch.com/new-york/babylon/local-news"),
    ("Brookhaven",   "https://patch.com/new-york/brookhaven/local-news"),
    ("Huntington",   "https://patch.com/new-york/huntington/local-news"),
    ("Islip",        "https://patch.com/new-york/islip/local-news"),
    ("Smithtown",    "https://patch.com/new-york/smithtown/local-news"),
    ("Southampton",  "https://patch.com/new-york/southampton/local-news"),
    ("EastHampton",  "https://patch.com/new-york/east-hampton/local-news"),
]

NEWSDAY_SEARCH_URL = (
    "https://www.newsday.com/search?q=house+fire+suffolk+county&sort=date"
)

FACEBOOK_FD_PAGES = [
    ("Brookhaven FD",  "https://mbasic.facebook.com/BrookhavenFD",    "Brookhaven", "Suffolk"),
    ("Huntington FD",  "https://mbasic.facebook.com/HuntingtonFD",    "Huntington", "Suffolk"),
    ("Babylon FD",     "https://mbasic.facebook.com/babylonfd",        "Babylon",    "Suffolk"),
    ("Islip FD",       "https://mbasic.facebook.com/IslipFireDept",    "Islip",      "Suffolk"),
    ("Smithtown FD",   "https://mbasic.facebook.com/SmithtownFire",    "Smithtown",  "Suffolk"),
]

GA_FD_SOURCES = [
    {
        "name":   "Atlanta Fire Rescue",
        "url":    "https://www.atlantaga.gov/government/departments/fire-rescue/news",
        "state":  "GA",
        "county": "Fulton",
    },
    {
        "name":   "Gwinnett County Fire",
        "url":    "https://www.gwinnettcounty.com/web/gwinnett/departments/fireandems",
        "state":  "GA",
        "county": "Gwinnett",
    },
    {
        "name":   "Cobb County Fire",
        "url":    "https://www.cobbcounty.org/fire",
        "state":  "GA",
        "county": "Cobb",
    },
    {
        "name":   "DeKalb County Fire Rescue",
        "url":    "https://www.dekalbcountyga.gov/fire-rescue",
        "state":  "GA",
        "county": "DeKalb",
    },
    {
        "name":   "Chatham Emergency Services",
        "url":    "https://www.chathamemergency.org",
        "state":  "GA",
        "county": "Chatham",
    },
    {
        "name":   "Athens-Clarke County Fire",
        "url":    "https://www.accgov.com/fire",
        "state":  "GA",
        "county": "Clarke",
    },
]

# ---------------------------------------------------------------------------
# Scraper class
# ---------------------------------------------------------------------------

class FireReportsScraper(BaseScraper):
    """
    Scrapes real residential fire incidents from Suffolk County NY and
    six Georgia counties.  Saves every incident directly as a lead
    (parcel_id=NULL when no property match exists).
    """

    def __init__(self):
        self._headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        # Keep backward-compatible alias
        self.headers = self._headers
        self._session = requests.Session()
        self._session.headers.update(self._headers)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scrape(self) -> list[dict]:
        """Main entry point."""
        print("[fire_reports] ============================================================", flush=True)
        print("[fire_reports] STARTING FIRE REPORTS SCRAPER", flush=True)
        print(f"[fire_reports] DB available: {_DB_AVAILABLE}", flush=True)

        incidents: list[dict] = []

        # ── New York — Suffolk County ──────────────────────────────────────
        print("\n[fire_reports] --- Suffolk County FRES ---", flush=True)
        fres = self._scrape_suffolk_fres()
        print(f"[fire_reports] FRES: {len(fres)} residential incident(s) found", flush=True)
        incidents.extend(fres)

        print("\n[fire_reports] --- Patch.com (Suffolk towns) ---", flush=True)
        patch = self._scrape_patch()
        print(f"[fire_reports] Patch.com: {len(patch)} residential incident(s) found", flush=True)
        incidents.extend(patch)

        print("\n[fire_reports] --- Newsday ---", flush=True)
        newsday = self._scrape_newsday()
        print(f"[fire_reports] Newsday: {len(newsday)} residential incident(s) found", flush=True)
        incidents.extend(newsday)

        # ── Georgia ───────────────────────────────────────────────────────
        print("\n[fire_reports] --- Georgia fire departments ---", flush=True)
        ga = self._scrape_georgia()
        print(f"[fire_reports] Georgia FDs: {len(ga)} residential incident(s) found", flush=True)
        incidents.extend(ga)

        # ── Facebook fallback (only if NY sources returned nothing) ───────
        ny_count = sum(1 for i in incidents if i.get("state") == "NY")
        if ny_count == 0:
            print("\n[fire_reports] NY sources returned 0 — trying Facebook FD pages (fallback)...", flush=True)
            fb = self._scrape_facebook_fds()
            print(f"[fire_reports] Facebook: {len(fb)} residential incident(s) found", flush=True)
            incidents.extend(fb)

        # ── Filter ────────────────────────────────────────────────────────
        before = len(incidents)
        incidents = [i for i in incidents if self._is_valid_incident(i)]
        print(
            f"\n[fire_reports] After filter: {len(incidents)}/{before} incidents are "
            f"valid residential fires with street numbers",
            flush=True,
        )

        if not incidents:
            print("[fire_reports] No valid residential fire incidents found from any source.", flush=True)
            return []

        # ── Save ──────────────────────────────────────────────────────────
        saved = self._save_leads(incidents)
        print(f"[fire_reports] Saved {saved} new lead(s) to database.", flush=True)
        return incidents

    # ------------------------------------------------------------------
    # Source scrapers
    # ------------------------------------------------------------------

    def _scrape_suffolk_fres(self) -> list[dict]:
        """Scrape Suffolk County FRES news feed for residential fire incidents."""
        incidents = []
        try:
            print(f"[fire_reports] GET {SUFFOLK_FRES_URL}", flush=True)
            r = self._session.get(SUFFOLK_FRES_URL, timeout=15)
            print(f"[fire_reports] FRES HTTP {r.status_code}", flush=True)
            if r.status_code != 200:
                return incidents
            soup = BeautifulSoup(r.text, "html.parser")
            # Look for news items / press release links
            items = soup.find_all(
                ["article", "div", "li"],
                class_=re.compile(r"news|article|press|release|item|post", re.I),
            )
            if not items:
                items = soup.find_all(["h2", "h3", "h4", "a"])
            print(f"[fire_reports] FRES: {len(items)} candidate elements", flush=True)
            for item in items:
                text = item.get_text(separator=" ", strip=True)
                if not self._mentions_residential_fire(text):
                    continue
                if self._is_excluded(text):
                    continue
                address = self._extract_address(text)
                if not address:
                    link = item.find("a", href=True) if hasattr(item, "find") else None
                    if link:
                        address = self._fetch_article_address(link["href"])
                if not address:
                    continue
                incidents.append({
                    "date":          self._extract_date(text),
                    "address":       address,
                    "incident_type": "Residential Structure Fire",
                    "source":        "suffolk_fres",
                    "state":         "NY",
                    "county":        "Suffolk",
                    "detail_text":   text[:300],
                })
                print(f"[fire_reports]   FRES hit: {address}", flush=True)
        except Exception as exc:
            print(f"[fire_reports] FRES error: {exc}", flush=True)
            _tb.print_exc()
        return incidents

    def _scrape_patch(self) -> list[dict]:
        """Scrape Patch.com town pages for residential fire articles."""
        incidents = []
        for town, url in PATCH_SOURCES:
            try:
                print(f"[fire_reports] Patch/{town} GET {url}", flush=True)
                r = self._session.get(url, timeout=15)
                print(f"[fire_reports] Patch/{town}: HTTP {r.status_code}", flush=True)
                if r.status_code != 200:
                    time.sleep(REQUEST_DELAY)
                    continue
                soup = BeautifulSoup(r.text, "html.parser")
                cards = soup.find_all(
                    ["article", "div"],
                    class_=re.compile(r"card|article|story|item|post", re.I),
                )
                if not cards:
                    cards = soup.find_all(["h2", "h3"])
                print(f"[fire_reports] Patch/{town}: {len(cards)} cards", flush=True)
                for card in cards:
                    text = card.get_text(separator=" ", strip=True)
                    if not self._mentions_residential_fire(text):
                        continue
                    if self._is_excluded(text):
                        continue
                    address = self._extract_address(text)
                    if not address:
                        link = card.find("a", href=True) if hasattr(card, "find") else None
                        if link:
                            href = link.get("href", "")
                            if not href.startswith("http"):
                                href = "https://patch.com" + href
                            address = self._fetch_article_address(href)
                    if not address:
                        continue
                    incidents.append({
                        "date":          self._extract_date(text),
                        "address":       address,
                        "incident_type": "Residential Structure Fire",
                        "source":        f"patch_{town.lower()}",
                        "state":         "NY",
                        "county":        "Suffolk",
                        "detail_text":   text[:300],
                    })
                    print(f"[fire_reports]   Patch/{town} hit: {address}", flush=True)
                time.sleep(REQUEST_DELAY)
            except Exception as exc:
                print(f"[fire_reports] Patch/{town} error: {exc}", flush=True)
        return incidents

    def _scrape_newsday(self) -> list[dict]:
        """Scrape Newsday search results for Suffolk County house fires."""
        incidents = []
        try:
            print(f"[fire_reports] Newsday GET {NEWSDAY_SEARCH_URL}", flush=True)
            r = self._session.get(NEWSDAY_SEARCH_URL, timeout=15)
            print(f"[fire_reports] Newsday HTTP {r.status_code}", flush=True)
            if r.status_code != 200:
                return incidents
            soup = BeautifulSoup(r.text, "html.parser")
            results = soup.find_all(
                ["article", "div", "li"],
                class_=re.compile(r"result|article|story|card|item", re.I),
            )
            if not results:
                results = soup.find_all(["h2", "h3", "h4"])
            print(f"[fire_reports] Newsday: {len(results)} result elements", flush=True)
            for result in results:
                text = result.get_text(separator=" ", strip=True)
                if not self._mentions_residential_fire(text):
                    continue
                if self._is_excluded(text):
                    continue
                address = self._extract_address(text)
                if not address:
                    link = result.find("a", href=True) if hasattr(result, "find") else None
                    if link:
                        href = link.get("href", "")
                        if not href.startswith("http"):
                            href = "https://www.newsday.com" + href
                        address = self._fetch_article_address(href)
                if not address:
                    continue
                incidents.append({
                    "date":          self._extract_date(text),
                    "address":       address,
                    "incident_type": "Residential Structure Fire",
                    "source":        "newsday",
                    "state":         "NY",
                    "county":        "Suffolk",
                    "detail_text":   text[:300],
                })
                print(f"[fire_reports]   Newsday hit: {address}", flush=True)
        except Exception as exc:
            print(f"[fire_reports] Newsday error: {exc}", flush=True)
        return incidents

    def _scrape_georgia(self) -> list[dict]:
        """Scrape Georgia fire department news pages."""
        incidents = []
        for fd in GA_FD_SOURCES:
            try:
                print(f"[fire_reports] GA/{fd['county']} GET {fd['url']}", flush=True)
                r = self._session.get(fd["url"], timeout=15)
                print(f"[fire_reports] GA/{fd['county']}: HTTP {r.status_code}", flush=True)
                if r.status_code != 200:
                    time.sleep(REQUEST_DELAY)
                    continue
                soup = BeautifulSoup(r.text, "html.parser")
                items = soup.find_all(
                    ["article", "div", "li", "h2", "h3"],
                    class_=re.compile(r"news|article|story|card|item|post|release", re.I),
                )
                if not items:
                    items = soup.find_all(["h2", "h3", "h4"])
                print(f"[fire_reports] GA/{fd['county']}: {len(items)} candidate elements", flush=True)
                for item in items:
                    text = item.get_text(separator=" ", strip=True)
                    if not self._mentions_residential_fire(text):
                        continue
                    if self._is_excluded(text):
                        continue
                    address = self._extract_address(text)
                    if not address:
                        link = item.find("a", href=True) if hasattr(item, "find") else None
                        if link:
                            href = link.get("href", "")
                            if not href.startswith("http"):
                                base = re.match(r"https?://[^/]+", fd["url"])
                                href = (base.group(0) if base else "") + href
                            address = self._fetch_article_address(href)
                    if not address:
                        continue
                    incidents.append({
                        "date":          self._extract_date(text),
                        "address":       address,
                        "incident_type": "Residential Structure Fire",
                        "source":        f"ga_{fd['county'].lower()}",
                        "state":         "GA",
                        "county":        fd["county"],
                        "detail_text":   text[:300],
                    })
                    print(f"[fire_reports]   GA/{fd['county']} hit: {address}", flush=True)
                time.sleep(REQUEST_DELAY)
            except Exception as exc:
                print(f"[fire_reports] GA/{fd['county']} error: {exc}", flush=True)
        return incidents

    def _scrape_facebook_fds(self) -> list[dict]:
        """Fallback: scrape public FD Facebook pages via mbasic.facebook.com."""
        incidents = []
        for name, url, town, county in FACEBOOK_FD_PAGES:
            try:
                print(f"[fire_reports] Facebook/{name} GET {url}", flush=True)
                r = self._session.get(url, timeout=15)
                print(f"[fire_reports] Facebook/{name}: HTTP {r.status_code}", flush=True)
                if r.status_code != 200:
                    time.sleep(REQUEST_DELAY)
                    continue
                soup = BeautifulSoup(r.text, "html.parser")
                posts = soup.find_all(["div", "article", "section"])
                print(f"[fire_reports] Facebook/{name}: {len(posts)} post elements", flush=True)
                for post in posts:
                    text = post.get_text(separator=" ", strip=True)
                    if len(text) < 30:
                        continue
                    if not self._mentions_residential_fire(text):
                        continue
                    if self._is_excluded(text):
                        continue
                    address = self._extract_address(text)
                    if not address:
                        continue
                    incidents.append({
                        "date":          self._extract_date(text),
                        "address":       address,
                        "incident_type": "Residential Structure Fire",
                        "source":        f"facebook_{name.lower().replace(' ', '_')}",
                        "state":         "NY",
                        "county":        county,
                        "detail_text":   text[:300],
                    })
                    print(f"[fire_reports]   FB/{name} hit: {address}", flush=True)
                time.sleep(REQUEST_DELAY)
            except Exception as exc:
                print(f"[fire_reports] Facebook/{name} error: {exc}", flush=True)
        return incidents

    def _fetch_article_address(self, url: str) -> str:
        """Fetch a linked article and try to extract a street address from it."""
        if not url or not url.startswith("http"):
            return ""
        try:
            r = self._session.get(url, timeout=10)
            if r.status_code != 200:
                return ""
            soup = BeautifulSoup(r.text, "html.parser")
            body = soup.find(
                ["article", "main", "div"],
                class_=re.compile(r"article|body|content|story|text", re.I),
            )
            text = (body or soup).get_text(separator=" ", strip=True)
            return self._extract_address(text)
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Filtering helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _mentions_residential_fire(text: str) -> bool:
        lower = text.lower()
        return any(kw in lower for kw in RESIDENTIAL_KEYWORDS)

    @staticmethod
    def _is_excluded(text: str) -> bool:
        lower = text.lower()
        return any(kw in lower for kw in EXCLUDE_KEYWORDS)

    @staticmethod
    def _extract_address(text: str) -> str:
        m = ADDRESS_RE.search(text)
        return m.group(0).strip() if m else ""

    @staticmethod
    def _extract_date(text: str) -> str:
        patterns = [
            r"\b(\d{4}-\d{2}-\d{2})\b",
            r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b",
            r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\.?\s+\d{1,2},?\s+\d{4})\b",
        ]
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                return m.group(1)
        return datetime.date.today().strftime("%Y-%m-%d")

    @staticmethod
    def _is_valid_incident(inc: dict) -> bool:
        """
        Returns True only if:
          - address starts with a digit (has a street number)
          - text mentions a residential fire keyword
          - text does not match exclusion keywords
        """
        address = inc.get("address", "").strip()
        if not address or not re.match(r"^\d", address):
            return False
        combined = (
            inc.get("incident_type", "") + " " + inc.get("detail_text", "")
        ).lower()
        if not any(kw in combined for kw in RESIDENTIAL_KEYWORDS):
            return False
        if any(kw in combined for kw in EXCLUDE_KEYWORDS):
            return False
        return True

    @staticmethod
    def _is_residential_structure_fire(incident_type: str) -> bool:
        """Backward-compatible alias."""
        lower = incident_type.lower()
        return any(kw in lower for kw in RESIDENTIAL_KEYWORDS)

    @staticmethod
    def _address_matches_property(scraped_address: str, prop_address: str) -> bool:
        a = scraped_address.strip().upper()
        b = prop_address.strip().upper()
        return a in b or b in a

    # Backward-compatible aliases used by older callers
    _extract_address_from_text = staticmethod(lambda text: (
        ADDRESS_RE.search(text).group(0).strip()
        if ADDRESS_RE.search(text) else ""
    ))
    _extract_date_from_text = staticmethod(lambda text: (
        next(
            (re.search(p, text, re.IGNORECASE).group(1)
             for p in [r"\b(\d{4}-\d{2}-\d{2})\b", r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b"]
             if re.search(p, text, re.IGNORECASE)),
            datetime.date.today().strftime("%Y-%m-%d"),
        )
    ))

    # ------------------------------------------------------------------
    # Database persistence
    # ------------------------------------------------------------------

    def _save_leads(self, incidents: list[dict]) -> int:
        """
        Saves every incident directly as a lead — no property match required.
        If a matching property record exists it is linked; otherwise parcel_id=NULL
        and owner_name='Unknown - needs lookup'.
        Returns number of new leads inserted.
        """
        if not _DB_AVAILABLE or not SessionLocal:
            print("[fire_reports] _save_leads: DB not available.", flush=True)
            return 0

        print(f"[fire_reports] _save_leads: attempting to save {len(incidents)} incident(s).", flush=True)
        saved_count = 0
        session = SessionLocal()
        try:
            try:
                properties = session.query(Property).all() if Property else []
                print(f"[fire_reports] Properties in DB: {len(properties)}", flush=True)
            except Exception as prop_exc:
                print(f"[fire_reports] Could not load properties: {prop_exc}", flush=True)
                properties = []

            for idx, inc in enumerate(incidents):
                is_residential = self._is_residential_structure_fire(
                    inc.get("incident_type", "") + " " + inc.get("detail_text", "")
                )
                address = inc.get("address", "").strip()
                if not address:
                    print(f"[fire_reports]   incident[{idx}]: no address — skipping.", flush=True)
                    continue

                # Optional property match
                matched_prop = None
                for prop in properties:
                    if self._address_matches_property(address, prop.address or ""):
                        matched_prop = prop
                        print(f"[fire_reports]   MATCH: address matched property (owner: {prop.owner_name})", flush=True)
                        break

                # Deduplication
                try:
                    addr_key = address[:40]
                    existing = (
                        session.query(Lead)
                        .filter(
                            Lead.address.ilike(f"%{addr_key}%"),
                            Lead.source == "fire_report",
                        )
                        .first()
                    )
                    if existing:
                        print(f"[fire_reports]   DUPLICATE: lead already exists for '{address[:50]}' — skipping.", flush=True)
                        continue
                except Exception as dup_exc:
                    print(f"[fire_reports]   Dedup check error: {dup_exc}", flush=True)

                raw_data = {
                    "scraped_address":               address,
                    "scraped_date":                  inc.get("date", ""),
                    "incident_type":                 inc.get("incident_type", ""),
                    "is_residential_structure_fire": is_residential,
                    "owner_name":                    matched_prop.owner_name if matched_prop else "Unknown - needs lookup",
                    "owner_mailing_address":         getattr(matched_prop, "owner_mailing_address", "") if matched_prop else "",
                    "source_name":                   inc.get("source", ""),
                    "detail_text":                   inc.get("detail_text", "")[:300],
                }
                lead = Lead(
                    address=matched_prop.address if matched_prop else address,
                    parcel_id=matched_prop.parcel_id if matched_prop else None,
                    source="fire_report",
                    raw_data=json.dumps(raw_data),
                    score=0.85 if is_residential else 0.55,
                    status="new",
                    state=inc.get("state", getattr(matched_prop, "state", "NY") if matched_prop else "NY"),
                    county=inc.get("county", getattr(matched_prop, "county", "Suffolk") if matched_prop else "Suffolk"),
                )
                try:
                    session.add(lead)
                    session.flush()
                    saved_count += 1
                    print(
                        f"[fire_reports]   SAVED: lead id={lead.id} | {address[:60]} "
                        f"| score={lead.score} | parcel={lead.parcel_id or 'NULL'} "
                        f"| {lead.state}/{lead.county}",
                        flush=True,
                    )
                except Exception as ins_exc:
                    session.rollback()
                    print(f"[fire_reports]   INSERT ERROR for '{address[:50]}': {ins_exc}", flush=True)
                    _tb.print_exc()
                    continue

            session.commit()
            print(f"[fire_reports] _save_leads: committed. Total new leads saved: {saved_count}", flush=True)
        except Exception as exc:
            session.rollback()
            print(f"[fire_reports] _save_leads: FATAL error — {exc}", flush=True)
            _tb.print_exc()
        finally:
            session.close()
        return saved_count

    # ------------------------------------------------------------------
    # Fallback mock data (only used when --mock flag is passed)
    # ------------------------------------------------------------------

    @staticmethod
    def _get_mock_incidents() -> list[dict]:
        today = datetime.date.today().strftime("%Y-%m-%d")
        return [
            {
                "date": today,
                "address": "123 Main St, Brookhaven, NY 11719",
                "incident_type": "Residential Structure Fire (House Fire)",
                "source": "mock",
                "state": "NY",
                "county": "Suffolk",
                "detail_text": "Firefighters responded to a house fire at 123 Main St Brookhaven",
            },
            {
                "date": today,
                "address": "789 Oak Ave, Babylon, NY 11702",
                "incident_type": "Structure Fire (Residential Dwelling)",
                "source": "mock",
                "state": "NY",
                "county": "Suffolk",
                "detail_text": "Crews battled a residential structure fire at 789 Oak Ave Babylon",
            },
            {
                "date": today,
                "address": "234 Peachtree St NW, Atlanta, GA 30303",
                "incident_type": "Residential Structure Fire (House Fire)",
                "source": "mock",
                "state": "GA",
                "county": "Fulton",
                "detail_text": "Atlanta Fire Rescue responded to a house fire at 234 Peachtree St NW",
            },
        ]


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Fire reports scraper — Suffolk County NY + Georgia"
    )
    parser.add_argument(
        "--mock", action="store_true",
        help="Use mock data instead of live scraping (for testing DB save path)",
    )
    args = parser.parse_args()
    scraper = FireReportsScraper()
    if args.mock:
        print("[fire_reports] Running in MOCK mode.", flush=True)
        mock_incidents = scraper._get_mock_incidents()
        scraper._save_leads(mock_incidents)
    else:
        scraper.scrape()
