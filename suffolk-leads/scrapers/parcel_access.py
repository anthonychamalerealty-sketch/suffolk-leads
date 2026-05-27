"""
scrapers/parcel_access.py
--------------------------
Playwright-based scraper for Suffolk County's ParcelAccess portal
(https://suffolk.munisselfservice.com).

Searches by municipality across all 10 Suffolk County towns, extracts parcel
data, and upserts records into the ``properties`` table of the SQLite database.

Usage
-----
Run all towns (no limit):
    python -m scrapers.parcel_access

Run with a per-town limit (useful for testing):
    python -m scrapers.parcel_access --limit 5
"""

import os
import sys
import logging
import asyncio
import argparse
from datetime import datetime

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
# Scraper class
# ---------------------------------------------------------------------------
class ParcelAccessScraper(BaseScraper):
    """
    Scrapes the Suffolk County ParcelAccess / Munis self-service portal.

    Parameters
    ----------
    headless : bool
        Run Chromium in headless mode (default ``True``).
    """

    def __init__(self, headless: bool = True):
        self.headless = headless
        init_db()

    # ------------------------------------------------------------------
    # BaseScraper interface
    # ------------------------------------------------------------------
    def scrape(self, limit_per_town: int | None = None) -> list[dict]:
        """
        Synchronous entry point.  Runs the async scraper via ``asyncio.run()``.

        Parameters
        ----------
        limit_per_town : int | None
            Maximum parcels to scrape per town.  ``None`` means no limit.
        """
        return asyncio.run(self.scrape_async(limit_per_town=limit_per_town))

    # ------------------------------------------------------------------
    # Async scrape
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
            "Scraper finished. Total parcels scraped: %d.", len(all_results)
        )
        return all_results

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
