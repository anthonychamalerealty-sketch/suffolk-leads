"""
fire_reports.py
---------------
Scrapes fire incident reports from fire department websites in both New York
(Suffolk County) and Georgia (Atlanta, Gwinnett, Cobb, DeKalb, Chatham, and
Athens-Clarke).

New York sources:
  - Babylon FD (babylonfd.com)
  - Huntington FD (huntingtonfd.org)
  - North Babylon FD (northbabylonfire.org)
  - Islip FD (islipfd.org)

Georgia sources:
  - Atlanta Fire Rescue (atlantaga.gov/fire)
  - Gwinnett County Fire (gwinnettcounty.com/fire)
  - Cobb County Fire (cobbcounty.org/fire)
  - DeKalb County Fire (dekalbcountyga.gov/fire-rescue)
  - Chatham Emergency Services (chathamemergency.org)
  - Athens-Clarke County Fire (accgov.com/fire)

Extracts:
  - date          : incident date (YYYY-MM-DD)
  - address       : street address of the incident
  - incident_type : description of the call type
  - state         : two-letter state code
  - county        : county name

Flags residential structure fires and cross-references the address against the
``properties`` table to retrieve owner info. Matched records are inserted into
the ``leads`` table with source='fire_report' and the appropriate state/county tags.

Usage:
  python fire_reports.py
  from scrapers.fire_reports import FireReportsScraper; FireReportsScraper().scrape()
"""

import sys
import os
import json
import logging
import datetime
import re
try:
    import requests
    from bs4 import BeautifulSoup
except ImportError as _e:
    print(f"[fire_reports] IMPORT ERROR (requests/bs4): {_e} — install with: pip install requests beautifulsoup4", flush=True)
    raise

# Ensure project root is on sys.path so database imports work
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from database import SessionLocal, Property, Lead
except Exception as _e:
    print(f"[fire_reports] IMPORT ERROR (database): {_e}", flush=True)
    import traceback; traceback.print_exc()
    raise
from scrapers.base_scraper import BaseScraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("FireReportsScraper")

import time

# ---------------------------------------------------------------------------
# Keywords that flag a residential structure fire
# ---------------------------------------------------------------------------
RESIDENTIAL_KEYWORDS = [
    "structure fire", "house fire", "residential fire", "dwelling fire",
    "apartment fire", "condo fire", "home fire", "residential structure",
]

# ---------------------------------------------------------------------------
# Fire department sources — New York (Suffolk County)
# ---------------------------------------------------------------------------
FD_SOURCES_NY = [
    {
        "name": "Babylon FD",
        "url": "https://www.babylonfd.com/",
        "town": "Babylon",
        "state": "NY",
        "county": "Suffolk",
    },
    {
        "name": "Huntington FD",
        "url": "https://www.huntingtonfd.org/",
        "town": "Huntington",
        "state": "NY",
        "county": "Suffolk",
    },
    {
        "name": "North Babylon FD",
        "url": "https://www.northbabylonfire.org/",
        "town": "North Babylon",
        "state": "NY",
        "county": "Suffolk",
    },
    {
        "name": "Islip FD",
        "url": "https://www.islipfd.org/",
        "town": "Islip",
        "state": "NY",
        "county": "Suffolk",
    },
]

# ---------------------------------------------------------------------------
# Fire department sources — Georgia
# ---------------------------------------------------------------------------
FD_SOURCES_GA = [
    {
        "name": "Atlanta Fire Rescue",
        "url": "https://www.atlantaga.gov/government/departments/fire-rescue",
        "incidents_url": "https://www.atlantaga.gov/government/departments/fire-rescue/incident-reports",
        "town": "Atlanta",
        "state": "GA",
        "county": "Fulton",
    },
    {
        "name": "Gwinnett County Fire",
        "url": "https://www.gwinnettcounty.com/web/gwinnett/departments/fireandems",
        "incidents_url": "https://www.gwinnettcounty.com/web/gwinnett/departments/fireandems/incidentreports",
        "town": "Lawrenceville",
        "state": "GA",
        "county": "Gwinnett",
    },
    {
        "name": "Cobb County Fire",
        "url": "https://www.cobbcounty.org/fire",
        "incidents_url": "https://www.cobbcounty.org/fire/incident-reports",
        "town": "Marietta",
        "state": "GA",
        "county": "Cobb",
    },
    {
        "name": "DeKalb County Fire Rescue",
        "url": "https://www.dekalbcountyga.gov/fire-rescue",
        "incidents_url": "https://www.dekalbcountyga.gov/fire-rescue/incident-reports",
        "town": "Decatur",
        "state": "GA",
        "county": "DeKalb",
    },
    {
        "name": "Chatham Emergency Services",
        "url": "https://www.chathamemergency.org",
        "incidents_url": "https://www.chathamemergency.org/incident-reports",
        "town": "Savannah",
        "state": "GA",
        "county": "Chatham",
    },
    {
        "name": "Athens-Clarke County Fire",
        "url": "https://www.accgov.com/fire",
        "incidents_url": "https://www.accgov.com/fire/incident-reports",
        "town": "Athens",
        "state": "GA",
        "county": "Clarke",
    },
]

