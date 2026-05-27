"""
tests/test_social_signals.py
----------------------------
Unit tests for the SocialSignalsScraper.
"""

import os
import sys
import json
import sqlite3
import tempfile
import pytest
from unittest.mock import patch, MagicMock

# Ensure project root is on sys.path
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from database import SessionLocal, Property, Lead
from scrapers.social_signals import SocialSignalsScraper

class TestSocialSignalsScraper:
    """Tests for the SocialSignalsScraper class."""

    @pytest.fixture
    def temp_db(self):
        """Creates a temporary SQLite database and yields its path."""
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        
        # Initialize schema
        from database import Base, create_engine
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(bind=engine)
        
        yield db_path
        os.unlink(db_path)

    def test_address_extraction(self):
        """Verifies that the address extraction regex matches common Long Island formats."""
        scraper = SocialSignalsScraper()
        
        # Simple match
        addr = scraper._extract_address("Must sell: 123 Main St, Brookhaven, NY 11719!")
        assert addr == "123 Main St, Brookhaven 11719"

        # Match with town name and suffix
        addr = scraper._extract_address("Moving out, selling everything at 456 Elm Road, Huntington")
        assert addr == "456 Elm Road, Huntington"

        # Match with only street suffix
        addr = scraper._extract_address("Fixer upper located at 789 Oak Ave.")
        assert addr == "789 Oak Ave"

        # No match on invalid formats
        addr = scraper._extract_address("No address here, just a phone 516-555-0199")
        assert addr is None

    def test_address_matches_property(self):
        """Verifies that flexible address matching works as expected."""
        scraper = SocialSignalsScraper()
        
        assert scraper._address_matches_property("123 Main St", "123 MAIN ST, BROOKHAVEN, NY 11719")
        assert scraper._address_matches_property("123 MAIN ST, BROOKHAVEN, NY 11719", "123 Main St")
        assert not scraper._address_matches_property("123 Main St", "456 Main St")

    @patch("scrapers.social_signals.SessionLocal")
    def test_save_leads(self, mock_session_local, temp_db):
        """Verifies that leads are correctly cross-referenced and saved to the database."""
        # Create real session on temp DB
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        
        engine = create_engine(f"sqlite:///{temp_db}")
        Session = sessionmaker(bind=engine)
        session = Session()
        mock_session_local.return_value = session

        # Add a mock property
        prop = Property(
            parcel_id="0200-123",
            address="123 MAIN ST, BROOKHAVEN, NY 11719",
            owner_name="JOHN DOE",
            owner_mailing_address="123 MAIN ST, BROOKHAVEN, NY 11719",
            assessed_value=500000.0,
            last_sale_date="2023-06-15",
            property_class_code="210"
        )
        session.add(prop)
        session.commit()

        scraper = SocialSignalsScraper()
        
        # Test posts
        mock_posts = [
            {
                "source": "craigslist",
                "title": "Must sell 123 Main St, Brookhaven, NY 11719",
                "text": "Must sell 123 Main St, Brookhaven, NY 11719. Estate sale!",
                "url": "https://longisland.craigslist.org/hhh/d/123-main-st/12345.html",
                "date": "2026-05-27"
            },
            {
                "source": "reddit",
                "title": "Flipping a fixer upper",
                "text": "We bought a fixer upper at 456 Elm St, Huntington, NY 11743",
                "url": "https://reddit.com/r/longisland/comments/abcde/",
                "date": "2026-05-27"
            }
        ]

        saved_count = scraper._save_leads(mock_posts)
        
        # Only the 123 Main St post should match the properties table
        assert saved_count == 1

        # Check lead in database
        lead = session.query(Lead).filter_by(parcel_id="0200-123").first()
        assert lead is not None
        assert lead.source == "social_signal"
        assert lead.score == 0.80
        assert lead.address == "123 MAIN ST, BROOKHAVEN, NY 11719"
        
        raw_data = json.loads(lead.raw_data)
        assert raw_data["post_url"] == "https://longisland.craigslist.org/hhh/d/123-main-st/12345.html"
        assert raw_data["owner_name"] == "JOHN DOE"

        # Run again with same posts - should deduplicate and save 0 new leads
        saved_count_dup = scraper._save_leads(mock_posts)
        assert saved_count_dup == 0

        session.close()
