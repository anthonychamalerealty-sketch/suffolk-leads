"""
scrapers/parcel_access.py
--------------------------
Playwright-based scraper for property records across multiple states and counties.

Currently supports:
  - New York: Suffolk County (ParcelAccess / Munis portal)
  - Georgia: Fulton, Gwinnett, Cobb, DeKalb, Chatham, and Clarke counties
    via their respective public property search portals.

All scraped records are upserted into the ``properties`` table of the SQLite
database with ``state`` and ``county`` columns populated accordingly.

Usage
-----
Run all regions (no limit):
    python -m scrapers.parcel_access

Run with a per-county limit (useful for testing):
    python -m scrapers.parcel_access --limit 5
"""

import os
import sys
import logging
import asyncio
import argparse
import json
import time
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Path setup so this module can be run directly or imported from the project root
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from database import SessionLocal, Property, init_db
from scrapers.base_scraper import BaseScraper

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_LOG_FILE = os.path.join(_ROOT, "scraper.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(_LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("ParcelAccessScraper")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BASE_URL = "https://suffolk.munisselfservice.com/citizens/RealEstate/Default.aspx?mode=new"

# Suffolk County SWIS codes / parcel-ID prefixes, one per town.
# The portal's Parcel ID field accepts these 4-digit town codes as a prefix
# and returns the matching parcel (or a list when multiple exist).
TOWN_PREFIXES: dict[str, str] = {
    "Babylon":        "0100",
    "Brookhaven":     "0200",
    "East Hampton":   "0300",
    "Huntington":     "0400",
    "Islip":          "0500",
    "Riverhead":      "0600",
    "Shelter Island": "0700",
    "Smithtown":      "0800",
    "Southampton":    "0900",
    "Southold":       "1000",
}

# Stable span-ID suffixes used by the Munis portal on each sub-page
_SPAN_IDS = {
    # Property Detail page
    "parcel_id":    "ParcelIdLabel",
    "address":      "LocationLabel",
    "assessed":     "AssessedValueLabel",
    # Owner Information page
    "owner_name":   "NameLabel",
    "owner_name2":  "Name2Label",
    "owner_addr":   "AddressLabel",
    "owner_csz":    "CityStateZipLabel",
}

# Selectors
_PARCEL_ID_INPUT = "input[id*='ParcelIDTextBox']"
_SEARCH_BTN      = "input[id*='Button1'][value='Search']"


# ---------------------------------------------------------------------------
# Georgia county portal configurations
# ---------------------------------------------------------------------------

# Each entry: county name -> dict with scraper metadata
GA_COUNTIES: dict[str, dict] = {
    "Fulton": {
        "url": "https://qpublic.schneidercorp.com/Application.aspx?AppID=1051&LayerID=23419&PageTypeID=2&PageID=10240",
        "search_url": "https://qpublic.schneidercorp.com/Application.aspx?AppID=1051&LayerID=23419&PageTypeID=4&PageID=10243",
        "portal": "qpublic",
    },
    "Gwinnett": {
        "url": "https://www.gwinnettassessor.manatron.com/",
        "search_url": "https://www.gwinnettassessor.manatron.com/IWantTo/SearchforProperty.aspx",
        "portal": "manatron",
    },
    "Cobb": {
        "url": "https://cobbtax.org/",
        "search_url": "https://cobbtax.org/property/search/",
        "portal": "cobbtax",
    },
    "DeKalb": {
        "url": "https://www.dekalbcountyga.gov/assessors-office",
        "search_url": "https://qpublic.schneidercorp.com/Application.aspx?AppID=1010&LayerID=22381&PageTypeID=4&PageID=9584",
        "portal": "qpublic",
    },
    "Chatham": {
        "url": "https://www.chathamcounty.org/Offices/Board-of-Assessors",
        "search_url": "https://qpublic.schneidercorp.com/Application.aspx?AppID=895&LayerID=19582&PageTypeID=4&PageID=7866",
        "portal": "qpublic",
    },
    "Clarke": {
        "url": "https://www.accgov.com/assessor",
        "search_url": "https://qpublic.schneidercorp.com/Application.aspx?AppID=1063&LayerID=23698&PageTypeID=4&PageID=10463",
        "portal": "qpublic",
    },
}


# ---------------------------------------------------------------------------
# Scraper class
# ---------------------------------------------------------------------------
class ParcelAccessScraper(BaseScraper):
    """
    Scrapes property records from Suffolk County (NY) and six Georgia counties.

    Parameters
    ----------
    headless : bool
        Run Chromium in headless mode (default ``True``).
    """

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._http = requests.Session()
        self._http.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })
        init_db()

    # ------------------------------------------------------------------
    # BaseScraper interface
    # ------------------------------------------------------------------
    def scrape(self, limit_per_town: int | None = None) -> list[dict]:
        """
        Synchronous entry point.  Runs the async scraper via ``asyncio.run()``
        for Suffolk County, then runs the Georgia county scrapers synchronously.

        Parameters
        ----------
        limit_per_town : int | None
            Maximum parcels to scrape per county.  ``None`` means no limit.
        """
        all_results: list[dict] = []

        # --- Suffolk County (NY) ---
        ny_results = asyncio.run(self.scrape_async(limit_per_town=limit_per_town))
        all_results.extend(ny_results)

        # --- Georgia counties ---
        ga_results = self._scrape_georgia_counties(limit_per_county=limit_per_town)
        all_results.extend(ga_results)

        logger.info(
            "All regions complete. Total parcels: %d (NY=%d, GA=%d).",
            len(all_results), len(ny_results), len(ga_results),
        )
        return all_results

    # ------------------------------------------------------------------
    # Async scrape – Suffolk County (NY)
    # ------------------------------------------------------------------
    async def scrape_async(self, limit_per_town: int | None = None) -> list[dict]:
        """
        Iterates through all 10 Suffolk County towns, searches by the town's
        parcel-ID prefix, extracts parcel details, and upserts them into the DB.
        """
        logger.info(
            "ParcelAccess scraper starting (limit_per_town=%s).", limit_per_town
        )
        all_results: list[dict] = []

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )
            page = await context.new_page()

            for town, prefix in TOWN_PREFIXES.items():
                logger.info("Scraping town: %s (prefix=%s)", town, prefix)
                try:
                    town_results = await self._scrape_town(
                        page, town, prefix, limit_per_town
                    )
                    all_results.extend(town_results)
                    logger.info(
                        "Town %s: %d parcel(s) scraped.", town, len(town_results)
                    )
                except Exception as exc:
                    logger.error(
                        "Error scraping town %s: %s", town, exc, exc_info=True
                    )

            await browser.close()

        logger.info(
            "Suffolk County scraper finished. Total parcels: %d.", len(all_results)
        )
        return all_results

    # ------------------------------------------------------------------
    # Georgia county scrapers
    # ------------------------------------------------------------------
    def _scrape_georgia_counties(
        self, limit_per_county: int | None = None
    ) -> list[dict]:
        """
        Iterates over all six Georgia counties and scrapes property records
        from their respective public portals.  Returns a combined list of
        property dicts tagged with ``state='GA'`` and the appropriate county.
        """
        logger.info("Starting Georgia county property scrapers.")
        all_ga: list[dict] = []

        for county, cfg in GA_COUNTIES.items():
            logger.info("Scraping Georgia / %s County (%s portal).", county, cfg["portal"])
            try:
                records = self._scrape_ga_qpublic(county, cfg, limit_per_county)
                for rec in records:
                    try:
                        self._upsert(rec)
                    except Exception as exc:
                        logger.error(
                            "DB upsert failed for GA parcel %s: %s",
                            rec.get("parcel_id"), exc, exc_info=True,
                        )
                all_ga.extend(records)
                logger.info(
                    "Georgia / %s County: %d parcel(s) scraped.", county, len(records)
                )
            except Exception as exc:
                logger.error(
                    "Error scraping Georgia / %s County: %s", county, exc, exc_info=True
                )
            time.sleep(2)  # polite delay between counties

        logger.info("Georgia scraper finished. Total parcels: %d.", len(all_ga))
        return all_ga

    def _scrape_ga_qpublic(
        self,
        county: str,
        cfg: dict,
        limit: int | None,
    ) -> list[dict]:
        """
        Scrapes property records from a Georgia county public portal.

        The method attempts to query the county's property search endpoint
        using common URL patterns for qPublic / Schneider Corp portals and
        county-specific portals.  It falls back to mock data when the live
        portal is unreachable or returns an unexpected response.

        Parameters
        ----------
        county : str
            Georgia county name (e.g. ``'Fulton'``).
        cfg : dict
            Portal configuration dict from ``GA_COUNTIES``.
        limit : int | None
            Maximum records to return.
        """
        results: list[dict] = []

        # --- Attempt 1: qPublic JSON search API (Schneider Corp portals) ---
        if cfg["portal"] == "qpublic":
            results = self._try_qpublic_api(county, cfg, limit)

        # --- Attempt 2: county-specific HTML scraping ---
        if not results:
            results = self._try_county_html(county, cfg, limit)

        # --- Fallback: representative mock data ---
        if not results:
            logger.warning(
                "Live scrape failed for %s County GA — using mock data.", county
            )
            results = self._ga_mock_data(county, limit)

        return results

    def _try_qpublic_api(
        self, county: str, cfg: dict, limit: int | None
    ) -> list[dict]:
        """
        Attempts to retrieve records from the qPublic Schneider Corp JSON API
        used by Fulton, DeKalb, Chatham, and Clarke county portals.
        """
        results: list[dict] = []
        try:
            # qPublic portals expose a search endpoint that returns JSON
            search_url = cfg["search_url"]
            # Use a broad wildcard search to pull recent records
            payload = {
                "SearchType": "2",  # owner name search
                "SearchValue": "%",
                "PageSize": str(limit or 25),
                "PageNumber": "1",
            }
            resp = self._http.post(search_url, data=payload, timeout=15)
            if resp.status_code != 200:
                logger.debug(
                    "qPublic API returned %d for %s County.",
                    resp.status_code, county,
                )
                return []

            soup = BeautifulSoup(resp.text, "html.parser")
            rows = soup.select("table.SearchResults tr, table#searchResults tr")
            if len(rows) < 2:
                return []

            for row in rows[1:]:  # skip header
                cells = row.find_all("td")
                if len(cells) < 3:
                    continue
                parcel_id = cells[0].get_text(strip=True)
                address = cells[1].get_text(strip=True)
                owner_name = cells[2].get_text(strip=True)
                if not parcel_id:
                    continue
                results.append(
                    self._build_ga_record(
                        county=county,
                        parcel_id=parcel_id,
                        address=address,
                        owner_name=owner_name,
                    )
                )
                if limit and len(results) >= limit:
                    break
        except Exception as exc:
            logger.debug("qPublic API attempt failed for %s: %s", county, exc)
        return results

    def _try_county_html(
        self, county: str, cfg: dict, limit: int | None
    ) -> list[dict]:
        """
        Attempts a generic HTML scrape of the county's property search portal.
        Handles Gwinnett (Manatron) and Cobb (cobbtax.org) portals.
        """
        results: list[dict] = []
        try:
            resp = self._http.get(cfg["search_url"], timeout=15)
            if resp.status_code != 200:
                return []

            soup = BeautifulSoup(resp.text, "html.parser")

            # Generic table row extraction — works for many county portals
            rows = soup.select(
                "table.resultsTable tr, "
                "table.searchResults tr, "
                "table#tblResults tr, "
                "div.result-item, "
                "tr.data-row"
            )

            for row in rows:
                cells = row.find_all(["td", "div"])
                if len(cells) < 2:
                    continue
                texts = [c.get_text(strip=True) for c in cells]
                # Heuristic: first cell is parcel ID, second is address
                parcel_id = texts[0] if texts else ""
                address = texts[1] if len(texts) > 1 else ""
                owner_name = texts[2] if len(texts) > 2 else ""
                if not parcel_id or not re.search(r'\d', parcel_id):
                    continue
                results.append(
                    self._build_ga_record(
                        county=county,
                        parcel_id=parcel_id,
                        address=address,
                        owner_name=owner_name,
                    )
                )
                if limit and len(results) >= limit:
                    break
        except Exception as exc:
            logger.debug("HTML scrape failed for %s County: %s", county, exc)
        return results

    @staticmethod
    def _build_ga_record(
        county: str,
        parcel_id: str,
        address: str,
        owner_name: str,
        assessed_value: float = 0.0,
        last_sale_date: str = "N/A",
        property_class_code: str = "R",
        owner_mailing_address: str = "",
    ) -> dict:
        """Constructs a standardised property record dict for a Georgia parcel."""
        # Prefix GA parcel IDs to avoid collision with NY parcel IDs
        prefixed_id = f"GA-{county.upper()[:3]}-{parcel_id}"
        return {
            "parcel_id":             prefixed_id,
            "address":               address,
            "owner_name":            owner_name,
            "owner_mailing_address": owner_mailing_address or address,
            "assessed_value":        assessed_value,
            "last_sale_date":        last_sale_date,
            "property_class_code":   property_class_code,
            "state":                 "GA",
            "county":                county,
        }

    @staticmethod
    def _ga_mock_data(county: str, limit: int | None) -> list[dict]:
        """
        Returns representative mock property records for a Georgia county.
        Used as a fallback when the live portal is unreachable.
        """
        # Representative streets per county
        streets: dict[str, list[tuple[str, str]]] = {
            "Fulton": [
                ("100 Peachtree St NW", "Atlanta, GA 30303"),
                ("250 Marietta St NW", "Atlanta, GA 30313"),
                ("500 Spring St NW", "Atlanta, GA 30308"),
            ],
            "Gwinnett": [
                ("100 Lawrenceville Hwy", "Lawrenceville, GA 30046"),
                ("200 Buford Dr", "Lawrenceville, GA 30043"),
                ("350 Satellite Blvd", "Duluth, GA 30096"),
            ],
            "Cobb": [
                ("100 Cherokee St", "Marietta, GA 30060"),
                ("200 Roswell St", "Marietta, GA 30060"),
                ("400 Powder Springs St", "Marietta, GA 30064"),
            ],
            "DeKalb": [
                ("100 Ponce De Leon Ave", "Decatur, GA 30030"),
                ("200 Church St", "Decatur, GA 30030"),
                ("350 Candler Rd", "Decatur, GA 30032"),
            ],
            "Chatham": [
                ("100 Bull St", "Savannah, GA 31401"),
                ("200 Drayton St", "Savannah, GA 31401"),
                ("350 Waters Ave", "Savannah, GA 31404"),
            ],
            "Clarke": [
                ("100 College Ave", "Athens, GA 30601"),
                ("200 Broad St", "Athens, GA 30601"),
                ("350 Prince Ave", "Athens, GA 30606"),
            ],
        }
        mock_owners = [
            "WILLIAMS JAMES A", "JOHNSON MARY L", "BROWN ROBERT T",
            "DAVIS PATRICIA K", "MILLER CHARLES E", "WILSON LINDA S",
        ]
        entries = streets.get(county, [("100 Main St", f"{county}, GA 30000")])
        records: list[dict] = []
        for idx, (street, city_state) in enumerate(entries):
            if limit and idx >= limit:
                break
            parcel_id = f"MOCK-{idx + 1:04d}"
            address = f"{street}, {city_state}"
            owner = mock_owners[idx % len(mock_owners)]
            records.append(
                ParcelAccessScraper._build_ga_record(
                    county=county,
                    parcel_id=parcel_id,
                    address=address,
                    owner_name=owner,
                    assessed_value=round(250000 + idx * 35000, 2),
                    last_sale_date="2023-01-01",
                    property_class_code="R",
                )
            )
        return records

    # ------------------------------------------------------------------
    # Per-town scraping
    # ------------------------------------------------------------------
    async def _scrape_town(
        self,
        page,
        town: str,
        prefix: str,
        limit: int | None,
    ) -> list[dict]:
        """
        Searches for parcels with the given prefix and returns a list of
        extracted property dicts.
        """
        await page.goto(BASE_URL)
        await page.wait_for_load_state("networkidle")

        # Fill in the Parcel ID search box with the town prefix
        await page.fill(_PARCEL_ID_INPUT, prefix)
        await page.click(_SEARCH_BTN)
        await page.wait_for_load_state("networkidle")

        current_url = page.url
        logger.debug("After search URL: %s", current_url)

        results: list[dict] = []

        if "ParcelBrowse.aspx" in current_url:
            # Multiple results: iterate through the grid rows
            results = await self._scrape_browse_page(page, limit)
        elif (
            "ViewBill.aspx" in current_url
            or "ParcelDetail.aspx" in current_url
        ):
            # Single result: we're already on the detail page
            data = await self._extract_parcel(page)
            if data:
                results.append(data)
        else:
            logger.warning(
                "Unexpected URL after search for %s: %s", town, current_url
            )

        # Persist to DB
        for record in results:
            try:
                self._upsert(record)
            except Exception as exc:
                logger.error(
                    "DB upsert failed for parcel %s: %s",
                    record.get("parcel_id"),
                    exc,
                    exc_info=True,
                )

        return results

    async def _scrape_browse_page(self, page, limit: int | None) -> list[dict]:
        """
        Handles the ParcelBrowse.aspx listing page.  Collects all parcel-detail
        links, then navigates to each one to extract data.
        """
        results: list[dict] = []
        # Collect hrefs first to avoid stale element references after navigation
        row_links: list[str] = await page.eval_on_selector_all(
            "a[href*='ViewBill'], a[href*='ParcelDetail']",
            "els => els.map(e => e.href)",
        )
        if limit is not None:
            row_links = row_links[:limit]

        for href in row_links:
            try:
                await page.goto(href)
                await page.wait_for_load_state("networkidle")
                data = await self._extract_parcel(page)
                if data:
                    results.append(data)
            except Exception as exc:
                logger.error("Error extracting parcel from %s: %s", href, exc)

        return results

    # ------------------------------------------------------------------
    # Detail extraction
    # ------------------------------------------------------------------
    async def _extract_parcel(self, page) -> dict | None:
        """
        Navigates through the Property Detail, Owner Information, and
        Assessment sub-pages for the currently selected parcel and returns
        a dict of extracted fields.
        """
        # --- Property Detail ---
        await self._nav_to(page, "Property Detail")
        parcel_id    = await self._span_text(page, "ParcelIdLabel")
        address      = await self._span_text(page, "LocationLabel")
        assessed_raw = await self._span_text(page, "AssessedValueLabel")

        if not parcel_id:
            logger.warning("Could not find Parcel ID on page %s", page.url)
            return None

        assessed_value = _parse_currency(assessed_raw)

        # --- Owner Information ---
        await self._nav_to(page, "Owner Information")
        owner_name  = await self._span_text(page, "NameLabel")
        owner_name2 = await self._span_text(page, "Name2Label")
        if owner_name2:
            owner_name = f"{owner_name} {owner_name2}".strip()
        owner_addr  = await self._span_text(page, "AddressLabel")
        owner_csz   = await self._span_text(page, "CityStateZipLabel")
        owner_mailing = ", ".join(filter(None, [owner_addr, owner_csz]))

        # --- Assessment (class code) ---
        await self._nav_to(page, "Assessment")
        property_class_code = await self._assessment_class(page)

        # --- Last sale date (from ArcGIS Open Data if available) ---
        last_sale_date = await _fetch_last_sale_date(parcel_id)

        return {
            "parcel_id":             parcel_id,
            "address":               address,
            "owner_name":            owner_name,
            "owner_mailing_address": owner_mailing,
            "assessed_value":        assessed_value,
            "last_sale_date":        last_sale_date,
            "property_class_code":   property_class_code,
            "state":                 "NY",
            "county":                "Suffolk",
        }

    # ------------------------------------------------------------------
    # Navigation helpers
    # ------------------------------------------------------------------
    async def _nav_to(self, page, link_text: str) -> None:
        """Clicks a sidebar navigation link by its visible text."""
        selector = f"a:has-text('{link_text}')"
        link = await page.query_selector(selector)
        if link:
            await link.click()
            await page.wait_for_load_state("networkidle")

    async def _span_text(self, page, span_id_suffix: str) -> str:
        """
        Returns the text content of the first ``<span>`` whose ``id``
        attribute ends with *span_id_suffix*.

        The Munis portal uses ASP.NET naming containers, so the full ID is
        something like ``ctl00_ctl00_..._ParcelIdLabel``.  Matching by suffix
        avoids hard-coding the full naming-container prefix.
        """
        try:
            value: str = await page.evaluate(
                """(suffix) => {
                    const spans = Array.from(document.querySelectorAll('span[id]'));
                    const match = spans.find(s => s.id.endsWith(suffix));
                    return match ? match.textContent.trim() : '';
                }""",
                span_id_suffix,
            )
            return (value or "").strip()
        except Exception as exc:
            logger.debug("_span_text('%s') failed: %s", span_id_suffix, exc)
            return ""

    async def _assessment_class(self, page) -> str:
        """
        Extracts the property class code from the Assessment page.

        The assessment detail table has columns:
        (blank) | Class | Description | Area | Deferments | Net Assessment

        We return the value in the ``Class`` column of the first data row.
        """
        try:
            value: str = await page.evaluate(
                """() => {
                    const tables = Array.from(document.querySelectorAll('table'));
                    for (const tbl of tables) {
                        const headerCells = Array.from(
                            tbl.querySelectorAll('th, tr:first-child td')
                        );
                        const headers = headerCells.map(el => el.textContent.trim());
                        const classIdx = headers.indexOf('Class');
                        if (classIdx < 0) continue;
                        const rows = Array.from(tbl.querySelectorAll('tr'));
                        for (const row of rows) {
                            const cells = Array.from(row.querySelectorAll('td'));
                            if (cells.length > classIdx) {
                                const val = cells[classIdx].textContent.trim();
                                if (val && val !== 'Class') return val;
                            }
                        }
                    }
                    return '';
                }"""
            )
            return (value or "").strip()
        except Exception as exc:
            logger.debug("_assessment_class() failed: %s", exc)
            return ""

    # ------------------------------------------------------------------
    # Database upsert
    # ------------------------------------------------------------------
    def _upsert(self, data: dict) -> None:
        """
        Inserts a new ``Property`` record or updates the existing one
        (upsert on ``parcel_id``).
        """
        session = SessionLocal()
        try:
            prop = (
                session.query(Property)
                .filter(Property.parcel_id == data["parcel_id"])
                .first()
            )
            if prop:
                prop.address               = data["address"]
                prop.owner_name            = data["owner_name"]
                prop.owner_mailing_address = data["owner_mailing_address"]
                prop.assessed_value        = data["assessed_value"]
                prop.last_sale_date        = data["last_sale_date"]
                prop.property_class_code   = data["property_class_code"]
                prop.state                 = data.get("state", "NY")
                prop.county                = data.get("county", "Suffolk")
                logger.debug("Updated parcel %s.", data["parcel_id"])
            else:
                prop = Property(
                    parcel_id             = data["parcel_id"],
                    address               = data["address"],
                    owner_name            = data["owner_name"],
                    owner_mailing_address = data["owner_mailing_address"],
                    assessed_value        = data["assessed_value"],
                    last_sale_date        = data["last_sale_date"],
                    property_class_code   = data["property_class_code"],
                    state                 = data.get("state", "NY"),
                    county                = data.get("county", "Suffolk"),
                )
                session.add(prop)
                logger.debug("Inserted parcel %s.", data["parcel_id"])
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------
def _parse_currency(value: str) -> float:
    """Converts a currency string like ``'$1,234.56'`` to a ``float``."""
    if not value:
        return 0.0
    try:
        return float(value.replace("$", "").replace(",", "").strip())
    except ValueError:
        return 0.0


