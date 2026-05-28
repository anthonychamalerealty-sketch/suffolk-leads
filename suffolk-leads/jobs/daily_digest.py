"""
daily_digest.py
---------------
Queries all leads added in the last 24 hours sorted by score descending.
Generates a CSV file named suffolk-leads-YYYY-MM-DD.csv containing every enriched lead
including address, owner name, phone, email, source, and score.
Generates an HTML email showing:
  1. A summary table of lead counts by source.
  2. The top 10 leads with address, score, source, and phone number.
Sends the email with the CSV attached using the SendGrid API (SENDGRID_API_KEY)
to Anthonychamalerealty@gmail.com (DIGEST_EMAIL).
"""

from __future__ import annotations
import os
import sys
import datetime
import csv
import logging
from typing import List, Dict, Any

# Path setup — allow running as a script from any working directory
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
# Load env variables from .env file
load_dotenv()

from database import Lead, Contact, Property, SessionLocal
from sqlalchemy import desc

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("daily_digest")

def get_leads_last_24_hours() -> List[Dict[str, Any]]:
    """
    Query all leads added in the last 24 hours sorted by score descending.
    Join with Contact and Property tables to retrieve owner name, phone, email, and address.
    """
    session = SessionLocal()
    try:
        # Calculate the datetime 24 hours ago
        cutoff_time = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
        logger.info(f"Querying leads added since {cutoff_time} UTC...")

        # Query leads added in the last 24 hours, sorted by score descending
        leads = session.query(Lead).filter(
            Lead.created_at >= cutoff_time
        ).order_by(desc(Lead.score)).all()

        logger.info(f"Found {len(leads)} leads in the last 24 hours.")

        enriched_leads = []
        for lead in leads:
            # Get property details
            prop = session.query(Property).filter(Property.parcel_id == lead.parcel_id).first()
            owner_name = prop.owner_name if prop else ""
            
            # Get contact details (first one if multiple exist)
            contact = session.query(Contact).filter(Contact.lead_id == lead.id).first()
            phone = contact.phone if contact else ""
            email = contact.email if contact else ""
            if contact and contact.owner_name:
                owner_name = contact.owner_name

            enriched_leads.append({
                "id": lead.id,
                "address": lead.address or (prop.address if prop else ""),
                "owner_name": owner_name,
                "phone": phone,
                "email": email,
                "source": lead.source,
                "score": lead.score if lead.score is not None else 0.0,
                "created_at": lead.created_at,
                "status": lead.status,
                "parcel_id": lead.parcel_id
            })
        
        return enriched_leads
    except Exception as exc:
        logger.error(f"Error querying leads: {exc}", exc_info=True)
        return []
    finally:
        session.close()

