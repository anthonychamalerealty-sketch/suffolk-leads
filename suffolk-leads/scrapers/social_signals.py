"""
scrapers/social_signals.py
--------------------------
Scrapes Craigslist Suffolk County housing/for-sale sections and Reddit r/longisland
for posts containing specific motivated-seller keywords.
Extracts street addresses from matching posts, cross-references them against
the properties table, and inserts matches into the leads table with source='social_signal'.

Keywords:
  'must sell', 'motivated seller', 'estate sale', 'as-is', 'cash only', 'divorce',
  'relocating', 'behind on payments', 'inherited', 'fixer upper', 'needs work'

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
    r'(?:[\s,]+(?:NY|New\s+York))?'
    r'(?:[\s,]+(11[0-9]{3}))?\b',
    re.IGNORECASE
)

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
        Main entry point. Performs scraping of Craigslist and Reddit,
        extracts potential leads, and persists them to the database.
        """
        logger.info("Starting Social Signals Scraper...")
        posts = []

        # 1. Scrape Craigslist
        cl_posts = self._scrape_craigslist()
        posts.extend(cl_posts)
        logger.info(f"Found {len(cl_posts)} relevant posts from Craigslist.")

        # Rate limit between sources
        time.sleep(3)

        # 2. Scrape Reddit
        reddit_posts = self._scrape_reddit()
        posts.extend(reddit_posts)
        logger.info(f"Found {len(reddit_posts)} relevant posts from Reddit r/longisland.")

        # 3. Process posts & cross-reference with database properties
        if posts:
            saved_count = self._save_leads(posts)
            logger.info(f"Successfully processed posts and saved {saved_count} new leads.")
            return posts
        else:
            logger.info("No matching social signal posts found today.")
            return []

    # ------------------------------------------------------------------
    # Craigslist Scraper
    # ------------------------------------------------------------------
    def _scrape_craigslist(self) -> list[dict]:
        """
        Scrapes Craigslist Suffolk County housing and for-sale sections.
        Since Craigslist has an 'OR' operator '|', we can query multiple keywords.
        """
        results = []
        # We check housing (hhh) and for-sale (sss) sections
        sections = ['hhh', 'sss']
        
        # Build query with OR operator: e.g. "must sell" | "motivated seller" | ...
        query = " | ".join(f'"{kw}"' for kw in KEYWORDS)

        for sec in sections:
            url = f"https://longisland.craigslist.org/search/{sec}"
            params = {"query": query}
            
            logger.info(f"Scraping Craigslist section '{sec}' with query: {query[:50]}...")
            try:
                res = self.session.get(url, params=params)
                if res.status_code != 200:
                    logger.error(f"Failed to fetch Craigslist section '{sec}': Status {res.status_code}")
                    continue

                soup = BeautifulSoup(res.text, "html.parser")
                
                # Parse static search results
                items = soup.find_all(class_="cl-static-search-result")
                for item in items:
                    anchor = item.find("a")
                    if not anchor:
                        continue
                    
                    post_url = anchor.get("href", "")
                    title_div = item.find(class_="title")
                    title = title_div.text.strip() if title_div else ""
                    
                    # Optional details
                    price_div = item.find(class_="price")
                    price = price_div.text.strip() if price_div else ""
                    loc_div = item.find(class_="location")
                    location = loc_div.text.strip() if loc_div else ""

                    # For Craigslist static results, the text is usually just the title.
                    # We will use title as the text for matching/extraction.
                    results.append({
                        "source": "craigslist",
                        "title": title,
                        "text": f"{title} (Location: {location}, Price: {price})",
                        "url": post_url,
                        "date": datetime.date.today().isoformat()
                    })

            except Exception as e:
                logger.error(f"Error scraping Craigslist section '{sec}': {e}")
            
            # Rate limit requests
            time.sleep(3)

        return results

    # ------------------------------------------------------------------
    # Reddit Scraper
    # ------------------------------------------------------------------
    def _scrape_reddit(self) -> list[dict]:
        """
        Scrapes Reddit r/longisland for the same keyword list using pullpush.io API.
        """
        results = []
        logger.info("Scraping Reddit r/longisland via pullpush.io...")

        for kw in KEYWORDS:
            url = "https://api.pullpush.io/reddit/search/submission/"
            params = {
                "q": kw,
                "subreddit": "longisland",
                "size": 10
            }
            try:
                res = self.session.get(url, params=params)
                if res.status_code != 200:
                    logger.error(f"Failed to fetch Reddit search for keyword '{kw}': Status {res.status_code}")
                    continue

                data = res.json()
                submissions = data.get("data", [])
                for sub in submissions:
                    title = sub.get("title", "")
                    selftext = sub.get("selftext", "")
                    permalink = sub.get("permalink", "")
                    post_url = f"https://reddit.com{permalink}" if permalink else ""
                    
                    # Combine title and body for complete post text
                    combined_text = f"{title}\n\n{selftext}"
                    
                    results.append({
                        "source": "reddit",
                        "title": title,
                        "text": combined_text,
                        "url": post_url,
                        "date": datetime.date.today().isoformat()
                    })

            except Exception as e:
                logger.error(f"Error scraping Reddit for keyword '{kw}': {e}")
            
            # Rate limit requests
            time.sleep(3)

        return results

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
