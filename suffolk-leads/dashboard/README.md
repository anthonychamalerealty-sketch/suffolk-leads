# Suffolk Leads Dashboard

A Next.js 15 web application for managing Suffolk County real estate leads.

## Features

- Leads table: address, owner name, phone, source, score (1-10), date found, status
- Source filter buttons: Fire, Probate, Obituary, Social
- Address search with debouncing
- CSV export of current filtered view
- Side panel: full contact info, property details, raw source data, Google Maps link
- Inline status management via dropdown

## Tech Stack

- Framework: Next.js 15 (App Router)
- Language: TypeScript
- Styling: Tailwind CSS + inline styles
- Database: SQLite via better-sqlite3
- Deployment: Railway (standalone Docker image)

## Local Development

```bash
cd suffolk-leads/dashboard
npm install
npm run dev
```

Open http://localhost:3000. The app looks for the SQLite DB at `../sql_app.db`
or at the path set in `DATABASE_PATH` env var.

## Environment Variables

- `DATABASE_PATH` — absolute path to the SQLite file (default: `../sql_app.db`)
- `PORT` — HTTP port (default: 3000)

## Railway Deployment

Deploy as a separate service from the same monorepo:
1. Create a new Railway service pointing to this repo
2. Set Root Directory to `suffolk-leads/dashboard`
3. Railway uses the Dockerfile automatically
4. Set DATABASE_PATH to your mounted volume path if using persistent SQLite

## API Routes

- GET  /api/leads           — list leads (query: source, search, status)
- GET  /api/leads/[id]      — single lead detail
- PATCH /api/leads/[id]     — update lead status
- GET  /api/leads/export    — download filtered CSV
