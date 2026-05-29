"""
enrich.py
---------
Tiered contact enrichment for Suffolk Leads.

Tier 1 — Free
  • Store the owner's mailing address (already in ``properties``) as the
    primary contact record.  Direct mail remains the highest-conversion
    outreach channel for motivated sellers.
  • Scrape USPhoneBook.com with Playwright: 5-second inter-request delay,
    human-like mouse movement, rotating user-agent.  Extract phone numbers.
    If a CAPTCHA is detected, skip the record and set
    ``lead.enrich_status = 'needs_manual_lookup'``.

Tier 2 — Near-free fallback
  • NumVerify API  (env: NUMVERIFY_API_KEY)  — validate every phone number
    found in Tier 1.  Free tier: 250 lookups / month.
  • Hunter.io API  (env: HUNTER_API_KEY)     — email lookup by owner name +
    city.  Free tier: 25 lookups / month.

Tier 3 — Paid flag
  • If Tier 1 and Tier 2 return nothing, set
    ``lead.enrich_status = 'needs_paid_lookup'`` for manual BatchSkipTracing
    batching ($0.15 / record).

Lead scoring (1–10, additive)
  • +3  source == 'fire_report'
  • +3  source == 'probate'
  • +2  source == 'obituary'
  • +1  source == 'social_signal'
  • +1  last sale > 10 years ago
  Score is clamped to [1, 10].

All contact records are written to the ``contacts`` table with a ``source``
field: 'direct_mail', 'usphonebook', 'numverify', 'hunter', or 'manual'.

Usage
-----
  python processor/enrich.py                  # enrich all 'new' leads
  python processor/enrich.py --lead-id 42     # enrich a single lead
  python processor/enrich.py --limit 20       # process at most 20 leads
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import logging
import os
import random
import re
import sys
import time
from typing import Optional

try:
    import requests
except ImportError as _e:
    print(f"[enrich] IMPORT ERROR (requests): {_e} — install with: pip install requests", flush=True)
    raise

# ---------------------------------------------------------------------------
# Path setup — allow running as a script from any working directory
# ---------------------------------------------------------------------------
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from database import Base, Contact, Lead, Property, SessionLocal, engine
except Exception as _e:
    print(f"[enrich] IMPORT ERROR (database): {_e}", flush=True)
    import traceback; traceback.print_exc()
    raise
from sqlalchemy import Column, String, text

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("enrich")

# ---------------------------------------------------------------------------
# Schema migration — add columns that did not exist in the original schema
# ---------------------------------------------------------------------------

def _ensure_columns() -> None:
    """
    Idempotently add ``leads.enrich_status`` and ``contacts.mailing_address``
    if they are not already present.  Works for both SQLite and PostgreSQL.
    """
    with engine.connect() as conn:
        # --- leads.enrich_status ---
        try:
            conn.execute(
                text("ALTER TABLE leads ADD COLUMN enrich_status VARCHAR DEFAULT 'pending'")
            )
            conn.commit()
            logger.info("Added column leads.enrich_status")
        except Exception:
            conn.rollback()  # column already exists — safe to ignore

        # --- contacts.mailing_address ---
        try:
            conn.execute(
                text("ALTER TABLE contacts ADD COLUMN mailing_address VARCHAR")
            )
            conn.commit()
            logger.info("Added column contacts.mailing_address")
        except Exception:
            conn.rollback()

        # --- contacts.phone_valid ---
        try:
            conn.execute(
                text("ALTER TABLE contacts ADD COLUMN phone_valid VARCHAR")
            )
            conn.commit()
            logger.info("Added column contacts.phone_valid")
        except Exception:
            conn.rollback()

        # --- contacts.phone_line_type ---
        try:
            conn.execute(
                text("ALTER TABLE contacts ADD COLUMN phone_line_type VARCHAR")
            )
            conn.commit()
            logger.info("Added column contacts.phone_line_type")
        except Exception:
            conn.rollback()

        # --- contacts.phone_carrier ---
        try:
            conn.execute(
                text("ALTER TABLE contacts ADD COLUMN phone_carrier VARCHAR")
            )
            conn.commit()
            logger.info("Added column contacts.phone_carrier")
        except Exception:
            conn.rollback()

# ---------------------------------------------------------------------------
# Rotating user-agent pool
# ---------------------------------------------------------------------------
_USER_AGENTS: list[str] = [
    # Chrome on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Firefox on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
    "Gecko/20100101 Firefox/125.0",
    # Firefox on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) "
    "Gecko/20100101 Firefox/125.0",
    # Edge on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    # Safari on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]


def _random_ua() -> str:
    return random.choice(_USER_AGENTS)


# ---------------------------------------------------------------------------
# Lead scoring
# ---------------------------------------------------------------------------
_SOURCE_SCORES: dict[str, int] = {
    "fire_report": 3,
    "probate": 3,
    "obituary": 2,
    "social_signal": 1,
}

_TEN_YEARS_AGO = datetime.date.today() - datetime.timedelta(days=365 * 10)


def compute_lead_score(lead: Lead, prop: Optional[Property]) -> int:
    """
    Return an integer score in [1, 10] based on source and last-sale age.

    Scoring rules
    -------------
    +3  source == 'fire_report'
    +3  source == 'probate'
    +2  source == 'obituary'
    +1  source == 'social_signal'
    +1  last sale date > 10 years ago
    """
    score = 0
    score += _SOURCE_SCORES.get(lead.source, 0)

    if prop and prop.last_sale_date and prop.last_sale_date not in ("N/A", "", None):
        try:
            sale_date = datetime.date.fromisoformat(prop.last_sale_date)
            if sale_date < _TEN_YEARS_AGO:
                score += 1
        except ValueError:
            pass  # unparseable date — skip the bonus

    # Clamp to [1, 10]
    return max(1, min(10, score))


# ---------------------------------------------------------------------------
# Tier 1a — Direct-mail contact record
# ---------------------------------------------------------------------------

def store_mailing_address(session, lead: Lead, prop: Property) -> Contact:
    """
    Upsert a 'direct_mail' contact record for the owner's mailing address.
    This is always stored first — it is the highest-ROI outreach channel.
    """
    existing = (
        session.query(Contact)
        .filter(
            Contact.lead_id == lead.id,
            Contact.source == "direct_mail",
        )
        .first()
    )
    if existing:
        logger.debug("Direct-mail contact already exists for lead %d", lead.id)
        return existing

    contact = Contact(
        lead_id=lead.id,
        owner_name=prop.owner_name,
        phone=None,
        email=None,
        source="direct_mail",
    )
    # mailing_address is a dynamically-added column; set via __dict__ so
    # SQLAlchemy does not raise AttributeError on older mapped instances.
    contact.__dict__["mailing_address"] = prop.owner_mailing_address

    session.add(contact)
    session.flush()
    logger.info(
        "Stored direct-mail contact for lead %d — %s (%s)",
        lead.id,
        prop.owner_name,
        prop.owner_mailing_address,
    )
    return contact


# ---------------------------------------------------------------------------
# Tier 1b — USPhoneBook.com scrape via Playwright
# ---------------------------------------------------------------------------

# CAPTCHA detection heuristics
_CAPTCHA_SIGNALS: list[str] = [
    "captcha",
    "recaptcha",
    "hcaptcha",
    "are you a human",
    "verify you are human",
    "robot",
    "cloudflare",
    "access denied",
    "just a moment",
    "ddos-guard",
    "please enable javascript",
    "challenge",
]

_PHONE_RE = re.compile(r"\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}")


def _is_captcha_page(content: str) -> bool:
    lower = content.lower()
    return any(signal in lower for signal in _CAPTCHA_SIGNALS)


def _extract_phones_from_html(html: str) -> list[str]:
    """Return a deduplicated list of US phone numbers found in *html*."""
    raw = _PHONE_RE.findall(html)
    # Normalise to digits-only then format as (NXX) NXX-XXXX
    seen: set[str] = set()
    phones: list[str] = []
    for p in raw:
        digits = re.sub(r"\D", "", p)
        if len(digits) == 10 and digits not in seen:
            seen.add(digits)
            phones.append(f"({digits[:3]}) {digits[3:6]}-{digits[6:]}")
    return phones


async def _human_mouse_move(page, x_end: int, y_end: int) -> None:
    """
    Move the mouse from its current position to (x_end, y_end) in small
    random steps to mimic human cursor movement.
    """
    steps = random.randint(8, 18)
    # Playwright's mouse starts at (0, 0) — read current position if possible
    x_start = random.randint(100, 600)
    y_start = random.randint(100, 400)
    for i in range(1, steps + 1):
        t = i / steps
        # Ease-in-out cubic
        ease = t * t * (3 - 2 * t)
        x = int(x_start + (x_end - x_start) * ease + random.randint(-3, 3))
        y = int(y_start + (y_end - y_start) * ease + random.randint(-3, 3))
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.02, 0.07))


async def _scrape_usphonebook(owner_name: str, mailing_address: str) -> list[str]:
    """
    Search USPhoneBook.com for *owner_name* and return any phone numbers found.

    Returns an empty list if:
    - playwright is not installed
    - a CAPTCHA is detected  (caller should set enrich_status accordingly)
    - no results are found

    Raises ``CaptchaDetectedError`` if a CAPTCHA wall is encountered.
    """
    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    except ImportError:
        logger.warning(
            "playwright not installed — skipping USPhoneBook scrape. "
            "Run: pip install playwright && playwright install chromium"
        )
        return []

    # Parse first/last name and city from the mailing address
    name_parts = owner_name.strip().split()
    first_name = name_parts[0] if name_parts else ""
    last_name = name_parts[-1] if len(name_parts) > 1 else ""

    # Extract city from mailing address (e.g. "123 MAIN ST, BABYLON, NY 11702")
    city = ""
    addr_parts = mailing_address.split(",")
    if len(addr_parts) >= 2:
        city = addr_parts[-2].strip().title()

    search_url = (
        "https://www.usphonebook.com/"
        f"{first_name.lower()}-{last_name.lower()}"
        + (f"/{city.lower().replace(' ', '-')}" if city else "")
    )

    logger.info("USPhoneBook search URL: %s", search_url)

    phones: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            user_agent=_random_ua(),
            viewport={"width": random.randint(1280, 1920), "height": random.randint(768, 1080)},
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "DNT": "1",
            },
        )

        # Mask webdriver fingerprint
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page = await context.new_page()

        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)

            # Human-like pause after page load
            await asyncio.sleep(random.uniform(1.5, 3.0))

            content = await page.content()

            if _is_captcha_page(content):
                logger.warning("CAPTCHA detected on USPhoneBook for '%s'", owner_name)
                raise CaptchaDetectedError(owner_name)

            # Simulate reading the page — move mouse around result cards
            await _human_mouse_move(page, random.randint(300, 700), random.randint(200, 500))
            await asyncio.sleep(random.uniform(0.8, 1.8))

            phones = _extract_phones_from_html(content)
            logger.info(
                "USPhoneBook returned %d phone(s) for '%s'", len(phones), owner_name
            )

        except CaptchaDetectedError:
            raise
        except PWTimeout:
            logger.warning("Playwright timeout for '%s'", owner_name)
        except Exception as exc:
            logger.warning("USPhoneBook scrape error for '%s': %s", owner_name, exc)
        finally:
            await browser.close()

    return phones


class CaptchaDetectedError(Exception):
    """Raised when USPhoneBook.com returns a CAPTCHA challenge page."""


# ---------------------------------------------------------------------------
# Tier 1c — TruePeopleSearch phone lookup (probate / petition leads)
# ---------------------------------------------------------------------------

async def _scrape_truepeoplesearch(name: str, address: str) -> list[str]:
    """
    Search TruePeopleSearch.com for *name* at *address* and return any phone
    numbers found.

    URL format:
      https://www.truepeoplesearch.com/results?name=FIRST+LAST&citystatezip=CITY+STATE+ZIP

    Returns an empty list on failure.  Raises CaptchaDetectedError on CAPTCHA.
    """
    import urllib.parse
    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    except ImportError:
        logger.warning("playwright not installed — skipping TruePeopleSearch")
        return []

    name_parts = name.strip().split()
    first_name = name_parts[0] if name_parts else ""
    last_name   = name_parts[-1] if len(name_parts) > 1 else ""

    # Extract city/state/zip from address string
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
    logger.info("TruePeopleSearch URL: %s", search_url)
    print(f"[enrich] TruePeopleSearch URL: {search_url}", flush=True)

    phones: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=_random_ua(),
            viewport={"width": random.randint(1280, 1920), "height": random.randint(768, 1080)},
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "DNT": "1",
            },
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()
        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(random.uniform(2.0, 4.0))
            content = await page.content()

            if _is_captcha_page(content):
                logger.warning("CAPTCHA on TruePeopleSearch for '%s'", name)
                print(f"[enrich] CAPTCHA on TruePeopleSearch for '{name}' — marking needs_manual_lookup", flush=True)
                raise CaptchaDetectedError(name)

            await _human_mouse_move(page, random.randint(300, 700), random.randint(200, 500))
            await asyncio.sleep(random.uniform(1.0, 2.5))

            # Try to click into the first profile for more phone numbers
            try:
                profile_link = await page.query_selector(
                    "a[href*='/find/person/'], a[href*='/results/'], .card-summary a"
                )
                if profile_link:
                    href = await profile_link.get_attribute("href")
                    if href and not href.startswith("http"):
                        href = "https://www.truepeoplesearch.com" + href
                    print(f"[enrich] TruePeopleSearch: clicking profile {href}", flush=True)
                    await _human_mouse_move(page, random.randint(200, 600), random.randint(200, 400))
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                    await page.goto(href, wait_until="domcontentloaded", timeout=30_000)
                    await asyncio.sleep(random.uniform(3.0, 5.0))
                    content = await page.content()
                    if _is_captcha_page(content):
                        raise CaptchaDetectedError(name)
            except CaptchaDetectedError:
                raise
            except Exception as _link_err:
                logger.debug("TruePeopleSearch profile click failed: %s", _link_err)

            phones = _extract_phones_from_html(content)
            logger.info("TruePeopleSearch: %d phone(s) for '%s'", len(phones), name)
            print(f"[enrich] TruePeopleSearch: {len(phones)} phone(s) found for '{name}'", flush=True)

        except CaptchaDetectedError:
            raise
        except PWTimeout:
            logger.warning("TruePeopleSearch timeout for '%s'", name)
            print(f"[enrich] TruePeopleSearch timeout for '{name}'", flush=True)
        except Exception as exc:
            logger.warning("TruePeopleSearch error for '%s': %s", name, exc)
            print(f"[enrich] TruePeopleSearch error for '{name}': {exc}", flush=True)
        finally:
            await browser.close()
    return phones


# ---------------------------------------------------------------------------
# Tier 2a — NumVerify phone validation
# ---------------------------------------------------------------------------

_NUMVERIFY_BASE = "http://apilayer.net/api/validate"


def validate_phone_numverify(phone: str) -> dict:
    """
    Validate *phone* via the NumVerify API.

    Returns a dict with keys: valid, line_type, carrier, country_code.
    Returns an empty dict on error or missing API key.
    """
    api_key = os.getenv("NUMVERIFY_API_KEY", "").strip()
    if not api_key:
        logger.debug("NUMVERIFY_API_KEY not set — skipping NumVerify validation")
        return {}

    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        digits = "1" + digits  # prepend US country code

    try:
        resp = requests.get(
            _NUMVERIFY_BASE,
            params={"access_key": api_key, "number": digits, "country_code": "US", "format": 1},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            logger.warning("NumVerify error: %s", data["error"])
            return {}

        return {
            "valid": data.get("valid", False),
            "line_type": data.get("line_type", ""),
            "carrier": data.get("carrier", ""),
            "country_code": data.get("country_code", ""),
        }
    except Exception as exc:
        logger.warning("NumVerify request failed for %s: %s", phone, exc)
        return {}


# ---------------------------------------------------------------------------
# Tier 2b — Hunter.io email lookup
# ---------------------------------------------------------------------------

_HUNTER_BASE = "https://api.hunter.io/v2/email-finder"


def lookup_email_hunter(owner_name: str, mailing_address: str) -> Optional[str]:
    """
    Attempt to find an email address for *owner_name* via Hunter.io.

    Uses the owner's city (parsed from mailing address) as the domain hint.
    Returns the email string on success, or None.
    """
    api_key = os.getenv("HUNTER_API_KEY", "").strip()
    if not api_key:
        logger.debug("HUNTER_API_KEY not set — skipping Hunter.io lookup")
        return None

    name_parts = owner_name.strip().split()
    first_name = name_parts[0].title() if name_parts else ""
    last_name = name_parts[-1].title() if len(name_parts) > 1 else ""

    if not first_name or not last_name:
        return None

    # Hunter requires a domain; derive a plausible one from the city name.
    # For residential owners this will rarely match, but the free-tier quota
    # is small (25/month) so we only attempt it when we have a clean name.
    addr_parts = mailing_address.split(",")
    city = addr_parts[-2].strip().lower().replace(" ", "") if len(addr_parts) >= 2 else ""
    domain = f"{city}.com" if city else "gmail.com"

    try:
        resp = requests.get(
            _HUNTER_BASE,
            params={
                "first_name": first_name,
                "last_name": last_name,
                "domain": domain,
                "api_key": api_key,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        email = data.get("data", {}).get("email")
        score = data.get("data", {}).get("score", 0)

        if email and score >= 50:
            logger.info(
                "Hunter.io found email '%s' (score %d) for '%s'",
                email,
                score,
                owner_name,
            )
            return email

        logger.debug(
            "Hunter.io: no confident email for '%s' (score=%s)", owner_name, score
        )
        return None

    except Exception as exc:
        logger.warning("Hunter.io request failed for '%s': %s", owner_name, exc)
        return None


# ---------------------------------------------------------------------------
# Core enrichment logic — single lead
# ---------------------------------------------------------------------------

def enrich_lead(lead_id: int) -> None:
    """
    Run the full enrichment pipeline for a single lead identified by *lead_id*.

    Steps
    -----
    1. Load lead + property from DB.
    2. Compute and persist lead score.
    3. Tier 1a: store direct-mail contact.
    4. Tier 1b: USPhoneBook scrape → store contacts.
    5. Tier 2a: NumVerify validation for each phone found.
    6. Tier 2b: Hunter.io email lookup.
    7. Tier 3:  flag as 'needs_paid_lookup' if nothing found.
    8. Update lead.status = 'enriched'.
    """
    session = SessionLocal()
    try:
        lead = session.query(Lead).filter(Lead.id == lead_id).first()
        if not lead:
            logger.warning("Lead %d not found", lead_id)
            return

        prop = (
            session.query(Property)
            .filter(Property.parcel_id == lead.parcel_id)
            .first()
        )
        if not prop:
            logger.warning(
                "No property record for lead %d (parcel_id=%s) — checking for petition contact",
                lead_id, lead.parcel_id,
            )
            print(f"[enrich] Lead {lead_id}: no property record — checking for petition_document contact", flush=True)
            # ------------------------------------------------------------------
            # Probate / petition leads have no property record but DO have a
            # petitioner contact saved with source='petition_document'.  Run
            # TruePeopleSearch as Tier 2 phone fallback if no phone was found
            # in the petition PDF.
            # ------------------------------------------------------------------
            petition_contacts = (
                session.query(Contact)
                .filter(
                    Contact.lead_id == lead_id,
                    Contact.source == "petition_document",
                )
                .all()
            )
            if petition_contacts:
                print(f"[enrich] Lead {lead_id}: found {len(petition_contacts)} petition_document contact(s)", flush=True)
                # Score the lead based on source alone (no property)
                new_score = compute_lead_score(lead, None)
                lead.score = float(new_score)
                print(f"[enrich] Lead {lead_id}: score={new_score}/10", flush=True)

                for pc in petition_contacts:
                    petitioner_name = pc.owner_name or ""
                    petitioner_phone = pc.phone or ""
                    petitioner_addr = pc.__dict__.get("mailing_address") or lead.address or ""

                    if petitioner_phone:
                        print(
                            f"[enrich] Lead {lead_id}: petition phone already present ({petitioner_phone}) — Tier 2 skipped",
                            flush=True,
                        )
                        continue  # Tier 1 already has a phone — skip TruePeopleSearch

                    if not petitioner_name:
                        print(f"[enrich] Lead {lead_id}: no petitioner name — skipping TruePeopleSearch", flush=True)
                        continue

                    print(
                        f"[enrich] Lead {lead_id}: no petition phone for '{petitioner_name}' — running TruePeopleSearch",
                        flush=True,
                    )
                    try:
                        tps_phones = asyncio.run(
                            _scrape_truepeoplesearch(petitioner_name, petitioner_addr)
                        )
                    except CaptchaDetectedError:
                        lead.__dict__["enrich_status"] = "needs_manual_lookup"
                        tps_phones = []
                    except Exception as _tps_exc:
                        logger.error("TruePeopleSearch failed for lead %d: %s", lead_id, _tps_exc)
                        print(f"[enrich] TruePeopleSearch ERROR for lead {lead_id}: {_tps_exc}", flush=True)
                        tps_phones = []

                    for ph in tps_phones:
                        _upsert_contact(
                            session,
                            lead_id=lead_id,
                            owner_name=petitioner_name,
                            phone=ph,
                            email=None,
                            source="truepeoplesearch",
                            mailing_address=petitioner_addr,
                        )
                        print(f"[enrich] Lead {lead_id}: saved TruePeopleSearch phone {ph}", flush=True)

                    # 5-second delay between TruePeopleSearch lookups
                    if len(petition_contacts) > 1:
                        time.sleep(5.0 + random.uniform(0.0, 2.0))

            else:
                print(f"[enrich] Lead {lead_id}: no petition_document contact — marking no_property_record", flush=True)

            lead.status = "enriched"
            if not lead.__dict__.get("enrich_status"):
                lead.__dict__["enrich_status"] = "no_property_record"
            session.commit()
            print(f"[enrich] Lead {lead_id}: enrichment complete (no-property path)", flush=True)
            return

        # ------------------------------------------------------------------
        # Step 2 — Lead scoring
        # ------------------------------------------------------------------
        new_score = compute_lead_score(lead, prop)
        lead.score = float(new_score)
        logger.info("Lead %d scored %d/10 (source=%s)", lead_id, new_score, lead.source)

        # ------------------------------------------------------------------
        # Step 3 — Tier 1a: direct-mail contact
        # ------------------------------------------------------------------
        store_mailing_address(session, lead, prop)
        session.flush()

        # ------------------------------------------------------------------
        # Step 4 — Tier 1b: USPhoneBook scrape
        # ------------------------------------------------------------------
        phones_found: list[str] = []
        captcha_hit = False

        if prop.owner_name and prop.owner_mailing_address:
            try:
                phones_found = asyncio.run(
                    _scrape_usphonebook(prop.owner_name, prop.owner_mailing_address)
                )
            except CaptchaDetectedError:
                captcha_hit = True
                lead.__dict__["enrich_status"] = "needs_manual_lookup"
                logger.warning(
                    "Lead %d flagged as needs_manual_lookup (CAPTCHA)", lead_id
                )
            except Exception as exc:
                logger.error(
                    "Unexpected error during USPhoneBook scrape for lead %d: %s",
                    lead_id,
                    exc,
                )

        # Persist raw USPhoneBook phone contacts
        for phone in phones_found:
            _upsert_contact(
                session,
                lead_id=lead.id,
                owner_name=prop.owner_name,
                phone=phone,
                email=None,
                source="usphonebook",
                mailing_address=prop.owner_mailing_address,
            )

        # ------------------------------------------------------------------
        # Step 5 — Tier 2a: NumVerify validation
        # ------------------------------------------------------------------
        for phone in phones_found:
            nv = validate_phone_numverify(phone)
            if not nv:
                continue

            # Update the existing usphonebook contact with validation data
            contact = (
                session.query(Contact)
                .filter(
                    Contact.lead_id == lead.id,
                    Contact.phone == phone,
                    Contact.source == "usphonebook",
                )
                .first()
            )
            if contact:
                contact.__dict__["phone_valid"] = str(nv.get("valid", ""))
                contact.__dict__["phone_line_type"] = nv.get("line_type", "")
                contact.__dict__["phone_carrier"] = nv.get("carrier", "")

            # Also write a separate 'numverify' record for audit trail
            _upsert_contact(
                session,
                lead_id=lead.id,
                owner_name=prop.owner_name,
                phone=phone,
                email=None,
                source="numverify",
                mailing_address=prop.owner_mailing_address,
            )

        # ------------------------------------------------------------------
        # Step 6 — Tier 2b: Hunter.io email lookup
        # ------------------------------------------------------------------
        email_found: Optional[str] = None
        if not captcha_hit and prop.owner_name and prop.owner_mailing_address:
            email_found = lookup_email_hunter(prop.owner_name, prop.owner_mailing_address)
            if email_found:
                _upsert_contact(
                    session,
                    lead_id=lead.id,
                    owner_name=prop.owner_name,
                    phone=None,
                    email=email_found,
                    source="hunter",
                    mailing_address=prop.owner_mailing_address,
                )

        # ------------------------------------------------------------------
        # Step 7 — Tier 3: flag for paid lookup if nothing found
        # ------------------------------------------------------------------
        has_phone = len(phones_found) > 0
        has_email = email_found is not None
        already_flagged = lead.__dict__.get("enrich_status") == "needs_manual_lookup"

        if not has_phone and not has_email and not already_flagged:
            lead.__dict__["enrich_status"] = "needs_paid_lookup"
            logger.info(
                "Lead %d flagged as needs_paid_lookup (no phone or email found)",
                lead_id,
            )
        elif not already_flagged:
            lead.__dict__["enrich_status"] = "enriched"

        # ------------------------------------------------------------------
        # Step 8 — Mark lead as enriched
        # ------------------------------------------------------------------
        lead.status = "enriched"
        session.commit()
        logger.info(
            "Lead %d enrichment complete — status=%s enrich_status=%s phones=%d email=%s",
            lead_id,
            lead.status,
            lead.__dict__.get("enrich_status"),
            len(phones_found),
            email_found or "none",
        )

    except Exception as exc:
        session.rollback()
        logger.error("Enrichment failed for lead %d: %s", lead_id, exc, exc_info=True)
        raise
    finally:
        session.close()


def _upsert_contact(
    session,
    *,
    lead_id: int,
    owner_name: str,
    phone: Optional[str],
    email: Optional[str],
    source: str,
    mailing_address: Optional[str] = None,
) -> Contact:
    """
    Insert a contact record if an identical one (same lead_id + source +
    phone/email combination) does not already exist.
    """
    query = session.query(Contact).filter(
        Contact.lead_id == lead_id,
        Contact.source == source,
    )
    if phone:
        query = query.filter(Contact.phone == phone)
    if email:
        query = query.filter(Contact.email == email)

    existing = query.first()
    if existing:
        return existing

    contact = Contact(
        lead_id=lead_id,
        owner_name=owner_name,
        phone=phone,
        email=email,
        source=source,
    )
    if mailing_address:
        contact.__dict__["mailing_address"] = mailing_address

    session.add(contact)
    session.flush()
    logger.debug(
        "Inserted contact for lead %d — source=%s phone=%s email=%s",
        lead_id,
        source,
        phone,
        email,
    )
    return contact


# ---------------------------------------------------------------------------
# Batch enrichment entry point
# ---------------------------------------------------------------------------

def enrich_all(limit: Optional[int] = None, lead_id: Optional[int] = None) -> None:
    """
    Enrich leads from the database.

    Parameters
    ----------
    limit:    Maximum number of leads to process in this run.
    lead_id:  If provided, enrich only this specific lead.
    """
    _ensure_columns()

    session = SessionLocal()
    try:
        if lead_id is not None:
            leads = session.query(Lead).filter(Lead.id == lead_id).all()
        else:
            # Process leads that are 'new' or previously flagged for manual lookup
            # (so re-runs can pick up previously skipped records)
            query = session.query(Lead).filter(
                Lead.status.in_(["new", "needs_manual_lookup"])
            )
            if limit:
                query = query.limit(limit)
            leads = query.all()
    finally:
        session.close()

    if not leads:
        logger.info("No leads to enrich.")
        return

    logger.info("Enriching %d lead(s)...", len(leads))

    for i, lead in enumerate(leads):
        logger.info(
            "Processing lead %d/%d — id=%d source=%s",
            i + 1,
            len(leads),
            lead.id,
            lead.source,
        )
        enrich_lead(lead.id)

        # 5-second inter-request delay between leads (USPhoneBook rate limit)
        if i < len(leads) - 1:
            delay = 5.0 + random.uniform(0.0, 2.0)
            logger.debug("Sleeping %.1fs before next lead...", delay)
            time.sleep(delay)

    logger.info("Enrichment run complete.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run tiered contact enrichment for Suffolk Leads."
    )
    parser.add_argument(
        "--lead-id",
        type=int,
        default=None,
        metavar="ID",
        help="Enrich a single lead by its database ID.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Maximum number of leads to process (default: all pending).",
    )
    args = parser.parse_args()
    enrich_all(limit=args.limit, lead_id=args.lead_id)
