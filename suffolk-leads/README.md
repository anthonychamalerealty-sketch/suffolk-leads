# Suffolk Leads

A Python project for scraping, deduplicating, and scoring real estate leads in Suffolk County.

## Project Structure

- `/scrapers/`: Individual data scrapers for various sources.
- `/processor/`: Lead deduplication and scoring logic.
- `/dashboard/`: A React frontend for viewing and managing leads.
- `/jobs/`: Cron/scheduled tasks for automated data collection and processing.

## Database Schema

The project uses a SQLite database with the following tables:
- `properties`: `parcel_id`, `address`, `owner_name`, `owner_mailing_address`, `assessed_value`, `last_sale_date`
- `leads`: `id`, `address`, `parcel_id`, `source`, `raw_data`, `score`, `created_at`, `status`
- `contacts`: `lead_id`, `owner_name`, `phone`, `email`, `source`

## Deployment

This project is configured to be deployed on Railway.

1. Connect your GitHub repository to Railway.
2. Railway will automatically detect the `Dockerfile` and `railway.toml` for deployment.
3. Set up any necessary environment variables in the Railway dashboard based on `.env.example`.

## Local Development

1. Clone the repository.
2. Install dependencies: `pip install -r requirements.txt`
3. Run the main entrypoint: `python main.py`
