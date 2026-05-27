import Database from "better-sqlite3";
import path from "path";
import fs from "fs";

// Resolve the SQLite database path. In production the DB lives one level up
// from the dashboard directory (i.e. ../sql_app.db). We also honour the
// DATABASE_PATH env variable so Railway can point to a mounted volume.
function getDbPath(): string {
  if (process.env.DATABASE_PATH) {
    return process.env.DATABASE_PATH;
  }
  // Walk up from dashboard/ to find sql_app.db
  const candidates = [
    path.resolve(process.cwd(), "../sql_app.db"),
    path.resolve(process.cwd(), "sql_app.db"),
    path.resolve(__dirname, "../../sql_app.db"),
  ];
  for (const p of candidates) {
    if (fs.existsSync(p)) return p;
  }
  // Fallback – create a fresh db in cwd so the app always starts
  return path.resolve(process.cwd(), "sql_app.db");
}

let _db: Database.Database | null = null;

export function getDb(): Database.Database {
  if (_db) return _db;
  const dbPath = getDbPath();
  _db = new Database(dbPath);

  // Ensure tables exist (idempotent – mirrors database.py schema)
  _db.exec(`
    CREATE TABLE IF NOT EXISTS properties (
      parcel_id   TEXT PRIMARY KEY,
      address     TEXT,
      owner_name  TEXT,
      owner_mailing_address TEXT,
      assessed_value REAL,
      last_sale_date TEXT,
      property_class_code TEXT
    );

    CREATE TABLE IF NOT EXISTS leads (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      address     TEXT,
      parcel_id   TEXT REFERENCES properties(parcel_id),
      source      TEXT,
      raw_data    TEXT,
      score       REAL,
      created_at  TEXT DEFAULT (datetime('now')),
      status      TEXT DEFAULT 'new'
    );

    CREATE TABLE IF NOT EXISTS contacts (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      lead_id     INTEGER REFERENCES leads(id),
      owner_name  TEXT,
      phone       TEXT,
      email       TEXT,
      source      TEXT
    );
  `);

  // Seed demo data if the leads table is empty
  const count = (_db.prepare("SELECT COUNT(*) as c FROM leads").get() as { c: number }).c;
  if (count === 0) {
    seedDemoData(_db);
  }

  return _db;
}

