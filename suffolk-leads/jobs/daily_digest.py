"""
daily_digest.py
---------------
Queries all leads added in the last 24 hours sorted by score descending.
Generates a CSV file named suffolk-leads-YYYY-MM-DD.csv containing every
enriched lead including address, owner name, phone, email, source, score,
state, and county.
Generates an HTML email showing:
  1. A summary table of lead counts by source.
  2. The top 10 leads with address, score, source, and phone number.
Sends the email with the CSV attached using the SendGrid API
(SENDGRID_API_KEY) to the address in DIGEST_EMAIL env var.

All errors are printed explicitly — nothing is silently swallowed.
"""

# ─────────────────────────────────────────────────────────────────────────────
# Startup banner — printed before any imports that could fail.
# ─────────────────────────────────────────────────────────────────────────────
import sys
import os

print("=" * 70, flush=True)
print("STARTING SUFFOLK LEADS DAILY DIGEST", flush=True)
print("=" * 70, flush=True)
print(f"[DIAG] Python      : {sys.version}", flush=True)
print(f"[DIAG] Working dir : {os.getcwd()}", flush=True)
print(f"[DIAG] Script path : {os.path.abspath(__file__)}", flush=True)

# ─────────────────────────────────────────────────────────────────────────────
# Path setup — project root is one level above jobs/
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
print(f"[DIAG] BASE_DIR    : {BASE_DIR}", flush=True)

# ─────────────────────────────────────────────────────────────────────────────
# Logging — force stdout so Railway captures every line
# ─────────────────────────────────────────────────────────────────────────────
import logging
import datetime
import csv
import traceback
from typing import List, Dict, Any

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
logger = logging.getLogger("daily_digest")


# ─────────────────────────────────────────────────────────────────────────────
# Environment variable check — log every relevant variable up front
# ─────────────────────────────────────────────────────────────────────────────
def _check_env() -> None:
    keys = ["DATABASE_URL", "DATABASE_PATH", "SENDGRID_API_KEY", "DIGEST_EMAIL",
            "RAILWAY_ENVIRONMENT", "RAILWAY_SERVICE_NAME"]
    for k in keys:
        v = os.environ.get(k)
        if v:
            display = v if len(v) <= 20 else v[:8] + "…[redacted]"
            logger.info(f"[ENV] {k} = {display}")
        else:
            logger.warning(f"[ENV] {k} = <not set>")


# ─────────────────────────────────────────────────────────────────────────────
# Database URL resolution — find the SQLite file in Railway's container or
# honour an explicit DATABASE_URL / DATABASE_PATH env variable.
# ─────────────────────────────────────────────────────────────────────────────
def _resolve_database_url() -> str:
    """
    Return a SQLAlchemy-compatible database URL, probing several locations
    for the SQLite file when no explicit URL is configured.
    """
    # 1. Explicit DATABASE_URL already set (may be Postgres or SQLite)
    existing_url = os.environ.get("DATABASE_URL", "")
    if existing_url:
        logger.info(f"[DB] Using DATABASE_URL from environment: {existing_url[:40]}…")
        return existing_url

    # 2. Explicit DATABASE_PATH (SQLite file path)
    db_path_env = os.environ.get("DATABASE_PATH", "")
    if db_path_env:
        logger.info(f"[DB] Using DATABASE_PATH from environment: {db_path_env}")
        return f"sqlite:///{db_path_env}"

    # 3. Search common locations
    candidates = [
        os.path.join(BASE_DIR, "sql_app.db"),   # project root (most likely)
        os.path.join(os.getcwd(), "sql_app.db"), # current working dir
        "/data/sql_app.db",                      # Railway persistent volume
        "/app/sql_app.db",                       # Dockerfile WORKDIR
    ]

    for path in candidates:
        exists = os.path.isfile(path)
        size = os.path.getsize(path) if exists else 0
        logger.info(f"[DB] Probe: {path}  exists={exists}  size={size}B")
        if exists:
            url = f"sqlite:///{path}"
            logger.info(f"[DB] Resolved to: {url}")
            return url

    # 4. Fallback — create at project root (init_db will create tables)
    fallback = os.path.join(BASE_DIR, "sql_app.db")
    logger.warning(f"[DB] No existing SQLite file found. Will create: {fallback}")
    return f"sqlite:///{fallback}"


