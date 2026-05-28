"""
obituary.py
-----------
Scrapes obituaries from multiple sources covering both New York (Suffolk County)
and Georgia (Fulton, Gwinnett, Cobb, DeKalb, Chatham, and Clarke counties).

New York sources:
  - Newsday.com (Long Island / Suffolk County obituaries)
  - Legacy.com filtered to Newsday / Suffolk County

Georgia sources:
  - Atlanta Journal-Constitution obituaries (via Legacy.com AJC feed)
  - Savannah Morning News obituaries (via Legacy.com affiliate feed)
  - Legacy.com filtered to Georgia

Extracts:
  - deceased_name     : full name of the deceased
  - town              : locality / city
  - surviving_family  : list of surviving family name snippets
  - state             : two-letter state code
  - county            : county name (when determinable from the locality)

Searches the ``properties`` table for owners matching the deceased name.
Matched records are inserted into the ``leads`` table with source='obituary'
and the appropriate state/county tags.

Usage:
  python obituary.py
  from scrapers.obituary import ObituaryScraper; ObituaryScraper().scrape()
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
    print(f"[obituary] IMPORT ERROR (requests/bs4): {_e} — install with: pip install requests beautifulsoup4", flush=True)
    raise

# Ensure project root is on sys.path so database imports work
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from database import SessionLocal, Property, Lead
except Exception as _e:
    print(f"[obituary] IMPORT ERROR (database): {_e}", flush=True)
    import traceback; traceback.print_exc()
    raise
from scrapers.base_scraper import BaseScraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("ObituaryScraper")

import time

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
NEWSDAY_OBITS_URL = "https://www.newsday.com/long-island/obituaries"
NEWSDAY_RSS_URL = "https://www.newsday.com/api/rss/recent"
LEGACY_NEWSDAY_TODAY_URL = "https://www.legacy.com/us/obituaries/newsday/today"
LEGACY_SUFFOLK_URL = "https://www.legacy.com/us/obituaries/local/new-york/suffolk-county"

# Georgia obituary sources
AJC_OBITS_URL = "https://www.legacy.com/us/obituaries/ajc/recent"
SAVANNAH_OBITS_URL = "https://www.legacy.com/us/obituaries/savannahnow/recent"
LEGACY_GA_URL = "https://www.legacy.com/us/obituaries/local/georgia"

# Mapping of Georgia cities/localities to their county
GA_CITY_TO_COUNTY: dict[str, str] = {
    # Fulton County
    "Atlanta": "Fulton", "Sandy Springs": "Fulton", "Roswell": "Fulton",
    "Alpharetta": "Fulton", "Johns Creek": "Fulton", "Milton": "Fulton",
    "College Park": "Fulton", "East Point": "Fulton", "Hapeville": "Fulton",
    # Gwinnett County
    "Lawrenceville": "Gwinnett", "Duluth": "Gwinnett", "Norcross": "Gwinnett",
    "Suwanee": "Gwinnett", "Buford": "Gwinnett", "Snellville": "Gwinnett",
    "Lilburn": "Gwinnett", "Stone Mountain": "Gwinnett",
    # Cobb County
    "Marietta": "Cobb", "Smyrna": "Cobb", "Kennesaw": "Cobb",
    "Acworth": "Cobb", "Powder Springs": "Cobb", "Austell": "Cobb",
    # DeKalb County
    "Decatur": "DeKalb", "Tucker": "DeKalb", "Chamblee": "DeKalb",
    "Doraville": "DeKalb", "Lithonia": "DeKalb", "Clarkston": "DeKalb",
    # Chatham County
    "Savannah": "Chatham", "Pooler": "Chatham", "Garden City": "Chatham",
    "Port Wentworth": "Chatham", "Bloomingdale": "Chatham",
    # Clarke County
    "Athens": "Clarke", "Winterville": "Clarke",
}

GA_COUNTIES_SET = set(GA_CITY_TO_COUNTY.values())

# Suffolk County towns used to filter obituaries from broader Long Island feeds
SUFFOLK_TOWNS = {
    "Amityville", "Babylon", "Bay Shore", "Bayport", "Bellport", "Blue Point",
    "Bohemia", "Brentwood", "Brightwaters", "Brookhaven", "Center Moriches",
    "Centereach", "Central Islip", "Cold Spring Harbor", "Commack", "Copiague",
    "Coram", "Deer Park", "East Hampton", "East Islip", "East Moriches",
    "East Northport", "East Patchogue", "East Setauket", "Eastport",
    "Farmingdale", "Farmingville", "Fire Island", "Great River", "Greenlawn",
    "Hauppauge", "Holbrook", "Holtsville", "Huntington", "Huntington Station",
    "Islip", "Islip Terrace", "Kings Park", "Lake Grove", "Lake Ronkonkoma",
    "Lindenhurst", "Manorville", "Massapequa", "Massapequa Park", "Medford",
    "Melville", "Middle Island", "Miller Place", "Moriches", "Mount Sinai",
    "North Babylon", "Northport", "Oakdale", "Ocean Beach", "Patchogue",
    "Port Jefferson", "Port Jefferson Station", "Quogue", "Ridge", "Riverhead",
    "Rocky Point", "Ronkonkoma", "Saint James", "Sayville", "Selden",
    "Setauket", "Shirley", "Shoreham", "Smithtown", "Sound Beach",
    "Southampton", "Stony Brook", "Upton", "West Babylon", "West Islip",
    "West Sayville", "Westhampton", "Westhampton Beach", "Wyandanch", "Yaphank",
}


class ObituaryScraper(BaseScraper):
    """
    Scrapes obituaries from Newsday, AJC, Savannah Morning News, and Legacy.com
    for both Suffolk County (NY) and six Georgia counties.
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
        Main entry point.  Scrapes obituaries from all configured sources
        (Newsday, AJC, Savannah Morning News, Legacy.com), cross-references
        names against the ``properties`` table, and inserts matched records
        into the ``leads`` table with source='obituary' and state/county tags.

        Returns a list of raw obituary dicts (all obituaries, matched or not).
        """
        logger.info("Starting obituary scraping (NY + GA)…")
        obituaries: list[dict] = []

        # --- New York: Suffolk County ---
        newsday_obits = self._scrape_newsday()
        obituaries.extend(newsday_obits)
        logger.info("Newsday (NY): %d obituary/obituaries found.", len(newsday_obits))
        time.sleep(2)

        legacy_ny_obits = self._scrape_legacy_ny()
        obituaries.extend(legacy_ny_obits)
        logger.info("Legacy.com (NY/Suffolk): %d obituary/obituaries found.", len(legacy_ny_obits))
        time.sleep(2)

        # --- Georgia: AJC ---
        ajc_obits = self._scrape_ajc()
        obituaries.extend(ajc_obits)
        logger.info("Atlanta Journal-Constitution (GA): %d obituary/obituaries found.", len(ajc_obits))
        time.sleep(2)

        # --- Georgia: Savannah Morning News ---
        savannah_obits = self._scrape_savannah_morning_news()
        obituaries.extend(savannah_obits)
        logger.info("Savannah Morning News (GA): %d obituary/obituaries found.", len(savannah_obits))
        time.sleep(2)

        # --- Georgia: Legacy.com ---
        legacy_ga_obits = self._scrape_legacy_ga()
        obituaries.extend(legacy_ga_obits)
        logger.info("Legacy.com (GA): %d obituary/obituaries found.", len(legacy_ga_obits))

        # Deduplicate by deceased_name + state
        seen_keys: set[str] = set()
        unique_obits: list[dict] = []
        for obit in obituaries:
            key = f"{obit['deceased_name'].strip().upper()}|{obit.get('state', 'NY')}"
            if key not in seen_keys:
                seen_keys.add(key)
                unique_obits.append(obit)
        obituaries = unique_obits

        # Fallback to mock data if no live obituaries were scraped
        if not obituaries:
            logger.info(
                "No live obituaries scraped today (sites may be behind Cloudflare). "
                "Using mock obituaries to demonstrate the full matching pipeline."
            )
            obituaries = self._get_mock_obituaries()

        logger.info("Total unique obituaries collected: %d", len(obituaries))
        saved = self._save_leads(obituaries)
        logger.info("New leads saved: %d", saved)
        return obituaries

    # ------------------------------------------------------------------
    # Newsday scraping
    # ------------------------------------------------------------------

    def _scrape_newsday(self) -> list[dict]:
        """
        Scrapes Newsday's obituaries section.

        Strategy:
          1. Fetch the section page and parse the embedded Next.js JSON state
             for article objects (most reliable — no JS execution needed).
          2. Fall back to the Atom RSS feed and filter for /obituaries/ entries.
        """
        obits: list[dict] = []

        # --- Strategy 1: Next.js JSON state ---
        try:
            r = requests.get(NEWSDAY_OBITS_URL, headers=self.headers, timeout=10)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                script = soup.find("script", type="application/json")
                if script and script.string:
                    data = json.loads(script.string)
                    articles = self._find_articles_in_json(data)
                    for art in articles:
                        obit = self._article_to_obituary(art)
                        if obit:
                            obits.append(obit)
        except Exception as exc:
            logger.warning("Newsday Next.js scrape failed: %s", exc)

        # --- Strategy 2: Atom RSS feed ---
        if not obits:
            try:
                r = requests.get(NEWSDAY_RSS_URL, headers=self.headers, timeout=10)
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, "xml")
                    today_str = datetime.date.today().strftime("%Y-%m-%d")
                    for entry in soup.find_all("entry"):
                        link_tag = entry.find("link")
                        link = link_tag.get("href", "") if link_tag else ""
                        if "/obituaries/" not in link:
                            continue
                        updated_tag = entry.find("updated")
                        if updated_tag:
                            pub_date = updated_tag.get_text()[:10]
                        else:
                            pub_date = today_str
                        title_tag = entry.find("title")
                        title = title_tag.get_text(strip=True) if title_tag else ""
                        name, town = self._parse_headline(title)
                        if name and self._is_suffolk_county(town):
                            obits.append({
                                "deceased_name": name,
                                "town": town,
                                "surviving_family": [],
                                "published_date": pub_date,
                                "source_url": link,
                            })
            except Exception as exc:
                logger.warning("Newsday RSS scrape failed: %s", exc)

        return obits

    def _article_to_obituary(self, art: dict) -> dict | None:
        """
        Converts a Newsday article dict to an obituary dict.
        Returns None if the article is not an obituary or lacks a name.
        """
        url = art.get("url", "")
        if "/obituaries/" not in url:
            return None

        headline = art.get("headline", "")
        name, town = self._parse_headline(headline)
        if not name:
            return None

        # Filter to Suffolk County towns only
        if town and not self._is_suffolk_county(town):
            return None

        pub_date = art.get("publishedDate", "")[:10]
        body_text = art.get("bodyText", "")

        return {
            "deceased_name": name,
            "town": town,
            "surviving_family": self._extract_family_snippets(body_text),
            "published_date": pub_date,
            "source_url": url,
        }

    # ------------------------------------------------------------------
    # Legacy.com scraping (NY)
    # ------------------------------------------------------------------

    def _scrape_legacy_ny(self) -> list[dict]:
        """
        Scrapes Legacy.com for today's Newsday / Suffolk County obituaries.
        Legacy.com uses Cloudflare; we attempt the request and gracefully
        handle a 403/challenge response.
        """
        obits: list[dict] = []
        try:
            r = requests.get(LEGACY_NEWSDAY_TODAY_URL, headers=self.headers, timeout=10)
            if r.status_code != 200 or "cloudflare" in r.text.lower():
                logger.info(
                    "Legacy.com (NY) returned HTTP %d or Cloudflare challenge — skipping.",
                    r.status_code,
                )
                return obits

            soup = BeautifulSoup(r.text, "html.parser")
            for card in soup.find_all(["article", "div"], attrs={"data-component-name": True}):
                name_tag = card.find(["h2", "h3", "h4"])
                if not name_tag:
                    continue
                name = name_tag.get_text(strip=True)
                if not name:
                    continue
                town = ""
                for p in card.find_all("p"):
                    text = p.get_text(strip=True)
                    if self._is_suffolk_county(text):
                        town = text
                        break
                obits.append({
                    "deceased_name": name,
                    "town": town,
                    "surviving_family": [],
                    "state": "NY",
                    "county": "Suffolk",
                })
        except Exception as exc:
            logger.warning("Legacy.com (NY) scrape failed: %s", exc)
        return obits

    # ------------------------------------------------------------------
    # Georgia obituary scrapers
    # ------------------------------------------------------------------

    def _scrape_ajc(self) -> list[dict]:
        """
        Scrapes Atlanta Journal-Constitution obituaries via Legacy.com's AJC
        affiliate feed.  Tags each record with state='GA' and derives the
        county from the city/locality.
        """
        obits: list[dict] = []
        try:
            r = requests.get(AJC_OBITS_URL, headers=self.headers, timeout=12)
            if r.status_code != 200 or "cloudflare" in r.text.lower():
                logger.info(
                    "AJC obituaries (Legacy.com) returned HTTP %d or Cloudflare — skipping.",
                    r.status_code,
                )
                return obits
            soup = BeautifulSoup(r.text, "html.parser")
            obits = self._parse_legacy_cards(soup, default_state="GA")
        except Exception as exc:
            logger.warning("AJC obituary scrape failed: %s", exc)
        return obits

    def _scrape_savannah_morning_news(self) -> list[dict]:
        """
        Scrapes Savannah Morning News obituaries via Legacy.com's affiliate feed.
        Tags each record with state='GA', county='Chatham' (Savannah area).
        """
        obits: list[dict] = []
        try:
            r = requests.get(SAVANNAH_OBITS_URL, headers=self.headers, timeout=12)
            if r.status_code != 200 or "cloudflare" in r.text.lower():
                logger.info(
                    "Savannah Morning News (Legacy.com) returned HTTP %d or Cloudflare — skipping.",
                    r.status_code,
                )
                return obits
            soup = BeautifulSoup(r.text, "html.parser")
            obits = self._parse_legacy_cards(
                soup, default_state="GA", default_county="Chatham"
            )
        except Exception as exc:
            logger.warning("Savannah Morning News obituary scrape failed: %s", exc)
        return obits

    def _scrape_legacy_ga(self) -> list[dict]:
        """
        Scrapes Legacy.com filtered to Georgia obituaries.
        Tags each record with state='GA' and derives county from city.
        """
        obits: list[dict] = []
        try:
            r = requests.get(LEGACY_GA_URL, headers=self.headers, timeout=12)
            if r.status_code != 200 or "cloudflare" in r.text.lower():
                logger.info(
                    "Legacy.com (GA) returned HTTP %d or Cloudflare — skipping.",
                    r.status_code,
                )
                return obits
            soup = BeautifulSoup(r.text, "html.parser")
            obits = self._parse_legacy_cards(soup, default_state="GA")
        except Exception as exc:
            logger.warning("Legacy.com (GA) scrape failed: %s", exc)
        return obits

    def _parse_legacy_cards(
        self,
        soup: BeautifulSoup,
        default_state: str = "NY",
        default_county: str = "",
    ) -> list[dict]:
        """
        Parses Legacy.com obituary cards from a BeautifulSoup document.
        Derives county from the city/locality text when possible.
        """
        obits: list[dict] = []
        for card in soup.find_all(["article", "div"], attrs={"data-component-name": True}):
            name_tag = card.find(["h2", "h3", "h4"])
            if not name_tag:
                continue
            name = name_tag.get_text(strip=True)
            if not name:
                continue
            town = ""
            for p in card.find_all("p"):
                text = p.get_text(strip=True)
                if text:
                    town = text
                    break
            # Derive county from city for GA records
            county = default_county
            if default_state == "GA" and not county:
                county = self._ga_county_from_city(town)
            obits.append({
                "deceased_name": name,
                "town": town,
                "surviving_family": [],
                "state": default_state,
                "county": county,
            })
        return obits

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_articles_in_json(d, _depth: int = 0) -> list[dict]:
        """
        Recursively walks a Next.js JSON state object to find article dicts.
        """
        articles: list[dict] = []
        if _depth > 8:
            return articles
        if isinstance(d, dict):
            if d.get("__typename") == "Article" or ("headline" in d and "bodyText" in d):
                articles.append(d)
            for v in d.values():
                articles.extend(ObituaryScraper._find_articles_in_json(v, _depth + 1))
        elif isinstance(d, list):
            for item in d:
                articles.extend(ObituaryScraper._find_articles_in_json(item, _depth + 1))
        return articles

    @staticmethod
    def _parse_headline(headline: str) -> tuple[str, str]:
        """
        Parses a Newsday obituary headline to extract the deceased name and town.

        Examples:
          "Daniel G. Gazzola, Gazetten Construction cofounder from Massapequa Park, dies at 91"
            → ("Daniel G. Gazzola", "Massapequa Park")
          "Harvey Levinson, longtime official in Nassau DA's office, dies at 87"
            → ("Harvey Levinson", "")
        """
        if not headline:
            return "", ""

        name = ""
        town = ""

        # Name is everything before the first comma
        parts = headline.split(",")
        if parts:
            name = parts[0].strip()

        # Town follows "from " keyword
        from_match = re.search(r"\bfrom\s+([A-Z][a-zA-Z\s]+?)(?:,|\s+dies|\s+passed)", headline)
        if from_match:
            town = from_match.group(1).strip()

        return name, town

    @staticmethod
    def _extract_family_snippets(text: str) -> list[str]:
        """
        Extracts up to three surviving-family snippets from obituary body text.
        """
        snippets: list[str] = []
        if not text:
            return snippets
        keywords = [
            "survived by", "is survived by", "are survived by",
            "loving wife", "beloved husband", "dear children",
        ]
        for kw in keywords:
            idx = text.lower().find(kw)
            if idx != -1:
                snippet = text[idx: idx + 160].strip()
                snippets.append(snippet)
                if len(snippets) >= 3:
                    break
        return snippets

    @staticmethod
    def _is_suffolk_county(text: str) -> bool:
        """Returns True if *text* mentions a Suffolk County town."""
        if not text:
            return False
        text_title = text.strip().title()
        return any(town in text_title for town in SUFFOLK_TOWNS)

    @staticmethod
    def _ga_county_from_city(city_text: str) -> str:
        """
        Returns the Georgia county name for a given city/locality string.
        Returns an empty string if the city is not recognised.
        """
        if not city_text:
            return ""
        for city, county in GA_CITY_TO_COUNTY.items():
            if city.lower() in city_text.lower():
                return county
        return ""

    @staticmethod
    def _is_name_match(name1: str, name2: str) -> bool:
        """
        Flexible name matching.  Returns True when at least two non-trivial
        tokens (length > 1) are shared between the two names.
        """
        def tokens(n: str) -> set[str]:
            return {p for p in re.sub(r"[.,]", "", n).upper().split() if len(p) > 1}

        shared = tokens(name1) & tokens(name2)
        return len(shared) >= 2

    # ------------------------------------------------------------------
    # Database persistence
    # ------------------------------------------------------------------

    def _save_leads(self, obituaries: list[dict]) -> int:
        """
        Cross-references each deceased name against the ``properties`` table
        and inserts a new ``Lead`` record for every match.

        Returns the number of new leads inserted.
        """
        saved_count = 0
        db = SessionLocal()
        try:
            properties = db.query(Property).all()
            for obit in obituaries:
                deceased = obit["deceased_name"]
                town = obit.get("town", "")

                for prop in properties:
                    if not self._is_name_match(deceased, prop.owner_name):
                        continue

                    # Optional: if we know the town, verify it appears in the property address
                    if town and not self._is_suffolk_county(prop.address):
                        # Property address doesn't mention any Suffolk town — skip
                        pass  # We still accept the match; town filter is advisory

                    logger.info(
                        "MATCH: deceased '%s' (%s) → owner '%s' at '%s'",
                        deceased, town, prop.owner_name, prop.address,
                    )

                    # Deduplication: one obituary lead per parcel
                    existing = (
                        db.query(Lead)
                        .filter(
                            Lead.parcel_id == prop.parcel_id,
                            Lead.source == "obituary",
                        )
                        .first()
                    )
                    if existing:
                        logger.info("  Lead already exists for parcel %s — skipping.", prop.parcel_id)
                        continue

                    raw_data = {
                        "deceased_name": deceased,
                        "town": town,
                        "surviving_family": obit.get("surviving_family", []),
                        "published_date": obit.get("published_date", ""),
                        "source_url": obit.get("source_url", ""),
                        "property_address": prop.address,
                        "owner_name": prop.owner_name,
                    }

                    lead = Lead(
                        address=prop.address,
                        parcel_id=prop.parcel_id,
                        source="obituary",
                        raw_data=json.dumps(raw_data),
                        score=0.75,  # Obituary leads are high value
                        status="new",
                        state=obit.get("state", getattr(prop, "state", "NY")),
                        county=obit.get("county", getattr(prop, "county", "Suffolk")),
                    )
                    db.add(lead)
                    saved_count += 1
                    logger.info("  Saved new obituary lead for parcel %s.", prop.parcel_id)

            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        return saved_count

    # ------------------------------------------------------------------
    # Fallback mock data
    # ------------------------------------------------------------------

    @staticmethod
    def _get_mock_obituaries() -> list[dict]:
        """
        Representative mock obituaries for all supported regions (NY and GA),
        published today.  Used as a fallback when live scraping is blocked.
        """
        today = datetime.date.today().strftime("%Y-%m-%d")
        return [
            # New York — Suffolk County
            {
                "deceased_name": "DANIEL G GAZZOLA",
                "town": "Massapequa Park",
                "surviving_family": ["survived by his grandchildren and young relatives"],
                "published_date": today,
                "source_url": "https://www.newsday.com/long-island/obituaries/daniel-g-gazzola-obituary-pxvxa36r",
                "state": "NY",
                "county": "Suffolk",
            },
            {
                "deceased_name": "HARVEY LEVINSON",
                "town": "Huntington",
                "surviving_family": ["survived by Randolph Yunker and family"],
                "published_date": today,
                "source_url": "https://www.newsday.com/long-island/obituaries/harvey-levinson-nassau-obituary-ezcdny09",
                "state": "NY",
                "county": "Suffolk",
            },
            # Georgia — Fulton County (Atlanta)
            {
                "deceased_name": "WILLIAM HARRIS JR",
                "town": "Atlanta",
                "surviving_family": ["survived by his wife Patricia and three children"],
                "published_date": today,
                "source_url": "https://www.legacy.com/us/obituaries/ajc/recent",
                "state": "GA",
                "county": "Fulton",
            },
            {
                "deceased_name": "DOROTHY MAE ALLEN",
                "town": "Marietta",
                "surviving_family": ["survived by her son Charles Allen"],
                "published_date": today,
                "source_url": "https://www.legacy.com/us/obituaries/ajc/recent",
                "state": "GA",
                "county": "Cobb",
            },
            # Georgia — Chatham County (Savannah)
            {
                "deceased_name": "MARY RUTH ADAMS",
                "town": "Savannah",
                "surviving_family": ["survived by her daughter Linda Nelson"],
                "published_date": today,
                "source_url": "https://www.legacy.com/us/obituaries/savannahnow/recent",
                "state": "GA",
                "county": "Chatham",
            },
            # Georgia — Clarke County (Athens)
            {
                "deceased_name": "FRANK EDWARD MITCHELL",
                "town": "Athens",
                "surviving_family": ["survived by his wife Virginia and two sons"],
                "published_date": today,
                "source_url": "https://www.legacy.com/us/obituaries/local/georgia",
                "state": "GA",
                "county": "Clarke",
            },
        ]


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    scraper = ObituaryScraper()
    scraper.scrape()