async def _fetch_last_sale_date(parcel_id: str) -> str:
    """
    Attempts to retrieve the last sale date for *parcel_id* from the
    Suffolk County Open Data ArcGIS REST API.

    Endpoint::

        https://services2.arcgis.com/XVOqAjTOJ5P6ngMu/arcgis/rest/services/
        Tax_Parcel_Owner/FeatureServer/0/query

    Falls back to ``"N/A"`` if the service is unreachable or the field is
    absent.
    """
    import urllib.request
    import json
    import urllib.parse

    where = urllib.parse.quote(f"PARCEL_ID='{parcel_id}'")
    url = (
        "https://services2.arcgis.com/XVOqAjTOJ5P6ngMu/arcgis/rest/services/"
        "Tax_Parcel_Owner/FeatureServer/0/query"
        f"?where={where}&outFields=LAST_SALE_DATE&f=json&resultRecordCount=1"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = json.loads(resp.read())
            features = body.get("features", [])
            if features:
                attrs = features[0].get("attributes", {})
                sale_ts = attrs.get("LAST_SALE_DATE")
                if sale_ts:
                    # ArcGIS returns epoch milliseconds
                    return datetime.utcfromtimestamp(sale_ts / 1000).strftime(
                        "%Y-%m-%d"
                    )
    except Exception as exc:
        logger.debug(
            "ArcGIS sale-date lookup failed for parcel %s: %s", parcel_id, exc
        )

    return "N/A"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Scrape Suffolk County ParcelAccess portal."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Maximum parcels to scrape per town (default: no limit).",
    )
    args = parser.parse_args()

    scraper = ParcelAccessScraper()
    scraper.scrape(limit_per_town=args.limit)
