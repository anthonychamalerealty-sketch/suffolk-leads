"""
tests/test_parcel_access.py
----------------------------
Tests for the ParcelAccess scraper.

The test suite is split into two categories:

1. **Unit tests** – fast, offline, no browser required.  They mock the
   Playwright browser and the database session to verify the scraper's
   internal logic (parsing, upsert, logging) without hitting the network.

2. **Integration test** – requires internet access and a real Chromium
   browser.  It verifies that the scraper can navigate the live Suffolk
   County portal and extract at least one valid parcel record.

Run all tests::

    pytest tests/test_parcel_access.py -v

Run only unit tests (skip the live integration test)::

    pytest tests/test_parcel_access.py -v -m "not integration"

Run only the integration test::

    pytest tests/test_parcel_access.py -v -m integration
"""

import asyncio
import os
import sys
import sqlite3
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scrapers.parcel_access import (
    ParcelAccessScraper,
    TOWN_PREFIXES,
    _parse_currency,
    _fetch_last_sale_date,
)


# ===========================================================================
# Unit tests
# ===========================================================================


class TestParseCurrency:
    """Tests for the ``_parse_currency`` helper."""

    def test_dollar_sign_and_comma(self):
        assert _parse_currency("$1,234.56") == 1234.56

    def test_plain_float(self):
        assert _parse_currency("400.00") == 400.0

    def test_empty_string(self):
        assert _parse_currency("") == 0.0

    def test_none_like_value(self):
        assert _parse_currency(None) == 0.0  # type: ignore[arg-type]

    def test_zero_dollars(self):
        assert _parse_currency("$0.00") == 0.0

    def test_large_value(self):
        assert _parse_currency("$1,000,000.00") == 1_000_000.0


class TestTownPrefixes:
    """Verifies that all 10 required towns are present in TOWN_PREFIXES."""

    REQUIRED_TOWNS = {
        "Brookhaven",
        "Huntington",
        "Babylon",
        "Islip",
        "Smithtown",
        "Riverhead",
        "Southampton",
        "East Hampton",
        "Shelter Island",
        "Southold",
    }

    def test_all_towns_present(self):
        missing = self.REQUIRED_TOWNS - set(TOWN_PREFIXES.keys())
        assert not missing, f"Missing towns: {missing}"

    def test_prefix_format(self):
        """Each prefix must be a 4-digit numeric string."""
        for town, prefix in TOWN_PREFIXES.items():
            assert prefix.isdigit() and len(prefix) == 4, (
                f"Town '{town}' has invalid prefix '{prefix}'"
            )


class TestUpsert:
    """Tests for the ``_upsert`` method using a temporary SQLite database."""

    def _make_scraper_and_session(self, db_url: str):
        """
        Creates a scraper and a patched SessionLocal that both use the same
        temporary SQLite database.  Returns (scraper, SessionLocal).
        """
        import database as db_module
        import scrapers.parcel_access as pa_module

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine(
            db_url, connect_args={"check_same_thread": False}
        )
        db_module.Base.metadata.create_all(bind=engine)
        TestSessionLocal = sessionmaker(
            autocommit=False, autoflush=False, bind=engine
        )
        # Patch the SessionLocal reference inside the scrapers.parcel_access module
        pa_module.SessionLocal = TestSessionLocal
        scraper = ParcelAccessScraper.__new__(ParcelAccessScraper)
        return scraper, TestSessionLocal

    def _sample_record(self, parcel_id: str = "TEST-001") -> dict:
        return {
            "parcel_id":             parcel_id,
            "address":               "123 TEST ST",
            "owner_name":            "JOHN DOE",
            "owner_mailing_address": "123 TEST ST, TESTVILLE, NY 11111",
            "assessed_value":        500_000.0,
            "last_sale_date":        "2023-06-15",
            "property_class_code":   "210",
        }

    def test_insert_new_record(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            scraper, _ = self._make_scraper_and_session(f"sqlite:///{db_path}")
            record = self._sample_record()
            scraper._upsert(record)

            # Verify via direct SQLite
            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                "SELECT parcel_id, owner_name FROM properties WHERE parcel_id=?",
                (record["parcel_id"],),
            ).fetchall()
            conn.close()
            assert len(rows) == 1
            assert rows[0][1] == "JOHN DOE"
        finally:
            os.unlink(db_path)

    def test_upsert_updates_existing_record(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            scraper, _ = self._make_scraper_and_session(f"sqlite:///{db_path}")
            record = self._sample_record()
            scraper._upsert(record)

            # Update the owner name
            updated = dict(record, owner_name="JANE DOE")
            scraper._upsert(updated)

            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                "SELECT owner_name FROM properties WHERE parcel_id=?",
                (record["parcel_id"],),
            ).fetchall()
            conn.close()
            assert len(rows) == 1
            assert rows[0][0] == "JANE DOE"
        finally:
            os.unlink(db_path)

    def test_upsert_stores_all_fields(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            scraper, _ = self._make_scraper_and_session(f"sqlite:///{db_path}")
            record = self._sample_record("FULL-FIELDS")
            scraper._upsert(record)

            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT parcel_id, address, owner_name, owner_mailing_address, "
                "assessed_value, last_sale_date, property_class_code "
                "FROM properties WHERE parcel_id=?",
                (record["parcel_id"],),
            ).fetchone()
            conn.close()
            assert row is not None
            assert row[0] == "FULL-FIELDS"
            assert row[2] == "JOHN DOE"
            assert row[4] == 500_000.0
            assert row[5] == "2023-06-15"
            assert row[6] == "210"
        finally:
            os.unlink(db_path)