# ─────────────────────────────────────────────────────────────────────────────
# Load .env (optional — Railway injects env vars directly)
# ─────────────────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
    logger.info("[ENV] .env file loaded (if present)")
except ImportError:
    logger.warning("[ENV] python-dotenv not installed — skipping .env load")
except Exception as exc:
    logger.warning(f"[ENV] .env load failed: {exc}")

_check_env()

# ─────────────────────────────────────────────────────────────────────────────
# Database setup — deferred so we can log the URL before connecting
# ─────────────────────────────────────────────────────────────────────────────
_db_url = _resolve_database_url()
os.environ["DATABASE_URL"] = _db_url   # ensure database.py picks it up

logger.info(f"[DB] Importing SQLAlchemy models from database.py …")
try:
    from database import Lead, Contact, Property, SessionLocal, init_db
    from sqlalchemy import desc
    logger.info("[DB] Models imported successfully.")
except Exception as exc:
    logger.error(f"[DB] FATAL: Failed to import database models: {exc}")
    traceback.print_exc()
    sys.exit(1)

# Ensure tables exist (idempotent)
try:
    init_db()
    logger.info("[DB] init_db() completed — tables verified.")
except Exception as exc:
    logger.error(f"[DB] init_db() failed: {exc}")
    traceback.print_exc()
    # Non-fatal — tables may already exist


# ─────────────────────────────────────────────────────────────────────────────
# Core functions
# ─────────────────────────────────────────────────────────────────────────────

def get_leads_last_24_hours() -> List[Dict[str, Any]]:
    """
    Query all leads added in the last 24 hours sorted by score descending.
    Join with Contact and Property tables for enriched details.
    """
    logger.info("[QUERY] Opening database session …")
    session = SessionLocal()
    try:
        cutoff_time = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
        logger.info(f"[QUERY] Cutoff time (UTC): {cutoff_time.isoformat()}")

        leads = (
            session.query(Lead)
            .filter(Lead.created_at >= cutoff_time)
            .order_by(desc(Lead.score))
            .all()
        )
        logger.info(f"[QUERY] Found {len(leads)} lead(s) in the last 24 hours.")

        enriched: List[Dict[str, Any]] = []
        for lead in leads:
            try:
                prop = (
                    session.query(Property)
                    .filter(Property.parcel_id == lead.parcel_id)
                    .first()
                )
                contact = (
                    session.query(Contact)
                    .filter(Contact.lead_id == lead.id)
                    .first()
                )

                owner_name = ""
                if prop and prop.owner_name:
                    owner_name = prop.owner_name
                if contact and contact.owner_name:
                    owner_name = contact.owner_name

                enriched.append({
                    "id": lead.id,
                    "address": lead.address or (prop.address if prop else ""),
                    "owner_name": owner_name,
                    "phone": contact.phone if contact else "",
                    "email": contact.email if contact else "",
                    "source": lead.source or "",
                    "score": lead.score if lead.score is not None else 0.0,
                    "created_at": lead.created_at,
                    "status": lead.status or "new",
                    "parcel_id": lead.parcel_id or "",
                    "state": getattr(lead, "state", "NY") or "NY",
                    "county": getattr(lead, "county", "Suffolk") or "Suffolk",
                })
            except Exception as row_exc:
                logger.error(f"[QUERY] Error enriching lead id={lead.id}: {row_exc}")

        logger.info(f"[QUERY] Enriched {len(enriched)} lead(s) successfully.")
        return enriched

    except Exception as exc:
        logger.error(f"[QUERY] Database query failed: {exc}")
        traceback.print_exc()
        return []
    finally:
        try:
            session.close()
            logger.info("[QUERY] Database session closed.")
        except Exception as close_exc:
            logger.warning(f"[QUERY] Session close error: {close_exc}")


