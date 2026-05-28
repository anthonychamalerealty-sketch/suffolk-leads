"""
scrapers/social_signals.py
--------------------------
Scrapes Craigslist and Reddit for motivated-seller posts in both New York
(Suffolk County / Long Island) and Georgia (Atlanta, Savannah, and Athens areas).

New York sources:
  - Craigslist Long Island (longisland.craigslist.org) — housing (hhh) and for-sale (sss)
  - Reddit r/longisland

Georgia sources:
  - Craigslist Atlanta (atlanta.craigslist.org) — housing (hhh) and for-sale (sss)
  - Craigslist Savannah (savannah.craigslist.org) — housing (hhh) and for-sale (sss)
  - Reddit r/Atlanta
  - Reddit r/Athens

Keywords monitored:
  'must sell', 'motivated seller', 'estate sale', 'as-is', 'cash only', 'divorce',
  'relocating', 'behind on payments', 'inherited', 'fixer upper', 'needs work'

Extracts street addresses from matching posts, cross-references them against
the properties table, and inserts matches into the leads table with
source='social_signal' and the appropriate state/county tags.

Rate limit: 1 request per 3 seconds.
Usage:
  python scrapers/social_signals.py
"""

import sys
import os
import re
import json
import time
import logging
import datetime
import requests
from bs4 import BeautifulSoup

# Ensure project root is on sys.path so database imports work
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from database import SessionLocal, Property, Lead
from scrapers.base_scraper import BaseScraper

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("SocialSignalsScraper")

# ---------------------------------------------------------------------------
# Constants & Keywords
# ---------------------------------------------------------------------------
KEYWORDS = [
    'must sell', 'motivated seller', 'estate sale', 'as-is', 'cash only', 'divorce',
    'relocating', 'behind on payments', 'inherited', 'fixer upper', 'needs work'
]

# Standard US street suffixes for address extraction
STREET_SUFFIXES = (
    r'Street|St|Road|Rd|Avenue|Ave|Drive|Dr|Lane|Ln|Way|Court|Ct|'
    r'Boulevard|Blvd|Place|Pl|Terrace|Ter|Parkway|Pkwy|Highway|Hwy|'
    r'Turnpike|Tpk|Tpke'
)

# Address matching regex: Number + Street Name + Suffix, optional Town, optional State, optional Zip
ADDRESS_REGEX = re.compile(
    r'\b(\d+)\s+([A-Za-z0-9\s\.\-\']+(?:' + STREET_SUFFIXES + r'))\b'
    r'(?:[\s,]+([A-Za-z\s\-\']+))?'
    r'(?:[\s,]+(?:NY|GA|New\s+York|Georgia))?'
    r'(?:[\s,]+(\d{5}))?\b',
    re.IGNORECASE
)

# ---------------------------------------------------------------------------
# Craigslist site configurations
# ---------------------------------------------------------------------------
CRAIGSLIST_SITES = [
    {
        "name": "Craigslist Long Island (NY)",
        "base_url": "https://longisland.craigslist.org",
        "state": "NY",
        "county": "Suffolk",
    },
    {
        "name": "Craigslist Atlanta (GA)",
        "base_url": "https://atlanta.craigslist.org",
        "state": "GA",
        "county": "",  # county resolved from address
    },
    {
        "name": "Craigslist Savannah (GA)",
        "base_url": "https://savannah.craigslist.org",
        "state": "GA",
        "county": "Chatham",  # Savannah is primarily Chatham County
    },
]

# ---------------------------------------------------------------------------
# Reddit subreddit configurations
# ---------------------------------------------------------------------------
REDDIT_SUBREDDITS = [
    {
        "name": "r/longisland",
        "subreddit": "longisland",
        "state": "NY",
        "county": "Suffolk",
    },
    {
        "name": "r/Atlanta",
        "subreddit": "Atlanta",
        "state": "GA",
        "county": "",  # county resolved from address
    },
    {
        "name": "r/Athens",
        "subreddit": "Athens",
        "state": "GA",
        "county": "Clarke",
    },
]