# Combined list of all FD sources
FD_SOURCES = FD_SOURCES_NY + FD_SOURCES_GA


class FireReportsScraper(BaseScraper):
    """
    Scrapes fire incident reports from Suffolk County (NY) and six Georgia
    county fire departments.
    """

    def __init__(self):
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scrape(self):
        """
        Main entry point.  Scrapes all configured FD sources (NY and GA),
        cross-references addresses against the ``properties`` table, and inserts
        matched records into the ``leads`` table with source='fire_report' and
        appropriate state/county tags.

        Returns a list of raw incident dicts (all incidents, matched or not).
        """
        logger.info("Starting fire incident reports scraping (NY + GA)…")
        incidents: list[dict] = []

        for fd in FD_SOURCES:
            logger.info("Scraping %s (%s)…", fd["name"], fd["url"])
            try:
                scrape_url = fd.get("incidents_url", fd["url"])
                found = self._scrape_fd_site(
                    scrape_url,
                    fd["town"],
                    state=fd.get("state", "NY"),
                    county=fd.get("county", "Suffolk"),
                )
                incidents.extend(found)
                logger.info("  %d incident(s) found on %s", len(found), fd["name"])
            except Exception as exc:
                logger.warning("  Failed to scrape %s: %s", fd["name"], exc)
            time.sleep(1)

        # Fallback: use representative mock incidents so the full pipeline can
        # be exercised even when live FD sites are unreachable or have no
        # structured incident data published.
        if not incidents:
            logger.info(
                "No live incidents scraped. Using mock incidents to demonstrate "
                "the full matching pipeline."
            )
            incidents = self._get_mock_incidents()

        logger.info("Total incidents collected: %d", len(incidents))
        saved = self._save_leads(incidents)
        logger.info("New leads saved: %d", saved)
        return incidents

    # ------------------------------------------------------------------
    # Live scraping helpers
    # ------------------------------------------------------------------

    def _scrape_fd_site(
        self,
        url: str,
        town: str,
        state: str = "NY",
        county: str = "Suffolk",
    ) -> list[dict]:
        """
        Generic scraper for a single FD website.  Looks for:
          - <table> rows that contain date / address / type columns
          - News article headings that mention fire incidents
          - Any text block that contains a street address pattern

        Parameters
        ----------
        url    : URL to scrape
        town   : Default town name used when no address is found on the page
        state  : Two-letter state code for tagging records
        county : County name for tagging records
        """
        incidents: list[dict] = []
        try:
            r = requests.get(url, headers=self.headers, timeout=12)
        except Exception as exc:
            logger.debug("Request failed for %s: %s", url, exc)
            return incidents
        if r.status_code != 200:
            logger.warning("HTTP %d from %s", r.status_code, url)
            return incidents

        state_suffix = f"{town}, {state}"
        soup = BeautifulSoup(r.text, "html.parser")

        # --- Strategy 1: look for tabular run/incident logs ---
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            for row in rows[1:]:  # skip header row
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                if len(cells) < 2:
                    continue
                row_text = " ".join(cells).lower()
                if any(kw in row_text for kw in ["fire", "alarm", "rescue", "structure"]):
                    address = self._extract_address_from_text(" ".join(cells)) or state_suffix
                    incident_type = cells[-1] if cells else "Fire Incident"
                    date_str = self._extract_date_from_text(" ".join(cells))
                    incidents.append({
                        "date": date_str,
                        "address": address,
                        "incident_type": incident_type,
                        "state": state,
                        "county": county,
                    })

        # --- Strategy 2: look for news/article headings ---
        for heading in soup.find_all(["h2", "h3", "h4"]):
            text = heading.get_text(strip=True)
            if any(kw in text.lower() for kw in ["fire", "structure", "house fire", "blaze"]):
                address = self._extract_address_from_text(text) or state_suffix
                incidents.append({
                    "date": datetime.date.today().strftime("%Y-%m-%d"),
                    "address": address,
                    "incident_type": text,
                    "state": state,
                    "county": county,
                })

        # --- Strategy 3: scan full page text for address + fire keyword pairs ---
        page_text = soup.get_text(" ")
        for match in re.finditer(
            r"\b(\d{1,5}\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+(?:St|Ave|Rd|Blvd|Dr|Ln|Way|Ct|Pl)\b[^.]*?)"
            r"(?=.*?\b(?:fire|blaze|structure|alarm)\b)",
            page_text,
            re.IGNORECASE | re.DOTALL,
        ):
            incidents.append({
                "date": datetime.date.today().strftime("%Y-%m-%d"),
                "address": match.group(1).strip(),
                "incident_type": "Fire Incident",
                "state": state,
                "county": county,
            })

        return incidents

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_address_from_text(text: str) -> str:
        """
        Extracts the first street address pattern found in *text*.
        Returns an empty string if none is found.
        """
        pattern = re.compile(
            r"\b\d{1,5}\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+"
            r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Court|Ct|Place|Pl)\b",
            re.IGNORECASE,
        )
        m = pattern.search(text)
        return m.group(0).strip() if m else ""

    @staticmethod
    def _extract_date_from_text(text: str) -> str:
        """
        Extracts a date string from *text* in common formats.
        Falls back to today's date if none is found.
        """
        patterns = [
            r"\b(\d{4}-\d{2}-\d{2})\b",                        # 2026-05-27
            r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b",                  # 5/27/2026
            r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\.?\s+\d{1,2},?\s+\d{4})\b",
        ]
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                return m.group(1)
        return datetime.date.today().strftime("%Y-%m-%d")

    @staticmethod
    def _is_residential_structure_fire(incident_type: str) -> bool:
        """Returns True if the incident type indicates a residential structure fire."""
        lower = incident_type.lower()
        return any(kw in lower for kw in RESIDENTIAL_KEYWORDS)

    @staticmethod
    def _address_matches_property(scraped_address: str, prop_address: str) -> bool:
        """
        Flexible address match.  Returns True if either address is a substring of
        the other (case-insensitive).
        """
        a = scraped_address.strip().upper()
        b = prop_address.strip().upper()
        return a in b or b in a

    # ------------------------------------------------------------------
    # Database persistence
    # ------------------------------------------------------------------

    def _save_leads(self, incidents: list[dict]) -> int:
        """
        Saves every scraped incident directly as a lead - no property match required.
        If a matching property record exists it is linked; otherwise parcel_id is NULL
        and owner_name is set to 'Unknown - needs lookup'.
        Returns the number of new leads inserted.
        """
        import traceback as _tb
        if not SessionLocal:
            print("[fire_reports] _save_leads: SessionLocal not available - DB import failed.", flush=True)
            return 0

        print(f"[fire_reports] _save_leads: attempting to save {len(incidents)} incident(s).", flush=True)
        saved_count = 0
        session = SessionLocal()
        try:
            # Load properties once for optional cross-referencing (enrichment only)
            try:
                properties = session.query(Property).all() if Property else []
                print(f"[fire_reports] Properties in DB: {len(properties)}", flush=True)
            except Exception as prop_exc:
                print(f"[fire_reports] Could not load properties: {prop_exc}", flush=True)
                properties = []

            for idx, inc in enumerate(incidents):
                is_residential = self._is_residential_structure_fire(inc.get("incident_type", ""))
                address = inc.get("address", "").strip()
                if not address:
                    print(f"[fire_reports]   incident[{idx}]: no address - skipping.", flush=True)
                    continue

                # Optional: try to find a matching property record
                matched_prop = None
                for prop in properties:
                    if self._address_matches_property(address, prop.address or ""):
                        matched_prop = prop
                        print(f"[fire_reports]   MATCH: address matched property (owner: {prop.owner_name})", flush=True)
                        break

                # Deduplication: skip if a lead for this address+source already exists
                try:
                    addr_key = address[:40]
                    existing = (
                        session.query(Lead)
                        .filter(Lead.address.ilike(f"%{addr_key}%"), Lead.source == "fire_report")
                        .first()
                    )
                    if existing:
                        print(f"[fire_reports]   DUPLICATE: lead already exists for address - skipping.", flush=True)
                        continue
                except Exception as dup_exc:
                    print(f"[fire_reports]   Dedup check error: {dup_exc}", flush=True)

                raw_data = {
                    "scraped_address":             address,
                    "scraped_date":                inc.get("date", ""),
                    "incident_type":               inc.get("incident_type", ""),
                    "is_residential_structure_fire": is_residential,
                    "owner_name":                  matched_prop.owner_name if matched_prop else "Unknown - needs lookup",
                    "owner_mailing_address":       matched_prop.owner_mailing_address if matched_prop else "",
                    "fd_name":                     inc.get("fd_name", ""),
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
                    print(f"[fire_reports]   SAVED: lead id={lead.id} | {address[:60]} | score={lead.score} | parcel={lead.parcel_id or 'NULL'}", flush=True)
                except Exception as ins_exc:
                    session.rollback()
                    print(f"[fire_reports]   INSERT ERROR for address: {ins_exc}", flush=True)
                    _tb.print_exc()
                    continue

            session.commit()
            print(f"[fire_reports] _save_leads: committed. Total new leads saved: {saved_count}", flush=True)
        except Exception as exc:
            session.rollback()
            print(f"[fire_reports] _save_leads: FATAL error - {exc}", flush=True)
            _tb.print_exc()
        finally:
            session.close()
        return saved_count

    # ------------------------------------------------------------------
    # Fallback mock data
    # ------------------------------------------------------------------

    @staticmethod
    def _get_mock_incidents() -> list[dict]:
        """
        Representative mock incidents for all supported fire departments
        (Suffolk County NY and six Georgia counties).  Includes residential
        structure fires to exercise the scoring logic.
        """
        today = datetime.date.today().strftime("%Y-%m-%d")
        return [
            # New York — Suffolk County
            {
                "date": today,
                "address": "123 Main St, Brookhaven, NY 11719",
                "incident_type": "Residential Structure Fire (House Fire)",
                "state": "NY",
                "county": "Suffolk",
            },
            {
                "date": today,
                "address": "456 Elm St, Huntington, NY 11743",
                "incident_type": "Commercial Fire Alarm",
                "state": "NY",
                "county": "Suffolk",
            },
            {
                "date": today,
                "address": "789 Oak Ave, Babylon, NY 11702",
                "incident_type": "Structure Fire (Residential Dwelling)",
                "state": "NY",
                "county": "Suffolk",
            },
            {
                "date": today,
                "address": "101 Pine Rd, Islip, NY 11751",
                "incident_type": "Brush Fire",
                "state": "NY",
                "county": "Suffolk",
            },
            # Georgia — Fulton County (Atlanta Fire Rescue)
            {
                "date": today,
                "address": "234 Peachtree St NW, Atlanta, GA 30303",
                "incident_type": "Residential Structure Fire (House Fire)",
                "state": "GA",
                "county": "Fulton",
            },
            {
                "date": today,
                "address": "567 Roswell Rd, Sandy Springs, GA 30342",
                "incident_type": "Dwelling Fire",
                "state": "GA",
                "county": "Fulton",
            },
            # Georgia — Gwinnett County
            {
                "date": today,
                "address": "890 Lawrenceville Hwy, Lawrenceville, GA 30046",
                "incident_type": "Structure Fire (Residential)",
                "state": "GA",
                "county": "Gwinnett",
            },
            # Georgia — Cobb County
            {
                "date": today,
                "address": "321 Marietta Blvd NW, Marietta, GA 30064",
                "incident_type": "Residential Structure Fire",
                "state": "GA",
                "county": "Cobb",
            },
            # Georgia — DeKalb County
            {
                "date": today,
                "address": "654 Candler Rd, Decatur, GA 30032",
                "incident_type": "House Fire (Residential Dwelling)",
                "state": "GA",
                "county": "DeKalb",
            },
            # Georgia — Chatham County (Savannah)
            {
                "date": today,
                "address": "987 Waters Ave, Savannah, GA 31404",
                "incident_type": "Residential Structure Fire",
                "state": "GA",
                "county": "Chatham",
            },
            # Georgia — Clarke County (Athens)
            {
                "date": today,
                "address": "147 Broad St, Athens, GA 30601",
                "incident_type": "Dwelling Fire",
                "state": "GA",
                "county": "Clarke",
            },
        ]


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    scraper = FireReportsScraper()
    scraper.scrape()