class TestFetchLastSaleDate:
    """Tests for the ``_fetch_last_sale_date`` async helper."""

    def test_returns_na_on_network_error(self):
        """Should return 'N/A' gracefully when the ArcGIS API is unreachable."""
        import urllib.error

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")):
            result = asyncio.run(_fetch_last_sale_date("0200"))
        assert result == "N/A"

    def test_returns_na_when_no_features(self):
        """Should return 'N/A' when the API returns an empty feature set."""
        import json
        from io import BytesIO
        from unittest.mock import MagicMock

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"features": []}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = asyncio.run(_fetch_last_sale_date("0200"))
        assert result == "N/A"

    def test_parses_epoch_ms_correctly(self):
        """Should convert an epoch-ms timestamp to a YYYY-MM-DD string."""
        import json
        from unittest.mock import MagicMock

        # 2023-06-15 00:00:00 UTC in epoch ms
        epoch_ms = 1686787200000
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {"features": [{"attributes": {"LAST_SALE_DATE": epoch_ms}}]}
        ).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = asyncio.run(_fetch_last_sale_date("0200"))
        assert result == "2023-06-15"


# ===========================================================================
# Integration test  (requires internet + Chromium)
# ===========================================================================


@pytest.mark.integration
def test_scrape_at_least_one_record():
    """
    Integration test: verifies that the scraper can navigate the live Suffolk
    County ParcelAccess portal and extract at least one valid parcel record.

    This test is marked ``integration`` and is skipped in offline environments.
    Run it with::

        pytest tests/test_parcel_access.py -v -m integration
    """
    # Restrict to a single town and a single parcel to keep the test fast
    original_towns = dict(TOWN_PREFIXES)
    TOWN_PREFIXES.clear()
    TOWN_PREFIXES["Brookhaven"] = "0200"

    try:
        scraper = ParcelAccessScraper(headless=True)
        results = scraper.scrape(limit_per_town=1)
    finally:
        TOWN_PREFIXES.clear()
        TOWN_PREFIXES.update(original_towns)

    # --- Assertions ---
    assert len(results) >= 1, "Expected at least one scraped record."

    record = results[0]

    # All required fields must be present
    required_fields = {
        "parcel_id",
        "address",
        "owner_name",
        "owner_mailing_address",
        "assessed_value",
        "last_sale_date",
        "property_class_code",
    }
    missing = required_fields - set(record.keys())
    assert not missing, f"Missing fields in scraped record: {missing}"

    # parcel_id must be a non-empty string
    assert isinstance(record["parcel_id"], str) and record["parcel_id"], (
        "parcel_id must be a non-empty string."
    )

    # assessed_value must be a non-negative number
    assert isinstance(record["assessed_value"], (int, float)) and record["assessed_value"] >= 0, (
        "assessed_value must be a non-negative number."
    )

    # Verify the record was persisted to the DB
    conn = sqlite3.connect(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sql_app.db")
    )
    rows = conn.execute(
        "SELECT parcel_id FROM properties WHERE parcel_id=?",
        (record["parcel_id"],),
    ).fetchall()
    conn.close()
    assert len(rows) >= 1, (
        f"Parcel {record['parcel_id']} was not found in the database after scraping."
    )