function seedDemoData(db: Database.Database) {
  const insertProp = db.prepare(`
    INSERT OR IGNORE INTO properties (parcel_id, address, owner_name, owner_mailing_address, assessed_value, last_sale_date, property_class_code)
    VALUES (?, ?, ?, ?, ?, ?, ?)
  `);
  const insertLead = db.prepare(`
    INSERT INTO leads (address, parcel_id, source, raw_data, score, created_at, status)
    VALUES (?, ?, ?, ?, ?, ?, ?)
  `);
  const insertContact = db.prepare(`
    INSERT INTO contacts (lead_id, owner_name, phone, email, source)
    VALUES (?, ?, ?, ?, ?)
  `);

  const props = [
    ["0500-001-001", "123 Main St, Huntington, NY 11743", "John Smith", "123 Main St, Huntington, NY 11743", 425000, "2018-06-15", "A"],
    ["0500-001-002", "456 Oak Ave, Babylon, NY 11702", "Mary Johnson", "PO Box 100, Babylon, NY 11702", 380000, "2015-03-22", "A"],
    ["0500-001-003", "789 Elm Rd, Islip, NY 11751", "Robert Davis", "789 Elm Rd, Islip, NY 11751", 510000, "2020-11-08", "A"],
    ["0500-001-004", "321 Pine St, Smithtown, NY 11787", "Patricia Wilson", "321 Pine St, Smithtown, NY 11787", 295000, "2012-07-30", "A"],
    ["0500-001-005", "654 Cedar Ln, Brentwood, NY 11717", "James Brown", "654 Cedar Ln, Brentwood, NY 11717", 340000, "2019-01-14", "A"],
    ["0500-001-006", "987 Maple Dr, Bay Shore, NY 11706", "Linda Martinez", "987 Maple Dr, Bay Shore, NY 11706", 460000, "2017-09-05", "A"],
    ["0500-001-007", "147 Birch Blvd, Coram, NY 11727", "Michael Taylor", "147 Birch Blvd, Coram, NY 11727", 275000, "2016-04-18", "A"],
    ["0500-001-008", "258 Willow Way, Medford, NY 11763", "Barbara Anderson", "258 Willow Way, Medford, NY 11763", 320000, "2021-08-22", "A"],
  ];

  const leads = [
    ["123 Main St, Huntington, NY 11743", "0500-001-001", "fire", JSON.stringify({ incident_date: "2026-03-12", incident_type: "Structure Fire", damage_estimate: "$45,000", report_number: "FD-2026-0312" }), 8.5, "2026-03-13", "new"],
    ["456 Oak Ave, Babylon, NY 11702", "0500-001-002", "probate", JSON.stringify({ case_number: "2026-PR-00142", filing_date: "2026-02-28", decedent: "George Johnson", estate_value: "$620,000" }), 7.2, "2026-03-01", "contacted"],
    ["789 Elm Rd, Islip, NY 11751", "0500-001-003", "obituary", JSON.stringify({ publication: "Newsday", date: "2026-04-05", name: "Helen Davis", survived_by: "Robert Davis (son)" }), 6.8, "2026-04-06", "new"],
    ["321 Pine St, Smithtown, NY 11787", "0500-001-004", "social", JSON.stringify({ platform: "Facebook", post_date: "2026-01-20", signal: "Moving sale post", engagement: 47 }), 5.5, "2026-01-21", "qualified"],
    ["654 Cedar Ln, Brentwood, NY 11717", "0500-001-005", "fire", JSON.stringify({ incident_date: "2026-04-18", incident_type: "Kitchen Fire", damage_estimate: "$12,000", report_number: "FD-2026-0418" }), 7.9, "2026-04-19", "new"],
    ["987 Maple Dr, Bay Shore, NY 11706", "0500-001-006", "probate", JSON.stringify({ case_number: "2026-PR-00289", filing_date: "2026-05-01", decedent: "Carlos Martinez", estate_value: "$890,000" }), 9.1, "2026-05-02", "new"],
    ["147 Birch Blvd, Coram, NY 11727", "0500-001-007", "obituary", JSON.stringify({ publication: "Suffolk Times", date: "2026-03-22", name: "Dorothy Taylor", survived_by: "Michael Taylor (son)" }), 6.3, "2026-03-23", "contacted"],
    ["258 Willow Way, Medford, NY 11763", "0500-001-008", "social", JSON.stringify({ platform: "Nextdoor", post_date: "2026-05-10", signal: "Asking for moving company recommendations", engagement: 12 }), 4.8, "2026-05-11", "new"],
  ];

  const contacts = [
    [1, "John Smith", "(631) 555-0101", "jsmith@email.com", "parcel_access"],
    [2, "Mary Johnson", "(631) 555-0202", "mjohnson@email.com", "parcel_access"],
    [3, "Robert Davis", "(631) 555-0303", "rdavis@email.com", "parcel_access"],
    [4, "Patricia Wilson", "(631) 555-0404", "pwilson@email.com", "parcel_access"],
    [5, "James Brown", "(631) 555-0505", "jbrown@email.com", "parcel_access"],
    [6, "Linda Martinez", "(631) 555-0606", "lmartinez@email.com", "parcel_access"],
    [7, "Michael Taylor", "(631) 555-0707", "mtaylor@email.com", "parcel_access"],
    [8, "Barbara Anderson", "(631) 555-0808", "banderson@email.com", "parcel_access"],
  ];

  const insertAll = db.transaction(() => {
    for (const p of props) insertProp.run(...(p as Parameters<typeof insertProp.run>));
    for (const l of leads) {
      const info = insertLead.run(...(l as Parameters<typeof insertLead.run>));
      const leadId = info.lastInsertRowid;
      const c = contacts.find((x) => x[0] === leads.indexOf(l) + 1);
      if (c) insertContact.run(leadId, c[1], c[2], c[3], c[4]);
    }
  });
  insertAll();
}
