"""
database.py
-----------
SQLAlchemy models and database initialisation for Suffolk Leads.

The entire SQLAlchemy setup is wrapped in a try/except so that importing
this module never crashes the process — even when DATABASE_URL is wrong,
the DB file is missing, or SQLAlchemy is not installed.

Callers should check that `engine` is not None before using it.
"""
import os
import datetime
import logging
import sys

logger = logging.getLogger(__name__)

# ── Database URL ──────────────────────────────────────────────────────────────
def _get_database_url() -> str:
    """Return the database URL from env, with sensible fallbacks."""
    url = os.getenv("DATABASE_URL", "")
    if url:
        return url
    db_path = os.getenv("DATABASE_PATH", "")
    if db_path:
        return f"sqlite:///{db_path}"
    # Default: SQLite file next to this module (works locally and on Railway)
    module_dir = os.path.dirname(os.path.abspath(__file__))
    default_path = os.path.join(module_dir, "sql_app.db")
    return f"sqlite:///{default_path}"


# ── SQLAlchemy setup — fully guarded ─────────────────────────────────────────
engine = None
SessionLocal = None
Base = None
Property = None
Lead = None
Contact = None

try:
    from sqlalchemy import (
        create_engine, Column, Integer, String, Float, DateTime, Text, ForeignKey,
    )
    from sqlalchemy.ext.declarative import declarative_base
    from sqlalchemy.orm import sessionmaker

    _DATABASE_URL = _get_database_url()
    logger.info(f"[database.py] Using database URL: {_DATABASE_URL[:60]}")

    _connect_args = {"check_same_thread": False} if "sqlite" in _DATABASE_URL else {}
    engine = create_engine(_DATABASE_URL, connect_args=_connect_args)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base = declarative_base()

    class Property(Base):
        __tablename__ = "properties"
        parcel_id = Column(String, primary_key=True, index=True)
        address = Column(String)
        owner_name = Column(String)
        owner_mailing_address = Column(String)
        assessed_value = Column(Float)
        last_sale_date = Column(String)
        property_class_code = Column(String)
        state = Column(String, default="NY")
        county = Column(String, default="Suffolk")

    class Lead(Base):
        __tablename__ = "leads"
        id = Column(Integer, primary_key=True, index=True)
        address = Column(String)
        parcel_id = Column(String, ForeignKey("properties.parcel_id"))
        source = Column(String)
        raw_data = Column(Text)
        score = Column(Float)
        created_at = Column(DateTime, default=datetime.datetime.utcnow)
        status = Column(String, default="new")
        state = Column(String, default="NY")
        county = Column(String, default="Suffolk")

    class Contact(Base):
        __tablename__ = "contacts"
        id = Column(Integer, primary_key=True, index=True)
        lead_id = Column(Integer, ForeignKey("leads.id"))
        owner_name = Column(String)
        phone = Column(String)
        email = Column(String)
        source = Column(String)

    def init_db():
        """Create all tables. Safe to call multiple times (idempotent)."""
        try:
            Base.metadata.create_all(bind=engine)
            logger.info("[database.py] Tables created / verified.")
        except Exception as exc:
            logger.error(f"[database.py] init_db() failed: {exc}")
            import traceback
            traceback.print_exc()
            raise

    logger.info("[database.py] SQLAlchemy models loaded successfully.")

except ImportError as _imp_exc:
    logger.error(
        f"[database.py] SQLAlchemy not installed — database unavailable: {_imp_exc}\n"
        "Run: pip install sqlalchemy"
    )

    def init_db():
        logger.error("[database.py] init_db() skipped — SQLAlchemy not installed.")

except Exception as _setup_exc:
    logger.error(
        f"[database.py] Database setup failed: {_setup_exc}\n"
        "Check DATABASE_URL / DATABASE_PATH environment variables."
    )
    import traceback
    traceback.print_exc()

    def init_db():
        logger.error(f"[database.py] init_db() skipped — setup failed: {_setup_exc}")


if __name__ == "__main__":
    init_db()
    print("Database initialised.", flush=True)