def generate_csv(leads: List[Dict[str, Any]], filename: str) -> bool:
    """
    Write enriched leads to a CSV file.  Returns True on success.
    """
    logger.info(f"[CSV] Generating {filename} with {len(leads)} row(s) …")
    headers = [
        "Address", "State", "County", "Owner Name", "Phone", "Email",
        "Source", "Score", "Created At (UTC)", "Status", "Parcel ID",
    ]
    try:
        with open(filename, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for lead in leads:
                created = lead["created_at"]
                created_str = (
                    created.strftime("%Y-%m-%d %H:%M:%S")
                    if isinstance(created, datetime.datetime)
                    else str(created or "")
                )
                writer.writerow([
                    lead["address"],
                    lead["state"],
                    lead["county"],
                    lead["owner_name"],
                    lead["phone"],
                    lead["email"],
                    lead["source"],
                    lead["score"],
                    created_str,
                    lead["status"],
                    lead["parcel_id"],
                ])
        size = os.path.getsize(filename)
        logger.info(f"[CSV] Written: {filename}  ({size} bytes)")
        return True
    except Exception as exc:
        logger.error(f"[CSV] Failed to write CSV: {exc}")
        traceback.print_exc()
        return False


def build_html_email(leads: List[Dict[str, Any]], date_str: str) -> str:
    """
    Build the HTML digest email body.
    """
    logger.info(f"[HTML] Building email for {len(leads)} lead(s) …")

    # Summary by source
    source_counts: Dict[str, int] = {}
    for lead in leads:
        src = lead["source"] or "unknown"
        source_counts[src] = source_counts.get(src, 0) + 1

    summary_rows = ""
    for src, cnt in sorted(source_counts.items(), key=lambda x: x[1], reverse=True):
        summary_rows += (
            f'<tr><td style="padding:8px;border:1px solid #ddd">{src}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;text-align:right;font-weight:bold">{cnt}</td></tr>'
        )
    summary_rows += (
        f'<tr style="background:#f2f2f2;font-weight:bold">'
        f'<td style="padding:8px;border:1px solid #ddd">Total</td>'
        f'<td style="padding:8px;border:1px solid #ddd;text-align:right">{len(leads)}</td></tr>'
    )

    # Top 10 leads
    top_rows = ""
    for idx, lead in enumerate(leads[:10], 1):
        score = lead["score"]
        color = "#e74c3c" if score >= 8 else ("#f39c12" if score >= 5 else "#2ecc71")
        phone_html = (
            lead["phone"]
            or '<span style="color:#aaa;font-style:italic">None</span>'
        )
        location = f"{lead['state']} · {lead['county']}"
        top_rows += (
            f'<tr>'
            f'<td style="padding:8px;border:1px solid #ddd;text-align:center">{idx}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;font-weight:bold">{lead["address"]}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;text-align:center">'
            f'<span style="background:{color};color:white;padding:2px 6px;border-radius:4px;font-weight:bold">{score}</span></td>'
            f'<td style="padding:8px;border:1px solid #ddd;text-align:center">{lead["source"]}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;text-align:center">{location}</td>'
            f'<td style="padding:8px;border:1px solid #ddd">{phone_html}</td>'
            f'</tr>'
        )

    if not top_rows:
        top_rows = (
            '<tr><td colspan="6" style="padding:15px;text-align:center;color:#777;font-style:italic">'
            "No leads found in the last 24 hours.</td></tr>"
        )

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Suffolk Leads Daily Digest</title></head>
<body style="font-family:Arial,sans-serif;line-height:1.6;color:#333;margin:0;padding:20px;background:#f9f9f9">
  <div style="max-width:700px;margin:0 auto;background:#fff;padding:25px;border-radius:8px;border:1px solid #e1e1e1">
    <div style="border-bottom:2px solid #3498db;padding-bottom:15px;margin-bottom:20px">
      <h1 style="color:#2c3e50;margin:0;font-size:24px">Suffolk &amp; Georgia Leads — Daily Digest</h1>
      <p style="color:#7f8c8d;margin:5px 0 0;font-size:14px">Date: {date_str}</p>
    </div>
    <p>Hello Anthony,</p>
    <p>Here is your daily real estate leads digest. In the last 24 hours, <strong>{len(leads)}</strong> new lead(s) were captured. The full CSV is attached.</p>

    <h2 style="color:#2c3e50;font-size:18px;border-bottom:1px solid #eee;padding-bottom:5px">Lead Counts by Source</h2>
    <table style="width:100%;border-collapse:collapse;margin-bottom:25px">
      <thead><tr style="background:#3498db;color:white">
        <th style="padding:10px;border:1px solid #ddd;text-align:left">Source</th>
        <th style="padding:10px;border:1px solid #ddd;text-align:right;width:120px">Count</th>
      </tr></thead>
      <tbody>{summary_rows}</tbody>
    </table>

    <h2 style="color:#2c3e50;font-size:18px;border-bottom:1px solid #eee;padding-bottom:5px">Top 10 Leads (Highest Score)</h2>
    <table style="width:100%;border-collapse:collapse;margin-bottom:25px;font-size:14px">
      <thead><tr style="background:#34495e;color:white">
        <th style="padding:10px;border:1px solid #ddd;width:40px">#</th>
        <th style="padding:10px;border:1px solid #ddd;text-align:left">Address</th>
        <th style="padding:10px;border:1px solid #ddd;width:60px">Score</th>
        <th style="padding:10px;border:1px solid #ddd;width:90px">Source</th>
        <th style="padding:10px;border:1px solid #ddd;width:110px">Location</th>
        <th style="padding:10px;border:1px solid #ddd;text-align:left;width:130px">Phone</th>
      </tr></thead>
      <tbody>{top_rows}</tbody>
    </table>

    <p style="font-size:13px;color:#7f8c8d;margin-top:30px;border-top:1px solid #eee;padding-top:15px">
      Automatically generated by the Suffolk Leads pipeline on Railway.
    </p>
  </div>
</body>
</html>"""
    logger.info("[HTML] Email HTML built successfully.")
    return html


def send_email_with_sendgrid(
    to_email: str,
    subject: str,
    html_content: str,
    attachment_path: str = None,
    attachment_filename: str = None,
) -> bool:
    """
    Send an email via the SendGrid API with an optional CSV attachment.
    Returns True on success, False on any error.
    """
    api_key = os.environ.get("SENDGRID_API_KEY", "")
    if not api_key:
        logger.error("[EMAIL] SENDGRID_API_KEY is not set — cannot send email.")
        return False

    logger.info(f"[EMAIL] Preparing email to: {to_email}")
    logger.info(f"[EMAIL] Subject: {subject}")

    try:
        import base64
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import (
            Mail, Attachment, FileContent, FileName, FileType, Disposition,
        )
        logger.info("[EMAIL] sendgrid package imported successfully.")
    except ImportError as exc:
        logger.error(f"[EMAIL] sendgrid package not installed: {exc}")
        return False
    except Exception as exc:
        logger.error(f"[EMAIL] Failed to import sendgrid: {exc}")
        traceback.print_exc()
        return False

    try:
        message = Mail(
            from_email="Anthonychamalerealty@gmail.com",
            to_emails=to_email,
            subject=subject,
            html_content=html_content,
        )
        logger.info("[EMAIL] Mail object created.")
    except Exception as exc:
        logger.error(f"[EMAIL] Failed to create Mail object: {exc}")
        traceback.print_exc()
        return False

    # Attach CSV if provided
    if attachment_path and attachment_filename:
        if not os.path.isfile(attachment_path):
            logger.warning(f"[EMAIL] Attachment file not found: {attachment_path} — sending without attachment.")
        else:
            try:
                with open(attachment_path, "rb") as f:
                    encoded = base64.b64encode(f.read()).decode()
                message.attachment = Attachment(
                    FileContent(encoded),
                    FileName(attachment_filename),
                    FileType("text/csv"),
                    Disposition("attachment"),
                )
                logger.info(f"[EMAIL] Attached: {attachment_filename}")
            except Exception as exc:
                logger.error(f"[EMAIL] Failed to attach file: {exc}")
                traceback.print_exc()
                # Continue — send without attachment rather than failing entirely

    # Send
    try:
        logger.info("[EMAIL] Calling SendGrid API …")
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        logger.info(f"[EMAIL] SendGrid response status: {response.status_code}")
        if response.status_code in (200, 202):
            logger.info("[EMAIL] Email sent successfully.")
            return True
        else:
            logger.error(
                f"[EMAIL] Unexpected status {response.status_code}: {response.body}"
            )
            return False
    except Exception as exc:
        logger.error(f"[EMAIL] SendGrid API error: {exc}")
        traceback.print_exc()
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    logger.info("daily_digest.py — main() starting")

    digest_email = os.environ.get("DIGEST_EMAIL", "Anthonychamalerealty@gmail.com")
    today = datetime.datetime.now()
    date_str = today.strftime("%Y-%m-%d")

    # ── 1. Fetch leads ────────────────────────────────────────────────────────
    logger.info("[STEP 1] Fetching leads from the last 24 hours …")
    try:
        leads = get_leads_last_24_hours()
    except Exception as exc:
        logger.error(f"[STEP 1] get_leads_last_24_hours() raised: {exc}")
        traceback.print_exc()
        leads = []

    logger.info(f"[STEP 1] Lead count: {len(leads)}")

    # ── 2. No-leads path — send a health-check email ──────────────────────────
    if not leads:
        logger.info("[STEP 2] No leads found — sending system health-check email.")
        subject = f"Suffolk Leads — System Check ({date_str}): 0 new leads"
        html_body = f"""<!DOCTYPE html>
<html><body style="font-family:Arial,sans-serif;padding:20px">
  <h2>Suffolk Leads — System Health Check</h2>
  <p>Date: <strong>{date_str}</strong></p>
  <p>The cron job ran successfully but found <strong>0 new leads</strong> in the last 24 hours.</p>
  <p>This confirms the Railway cron service is executing correctly.</p>
  <hr>
  <p style="font-size:12px;color:#888">Database: {_db_url[:60]}</p>
</body></html>"""
        ok = send_email_with_sendgrid(
            to_email=digest_email,
            subject=subject,
            html_content=html_body,
        )
        if ok:
            logger.info("[STEP 2] Health-check email sent successfully.")
        else:
            logger.error("[STEP 2] Health-check email failed.")
            sys.exit(1)
        return

    # ── 3. Generate CSV ───────────────────────────────────────────────────────
    logger.info("[STEP 3] Generating CSV …")
    csv_filename = f"suffolk-leads-{date_str}.csv"
    csv_path = os.path.join(os.getcwd(), csv_filename)
    csv_ok = generate_csv(leads, csv_path)
    if not csv_ok:
        logger.warning("[STEP 3] CSV generation failed — will send email without attachment.")
        csv_path = None
        csv_filename = None

    # ── 4. Build HTML ─────────────────────────────────────────────────────────
    logger.info("[STEP 4] Building HTML email …")
    try:
        html_email = build_html_email(leads, date_str)
    except Exception as exc:
        logger.error(f"[STEP 4] build_html_email() failed: {exc}")
        traceback.print_exc()
        html_email = f"<p>Daily digest for {date_str}: {len(leads)} lead(s) found. See attached CSV.</p>"

    # ── 5. Send email ─────────────────────────────────────────────────────────
    subject = f"Suffolk Leads Daily Digest — {date_str} ({len(leads)} lead(s))"
    logger.info(f"[STEP 5] Sending digest email to {digest_email} …")
    try:
        ok = send_email_with_sendgrid(
            to_email=digest_email,
            subject=subject,
            html_content=html_email,
            attachment_path=csv_path,
            attachment_filename=csv_filename,
        )
    except Exception as exc:
        logger.error(f"[STEP 5] send_email_with_sendgrid() raised: {exc}")
        traceback.print_exc()
        ok = False

    # ── 6. Cleanup ────────────────────────────────────────────────────────────
    if csv_path and os.path.isfile(csv_path):
        try:
            os.remove(csv_path)
            logger.info(f"[STEP 6] Temporary CSV removed: {csv_path}")
        except Exception as exc:
            logger.warning(f"[STEP 6] Could not remove CSV: {exc}")

    if ok:
        logger.info("daily_digest.py — completed successfully.")
    else:
        logger.error("daily_digest.py — email delivery failed.")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FATAL: Unhandled exception in daily_digest.py: {exc}", flush=True)
        traceback.print_exc()
        sys.exit(2)