# Georgia city-to-county mapping for resolving county from address
GA_CITY_COUNTY: dict[str, str] = {
    "atlanta": "Fulton", "sandy springs": "Fulton", "roswell": "Fulton",
    "alpharetta": "Fulton", "johns creek": "Fulton", "east point": "Fulton",
    "lawrenceville": "Gwinnett", "duluth": "Gwinnett", "norcross": "Gwinnett",
    "suwanee": "Gwinnett", "buford": "Gwinnett", "snellville": "Gwinnett",
    "marietta": "Cobb", "smyrna": "Cobb", "kennesaw": "Cobb",
    "acworth": "Cobb", "powder springs": "Cobb",
    "decatur": "DeKalb", "tucker": "DeKalb", "chamblee": "DeKalb",
    "doraville": "DeKalb", "lithonia": "DeKalb",
    "savannah": "Chatham", "pooler": "Chatham", "garden city": "Chatham",
    "athens": "Clarke", "winterville": "Clarke",
}

class SocialSignalsScraper(BaseScraper):
    """
    Scrapes Craigslist and Reddit for motivated seller posts, extracts addresses,
    cross-references against the properties table, and saves leads.
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
        self.session = requests.Session()
        self.session.headers.update(self.headers)

    def scrape(self):
        """
        Main entry point. Performs scraping of Craigslist (Long Island, Atlanta,
        Savannah) and Reddit (r/longisland, r/Atlanta, r/Athens), extracts
        potential leads, and persists them to the database with state/county tags.
        """
        logger.info("Starting Social Signals Scraper (NY + GA)...")
        posts = []

        # 1. Scrape all Craigslist sites
        for site in CRAIGSLIST_SITES:
            logger.info("Scraping %s...", site["name"])
            cl_posts = self._scrape_craigslist_site(
                site["base_url"],
                state=site["state"],
                county=site["county"],
            )
            posts.extend(cl_posts)
            logger.info("Found %d relevant posts from %s.", len(cl_posts), site["name"])
            time.sleep(3)

        # 2. Scrape all Reddit subreddits
        for sub_cfg in REDDIT_SUBREDDITS:
            logger.info("Scraping %s...", sub_cfg["name"])
            reddit_posts = self._scrape_reddit_subreddit(
                sub_cfg["subreddit"],
                state=sub_cfg["state"],
                county=sub_cfg["county"],
            )
            posts.extend(reddit_posts)
            logger.info("Found %d relevant posts from %s.", len(reddit_posts), sub_cfg["name"])
            time.sleep(3)

        # 3. Process posts & cross-reference with database properties
        if posts:
            saved_count = self._save_leads(posts)
            logger.info("Successfully processed posts and saved %d new leads.", saved_count)
            return posts
        else:
            logger.info("No matching social signal posts found today.")
            return []

    # ------------------------------------------------------------------
    # Craigslist Scraper
    # ------------------------------------------------------------------
    def _scrape_craigslist_site(
        self,
        base_url: str,
        state: str = "NY",
        county: str = "Suffolk",
    ) -> list[dict]:
        """
        Scrapes a single Craigslist site's housing (hhh) and for-sale (sss)
        sections for motivated-seller keyword matches.

        Parameters
        ----------
        base_url : Craigslist regional base URL (e.g. https://atlanta.craigslist.org)
        state    : Two-letter state code for tagging records
        county   : County name for tagging records (empty string = resolve from address)
        """
        results = []
        sections = ['hhh', 'sss']
        query = " | ".join(f'"{kw}"' for kw in KEYWORDS)

        for sec in sections:
            url = f"{base_url}/search/{sec}"
            params = {"query": query}
            logger.info("Scraping Craigslist section '%s' at %s...", sec, base_url)
            try:
                res = self.session.get(url, params=params, timeout=12)
                if res.status_code != 200:
                    logger.error("Failed to fetch Craigslist section '%s': Status %d", sec, res.status_code)
                    continue

                soup = BeautifulSoup(res.text, "html.parser")
                items = soup.find_all(class_="cl-static-search-result")
                for item in items:
                    anchor = item.find("a")
                    if not anchor:
                        continue
                    post_url = anchor.get("href", "")
                    title_div = item.find(class_="title")
                    title = title_div.text.strip() if title_div else ""
                    price_div = item.find(class_="price")
                    price = price_div.text.strip() if price_div else ""
                    loc_div = item.find(class_="location")
                    location = loc_div.text.strip() if loc_div else ""

                    # Resolve county from location text for GA records
                    resolved_county = county
                    if state == "GA" and not county:
                        resolved_county = self._ga_county_from_text(location or title)

                    results.append({
                        "source": "craigslist",
                        "title": title,
                        "text": f"{title} (Location: {location}, Price: {price})",
                        "url": post_url,
                        "date": datetime.date.today().isoformat(),
                        "state": state,
                        "county": resolved_county,
                    })

            except Exception as e:
                logger.error("Error scraping Craigslist section '%s': %s", sec, e)
            time.sleep(3)

        return results

    # Keep backward-compatible alias
    def _scrape_craigslist(self) -> list[dict]:
        """Backward-compatible wrapper — scrapes Long Island Craigslist only."""
        return self._scrape_craigslist_site(
            "https://longisland.craigslist.org", state="NY", county="Suffolk"
        )

    # ------------------------------------------------------------------
    # Reddit Scraper
    # ------------------------------------------------------------------
    def _scrape_reddit_subreddit(
        self,
        subreddit: str,
        state: str = "NY",
        county: str = "Suffolk",
    ) -> list[dict]:
        """
        Scrapes a Reddit subreddit for the keyword list using the pullpush.io API.

        Parameters
        ----------
        subreddit : Subreddit name (e.g. 'longisland', 'Atlanta', 'Athens')
        state     : Two-letter state code for tagging records
        county    : County name for tagging records (empty string = resolve from text)
        """
        results = []
        logger.info("Scraping Reddit r/%s via pullpush.io...", subreddit)

        for kw in KEYWORDS:
            url = "https://api.pullpush.io/reddit/search/submission/"
            params = {"q": kw, "subreddit": subreddit, "size": 10}
            try:
                res = self.session.get(url, params=params, timeout=12)
                if res.status_code != 200:
                    logger.error(
                        "Failed to fetch Reddit r/%s for keyword '%s': Status %d",
                        subreddit, kw, res.status_code,
                    )
                    continue

                data = res.json()
                submissions = data.get("data", [])
                for sub in submissions:
                    title = sub.get("title", "")
                    selftext = sub.get("selftext", "")
                    permalink = sub.get("permalink", "")
                    post_url = f"https://reddit.com{permalink}" if permalink else ""
                    combined_text = f"{title}\n\n{selftext}"

                    # Resolve county from post text for GA records
                    resolved_county = county
                    if state == "GA" and not county:
                        resolved_county = self._ga_county_from_text(combined_text)

                    results.append({
                        "source": "reddit",
                        "title": title,
                        "text": combined_text,
                        "url": post_url,
                        "date": datetime.date.today().isoformat(),
                        "state": state,
                        "county": resolved_county,
                    })

            except Exception as e:
                logger.error("Error scraping Reddit r/%s for keyword '%s': %s", subreddit, kw, e)
            time.sleep(3)

        return results

    # Keep backward-compatible alias
    def _scrape_reddit(self) -> list[dict]:
        """Backward-compatible wrapper — scrapes r/longisland only."""
        return self._scrape_reddit_subreddit("longisland", state="NY", county="Suffolk")

    # ------------------------------------------------------------------
    # Georgia county resolution helper
    # ------------------------------------------------------------------
    @staticmethod
    def _ga_county_from_text(text: str) -> str:
        """
        Attempts to derive a Georgia county from free-form text by matching
        known city names.  Returns an empty string if no match is found.
        """
        if not text:
            return ""
        lower = text.lower()
        for city, county in GA_CITY_COUNTY.items():
            if city in lower:
                return county
        return ""

    # ------------------------------------------------------------------
    # Address Extraction & Matching
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_address(text: str) -> str | None:
        """
        Attempts to extract a street address using regex patterns common to Long Island.
        Returns standardized address string or None.
        """
        match = ADDRESS_REGEX.search(text)
        if match:
            # Reconstruct the matched address nicely
            # group(1) = number, group(2) = street name + suffix
            addr = f"{match.group(1)} {match.group(2)}"
            
            # group(3) = optional town
            if match.group(3):
                addr += f", {match.group(3).strip()}"
            # group(4) = optional zip
            if match.group(4):
                addr += f" {match.group(4)}"
            return addr.strip()
        return None

    @staticmethod
    def _address_matches_property(scraped_address: str, prop_address: str) -> bool:
        """
        Flexible and robust address match. Returns True if:
        1. Either address is a substring of the other after basic normalization.
        2. Or, the street numbers match and the street names match/overlap.
        """
        def norm(s):
            s = s.upper()
            s = re.sub(r'[.,\s]+', ' ', s)
            return s.strip()

        s_norm = norm(scraped_address)
        p_norm = norm(prop_address)

        if s_norm in p_norm or p_norm in s_norm:
            return True

        # Extract number and street name for structural comparison
        suffixes = (
            r'STREET|ST|ROAD|RD|AVENUE|AVE|DRIVE|DR|LANE|LN|WAY|COURT|CT|'
            r'BOULEVARD|BLVD|PLACE|PL|TERRACE|TER|PARKWAY|PKWY|HIGHWAY|HWY|'
            r'TURNPIKE|TPK|TPKE'
        )
        pattern = r'^(\d+)\s+([A-Z0-9\s]+?)(?:\s+(?:' + suffixes + r'))?(?:\s|$)'
        
        m_s = re.match(pattern, s_norm)
        m_p = re.match(pattern, p_norm)

        if m_s and m_p:
            num_s, street_s = m_s.groups()
            num_p, street_p = m_p.groups()
            if num_s == num_p and (street_s in street_p or street_p in street_s):
                return True

        return False

    # ------------------------------------------------------------------
    # Database Persistence
    # ------------------------------------------------------------------
    def _save_leads(self, posts: list[dict]) -> int:
        """
        Processes posts, extracts addresses, cross-references against the
        properties table, and inserts matched records into the leads table.
        Returns the number of new leads inserted.
        """
        saved_count = 0
        session = SessionLocal()
        try:
            properties = session.query(Property).all()
            logger.info(f"Loaded {len(properties)} properties from the database for cross-referencing.")

            for post in posts:
                # Attempt to extract street address from title or combined text
                extracted_addr = self._extract_address(post["text"])
                if not extracted_addr:
                    # Try title specifically if combined text did not yield anything
                    extracted_addr = self._extract_address(post["title"])
                
                if not extracted_addr:
                    continue

                logger.info(f"Extracted address '{extracted_addr}' from post: '{post['title'][:40]}...'")

                # Cross-reference against properties table
                for prop in properties:
                    if not self._address_matches_property(extracted_addr, prop.address):
                        continue

                    logger.info(
                        "MATCH FOUND: '%s' (from %s) -> property '%s' (owner: %s)",
                        extracted_addr, post["source"], prop.address, prop.owner_name
                    )

                    # Deduplication: one social_signal lead per parcel per post URL
                    existing = (
                        session.query(Lead)
                        .filter(
                            Lead.parcel_id == prop.parcel_id,
                            Lead.source == "social_signal",
                            Lead.raw_data.like(f'%"{post["url"]}"%') | Lead.raw_data.like(f'%{post["url"]}%')
                        )
                        .first()
                    )
                    if existing:
                        logger.info(f"  Lead already exists for parcel {prop.parcel_id} with this post — skipping.")
                        continue

                    raw_data = {
                        "scraped_address": extracted_addr,
                        "post_title": post["title"],
                        "post_text": post["text"],
                        "post_url": post["url"],
                        "source": post["source"],
                        "date": post["date"],
                        "owner_name": prop.owner_name,
                    }

                    # Social signal leads are high-intent/motivated seller signals
                    lead = Lead(
                        address=prop.address,
                        parcel_id=prop.parcel_id,
                        source="social_signal",
                        raw_data=json.dumps(raw_data),
                        score=0.80,  # Highly motivated signal
                        status="new",
                        state=post.get("state", getattr(prop, "state", "NY")),
                        county=post.get("county", getattr(prop, "county", "Suffolk")),
                    )
                    session.add(lead)
                    saved_count += 1
                    logger.info(f"  Saved new lead for parcel {prop.parcel_id} (score=0.80)")

            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"Error saving leads to database: {e}")
            raise
        finally:
            session.close()

        return saved_count

if __name__ == "__main__":
    scraper = SocialSignalsScraper()
    scraper.scrape()