def generate_csv(leads: List[Dict[str, Any]], filename: str) -> None:
    """
    Generate a CSV file with every lead's enriched details.
    """
    logger.info(f"Generating CSV file: {filename} with {len(leads)} rows...")
    headers = ["Address", "Owner Name", "Phone", "Email", "Source", "Score", "Created At (UTC)", "Status", "Parcel ID"]
    
    with open(filename, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for lead in leads:
            writer.writerow([
                lead["address"],
                lead["owner_name"],
                lead["phone"],
                lead["email"],
                lead["source"],
                lead["score"],
                lead["created_at"].strftime("%Y-%m-%d %H:%M:%S") if lead["created_at"] else "",
                lead["status"],
                lead["parcel_id"]
            ])
    logger.info("CSV generation complete.")

def build_html_email(leads: List[Dict[str, Any]], date_str: str) -> str:
    """
    Generate the HTML content for the digest email.
    Includes a summary table of lead counts by source and the top 10 leads.
    """
    # 1. Summary table of lead counts by source
    source_counts: Dict[str, int] = {}
    for lead in leads:
        source = lead["source"] or "unknown"
        source_counts[source] = source_counts.get(source, 0) + 1

    summary_rows = ""
    total_leads = len(leads)
    for source, count in sorted(source_counts.items(), key=lambda x: x[1], reverse=True):
        summary_rows += f"""
        <tr>
            <td style="padding: 8px; border: 1px solid #ddd; text-align: left;">{source}</td>
            <td style="padding: 8px; border: 1px solid #ddd; text-align: right; font-weight: bold;">{count}</td>
        </tr>
        """
    # Add total row
    summary_rows += f"""
    <tr style="background-color: #f2f2f2; font-weight: bold;">
        <td style="padding: 8px; border: 1px solid #ddd; text-align: left;">Total</td>
        <td style="padding: 8px; border: 1px solid #ddd; text-align: right;">{total_leads}</td>
    </tr>
    """

    # 2. Top 10 leads table
    top_10 = leads[:10]
    top_rows = ""
    for idx, lead in enumerate(top_10, 1):
        # Format score with background color indicating priority
        score = lead["score"]
        score_color = "#e74c3c" if score >= 8 else ("#f39c12" if score >= 5 else "#2ecc71")
        
        top_rows += f"""
        <tr>
            <td style="padding: 8px; border: 1px solid #ddd; text-align: center;">{idx}</td>
            <td style="padding: 8px; border: 1px solid #ddd; text-align: left; font-weight: bold;">{lead["address"]}</td>
            <td style="padding: 8px; border: 1px solid #ddd; text-align: center;"><span style="background-color: {score_color}; color: white; padding: 2px 6px; border-radius: 4px; font-weight: bold;">{score}</span></td>
            <td style="padding: 8px; border: 1px solid #ddd; text-align: center;">{lead["source"]}</td>
            <td style="padding: 8px; border: 1px solid #ddd; text-align: left;">{lead["phone"] or '<span style="color: #aaa; font-style: italic;">None</span>'}</td>
        </tr>
        """

    if not top_rows:
        top_rows = """
        <tr>
            <td colspan="5" style="padding: 15px; text-align: center; color: #777; font-style: italic;">No leads found in the last 24 hours.</td>
        </tr>
        """

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>Suffolk Leads Daily Digest</title>
    </head>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 20px; background-color: #f9f9f9;">
        <div style="max-width: 650px; margin: 0 auto; background-color: #ffffff; padding: 25px; border-radius: 8px; box-shadow: 0 4px 8px rgba(0,0,0,0.05); border: 1px solid #e1e1e1;">
            
            <!-- Header -->
            <div style="border-bottom: 2px solid #3498db; padding-bottom: 15px; margin-bottom: 20px;">
                <h1 style="color: #2c3e50; margin: 0; font-size: 24px;">Suffolk Leads Daily Digest</h1>
                <p style="color: #7f8c8d; margin: 5px 0 0 0; font-size: 14px;">Date: {date_str}</p>
            </div>

            <!-- Intro -->
            <p>Hello Anthony,</p>
            <p>Here is your daily real estate leads digest for Suffolk County. In the last 24 hours, we captured and processed <strong>{total_leads}</strong> new leads. A complete CSV of all enriched leads is attached to this email.</p>

            <!-- Summary Table -->
            <h2 style="color: #2c3e50; font-size: 18px; margin-top: 25px; margin-bottom: 10px; border-bottom: 1px solid #eee; padding-bottom: 5px;">Lead Counts by Source</h2>
            <table style="width: 100%; border-collapse: collapse; margin-bottom: 25px;">
                <thead>
                    <tr style="background-color: #3498db; color: white;">
                        <th style="padding: 10px; border: 1px solid #ddd; text-align: left;">Source</th>
                        <th style="padding: 10px; border: 1px solid #ddd; text-align: right; width: 120px;">Lead Count</th>
                    </tr>
                </thead>
                <tbody>
                    {summary_rows}
                </tbody>
            </table>

            <!-- Top 10 Leads Table -->
            <h2 style="color: #2c3e50; font-size: 18px; margin-top: 25px; margin-bottom: 10px; border-bottom: 1px solid #eee; padding-bottom: 5px;">Top 10 Leads (Highest Score)</h2>
            <table style="width: 100%; border-collapse: collapse; margin-bottom: 25px; font-size: 14px;">
                <thead>
                    <tr style="background-color: #34495e; color: white;">
                        <th style="padding: 10px; border: 1px solid #ddd; text-align: center; width: 40px;">#</th>
                        <th style="padding: 10px; border: 1px solid #ddd; text-align: left;">Address</th>
                        <th style="padding: 10px; border: 1px solid #ddd; text-align: center; width: 60px;">Score</th>
                        <th style="padding: 10px; border: 1px solid #ddd; text-align: center; width: 100px;">Source</th>
                        <th style="padding: 10px; border: 1px solid #ddd; text-align: left; width: 130px;">Phone</th>
                    </tr>
                </thead>
                <tbody>
                    {top_rows}
                </tbody>
            </table>

            <p style="font-size: 13px; color: #7f8c8d; margin-top: 30px; border-top: 1px solid #eee; padding-top: 15px;">
                This digest was automatically generated and sent via the Suffolk Leads pipeline deployed on Railway.
            </p>
        </div>
    </body>
    </html>
    """
    return html

def send_email_with_sendgrid(
    to_email: str,
    subject: str,
    html_content: str,
    attachment_path: str = None,
    attachment_filename: str = None
) -> bool:
    """
    Send email with attachment using SendGrid API.
    """
    api_key = os.getenv("SENDGRID_API_KEY")
    if not api_key:
        logger.error("SENDGRID_API_KEY is not set in the environment variables.")
        return False

    import base64
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import (
        Mail, Attachment, FileContent, FileName, FileType, Disposition
    )

    message = Mail(
        from_email="Anthonychamalerealty@gmail.com",  # Using verified sender or matching target
        to_emails=to_email,
        subject=subject,
        html_content=html_content
    )

    # Read and encode attachment
    if attachment_path and attachment_filename:
        try:
            with open(attachment_path, "rb") as f:
                data = f.read()
                encoded_file = base64.b64encode(data).decode()
            
            attached_file = Attachment(
                FileContent(encoded_file),
                FileName(attachment_filename),
                FileType("text/csv"),
                Disposition("attachment")
            )
            message.attachment = attached_file
            logger.info(f"Attached {attachment_filename} to the email.")
        except Exception as exc:
            logger.error(f"Failed to attach file: {exc}", exc_info=True)
            return False

    try:
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        logger.info(f"Email sent successfully. Status code: {response.status_code}")
        return True
    except Exception as exc:
        logger.error(f"SendGrid API Error: {exc}", exc_info=True)
        return False

def main() -> None:
    """
    Main pipeline execution.
    """
    # 1. Fetch leads from the last 24 hours
    leads = get_leads_last_24_hours()
    
    # Format dates
    today = datetime.datetime.now()
    date_str = today.strftime("%Y-%m-%d")
    
    digest_email = os.getenv("DIGEST_EMAIL", "Anthonychamalerealty@gmail.com")

    # If no leads are found, send a test email as requested
    if not leads:
        subject = "Suffolk Leads - System Test"
        html_email = "System is running correctly. No new leads found in the last 24 hours."
        logger.info(f"No leads found. Sending system test email to {digest_email}...")
        success = send_email_with_sendgrid(
            to_email=digest_email,
            subject=subject,
            html_content=html_email
        )
        if success:
            logger.info("System test email sent successfully. Daily digest process completed.")
        else:
            logger.error("System test email failed during delivery.")
            sys.exit(1)
        return
    
    csv_filename = f"suffolk-leads-{date_str}.csv"
    csv_path = os.path.join(os.getcwd(), csv_filename)
    
    # 2. Generate CSV
    generate_csv(leads, csv_path)
    
    # 3. Generate HTML email content
    html_email = build_html_email(leads, date_str)
    
    # 4. Send email
    subject = f"Suffolk Leads Daily Digest - {date_str} ({len(leads)} leads)"
    
    logger.info(f"Sending digest to {digest_email}...")
    success = send_email_with_sendgrid(
        to_email=digest_email,
        subject=subject,
        html_content=html_email,
        attachment_path=csv_path,
        attachment_filename=csv_filename
    )
    
    if success:
        logger.info("Daily digest process completed successfully.")
        # Clean up local CSV file after successful sending
        try:
            os.remove(csv_path)
            logger.info("Temporary local CSV file cleaned up.")
        except Exception as exc:
            logger.warning(f"Failed to remove temporary CSV file: {exc}")
    else:
        logger.error("Daily digest process failed during email delivery.")
        sys.exit(1)

if __name__ == "__main__":
    main()
