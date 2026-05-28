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
      property_class_code TEXT,
      state       TEXT DEFAULT 'NY',
      county      TEXT DEFAULT 'Suffolk'
    );

    CREATE TABLE IF NOT EXISTS leads (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      address     TEXT,
      parcel_id   TEXT REFERENCES properties(parcel_id),
      source      TEXT,
      raw_data    TEXT,
      score       REAL,
      created_at  TEXT DEFAULT (datetime('now')),
      status      TEXT DEFAULT 'new',
      state       TEXT DEFAULT 'NY',
      county      TEXT DEFAULT 'Suffolk'
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
    INSERT OR IGNORE INTO properties (parcel_id, address, owner_name, owner_mailing_address, assessed_value, last_sale_date, property_class_code, state, county)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
  `);
  const insertLead = db.prepare(`
    INSERT INTO leads (address, parcel_id, source, raw_data, score, created_at, status, state, county)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
  `);
  const insertContact = db.prepare(`
    INSERT INTO contacts (lead_id, owner_name, phone, email, source)
    VALUES (?, ?, ?, ?, ?)
  `);

  const props = [
    // New York — Suffolk County
    ["0500-001-001", "123 Main St, Huntington, NY 11743", "John Smith", "123 Main St, Huntington, NY 11743", 425000, "2018-06-15", "A", "NY", "Suffolk"],
    ["0500-001-002", "456 Oak Ave, Babylon, NY 11702", "Mary Johnson", "PO Box 100, Babylon, NY 11702", 380000, "2015-03-22", "A", "NY", "Suffolk"],
    ["0500-001-003", "789 Elm Rd, Islip, NY 11751", "Robert Davis", "789 Elm Rd, Islip, NY 11751", 510000, "2020-11-08", "A", "NY", "Suffolk"],
    ["0500-001-004", "321 Pine St, Smithtown, NY 11787", "Patricia Wilson", "321 Pine St, Smithtown, NY 11787", 295000, "2012-07-30", "A", "NY", "Suffolk"],
    ["0500-001-005", "654 Cedar Ln, Brentwood, NY 11717", "James Brown", "654 Cedar Ln, Brentwood, NY 11717", 340000, "2019-01-14", "A", "NY", "Suffolk"],
    ["0500-001-006", "987 Maple Dr, Bay Shore, NY 11706", "Linda Martinez", "987 Maple Dr, Bay Shore, NY 11706", 460000, "2017-09-05", "A", "NY", "Suffolk"],
    ["0500-001-007", "147 Birch Blvd, Coram, NY 11727", "Michael Taylor", "147 Birch Blvd, Coram, NY 11727", 275000, "2016-04-18", "A", "NY", "Suffolk"],
    ["0500-001-008", "258 Willow Way, Medford, NY 11763", "Barbara Anderson", "258 Willow Way, Medford, NY 11763", 320000, "2021-08-22", "A", "NY", "Suffolk"],
    // Georgia — Fulton County
    ["GA-FUL-001", "234 Peachtree St NW, Atlanta, GA 30303", "William Harris", "234 Peachtree St NW, Atlanta, GA 30303", 520000, "2019-04-10", "R", "GA", "Fulton"],
    ["GA-FUL-002", "567 Roswell Rd, Sandy Springs, GA 30342", "Susan Clark", "567 Roswell Rd, Sandy Springs, GA 30342", 680000, "2017-11-20", "R", "GA", "Fulton"],
    // Georgia — Gwinnett County
    ["GA-GWI-001", "890 Lawrenceville Hwy, Lawrenceville, GA 30046", "Charles Lewis", "890 Lawrenceville Hwy, Lawrenceville, GA 30046", 310000, "2020-07-15", "R", "GA", "Gwinnett"],
    // Georgia — Cobb County
    ["GA-COB-001", "321 Marietta Blvd NW, Marietta, GA 30064", "Karen Robinson", "321 Marietta Blvd NW, Marietta, GA 30064", 395000, "2018-09-03", "R", "GA", "Cobb"],
    // Georgia — DeKalb County
    ["GA-DEK-001", "654 Candler Rd, Decatur, GA 30032", "Thomas Walker", "654 Candler Rd, Decatur, GA 30032", 285000, "2021-02-28", "R", "GA", "DeKalb"],
    // Georgia — Chatham County
    ["GA-CHA-001", "987 Waters Ave, Savannah, GA 31404", "Nancy Hall", "987 Waters Ave, Savannah, GA 31404", 340000, "2016-06-12", "R", "GA", "Chatham"],
    // Georgia — Clarke County
    ["GA-CLA-001", "147 Broad St, Athens, GA 30601", "Daniel Young", "147 Broad St, Athens, GA 30601", 255000, "2022-01-07", "R", "GA", "Clarke"],
  ];

  const leads = [
    // New York — Suffolk County
    ["123 Main St, Huntington, NY 11743", "0500-001-001", "fire", JSON.stringify({ incident_date: "2026-03-12", incident_type: "Structure Fire", damage_estimate: "$45,000", report_number: "FD-2026-0312" }), 8.5, "2026-03-13", "new", "NY", "Suffolk"],
    ["456 Oak Ave, Babylon, NY 11702", "0500-001-002", "probate", JSON.stringify({ case_number: "2026-PR-00142", filing_date: "2026-02-28", decedent: "George Johnson", estate_value: "$620,000" }), 7.2, "2026-03-01", "contacted", "NY", "Suffolk"],
    ["789 Elm Rd, Islip, NY 11751", "0500-001-003", "obituary", JSON.stringify({ publication: "Newsday", date: "2026-04-05", name: "Helen Davis", survived_by: "Robert Davis (son)" }), 6.8, "2026-04-06", "new", "NY", "Suffolk"],
    ["321 Pine St, Smithtown, NY 11787", "0500-001-004", "social", JSON.stringify({ platform: "Facebook", post_date: "2026-01-20", signal: "Moving sale post", engagement: 47 }), 5.5, "2026-01-21", "qualified", "NY", "Suffolk"],
    ["654 Cedar Ln, Brentwood, NY 11717", "0500-001-005", "fire", JSON.stringify({ incident_date: "2026-04-18", incident_type: "Kitchen Fire", damage_estimate: "$12,000", report_number: "FD-2026-0418" }), 7.9, "2026-04-19", "new", "NY", "Suffolk"],
    ["987 Maple Dr, Bay Shore, NY 11706", "0500-001-006", "probate", JSON.stringify({ case_number: "2026-PR-00289", filing_date: "2026-05-01", decedent: "Carlos Martinez", estate_value: "$890,000" }), 9.1, "2026-05-02", "new", "NY", "Suffolk"],
    ["147 Birch Blvd, Coram, NY 11727", "0500-001-007", "obituary", JSON.stringify({ publication: "Suffolk Times", date: "2026-03-22", name: "Dorothy Taylor", survived_by: "Michael Taylor (son)" }), 6.3, "2026-03-23", "contacted", "NY", "Suffolk"],
    ["258 Willow Way, Medford, NY 11763", "0500-001-008", "social", JSON.stringify({ platform: "Nextdoor", post_date: "2026-05-10", signal: "Asking for moving company recommendations", engagement: 12 }), 4.8, "2026-05-11", "new", "NY", "Suffolk"],
    // Georgia — Fulton County
    ["234 Peachtree St NW, Atlanta, GA 30303", "GA-FUL-001", "fire", JSON.stringify({ incident_date: "2026-04-22", incident_type: "Residential Structure Fire", damage_estimate: "$78,000", report_number: "AFR-2026-0422" }), 8.8, "2026-04-23", "new", "GA", "Fulton"],
    ["567 Roswell Rd, Sandy Springs, GA 30342", "GA-FUL-002", "probate", JSON.stringify({ case_number: "2026-PR-FUL-0091", filing_date: "2026-03-15", decedent: "Margaret Clark", estate_value: "$1,200,000" }), 9.3, "2026-03-16", "new", "GA", "Fulton"],
    // Georgia — Gwinnett County
    ["890 Lawrenceville Hwy, Lawrenceville, GA 30046", "GA-GWI-001", "obituary", JSON.stringify({ publication: "Atlanta Journal-Constitution", date: "2026-05-01", name: "Harold Lewis", survived_by: "Charles Lewis (son)" }), 7.1, "2026-05-02", "new", "GA", "Gwinnett"],
    // Georgia — Cobb County
    ["321 Marietta Blvd NW, Marietta, GA 30064", "GA-COB-001", "fire", JSON.stringify({ incident_date: "2026-05-05", incident_type: "Dwelling Fire", damage_estimate: "$55,000", report_number: "CCF-2026-0505" }), 8.2, "2026-05-06", "new", "GA", "Cobb"],
    // Georgia — DeKalb County
    ["654 Candler Rd, Decatur, GA 30032", "GA-DEK-001", "social", JSON.stringify({ platform: "Craigslist Atlanta", post_date: "2026-05-12", signal: "as-is sale, motivated seller", post_url: "https://atlanta.craigslist.org/hhh/example" }), 7.6, "2026-05-12", "new", "GA", "DeKalb"],
    // Georgia — Chatham County
    ["987 Waters Ave, Savannah, GA 31404", "GA-CHA-001", "probate", JSON.stringify({ case_number: "2026-PR-CHA-0044", filing_date: "2026-04-08", decedent: "Frances Hall", estate_value: "$480,000" }), 7.8, "2026-04-09", "contacted", "GA", "Chatham"],
    // Georgia — Clarke County
    ["147 Broad St, Athens, GA 30601", "GA-CLA-001", "obituary", JSON.stringify({ publication: "Savannah Morning News", date: "2026-04-28", name: "Eugene Young", survived_by: "Daniel Young (son)" }), 6.5, "2026-04-29", "new", "GA", "Clarke"],
  ];

  const contacts = [
    // New York — Suffolk County
    [1, "John Smith", "(631) 555-0101", "jsmith@email.com", "parcel_access"],
    [2, "Mary Johnson", "(631) 555-0202", "mjohnson@email.com", "parcel_access"],
    [3, "Robert Davis", "(631) 555-0303", "rdavis@email.com", "parcel_access"],
    [4, "Patricia Wilson", "(631) 555-0404", "pwilson@email.com", "parcel_access"],
    [5, "James Brown", "(631) 555-0505", "jbrown@email.com", "parcel_access"],
    [6, "Linda Martinez", "(631) 555-0606", "lmartinez@email.com", "parcel_access"],
    [7, "Michael Taylor", "(631) 555-0707", "mtaylor@email.com", "parcel_access"],
    [8, "Barbara Anderson", "(631) 555-0808", "banderson@email.com", "parcel_access"],
    // Georgia leads — contacts
    [9, "William Harris", "(404) 555-0901", "wharris@email.com", "parcel_access"],
    [10, "Susan Clark", "(404) 555-1001", "sclark@email.com", "parcel_access"],
    [11, "Charles Lewis", "(770) 555-1101", "clewis@email.com", "parcel_access"],
    [12, "Karen Robinson", "(770) 555-1201", "krobinson@email.com", "parcel_access"],
    [13, "Thomas Walker", "(404) 555-1301", "twalker@email.com", "parcel_access"],
    [14, "Nancy Hall", "(912) 555-1401", "nhall@email.com", "parcel_access"],
    [15, "Daniel Young", "(706) 555-1501", "dyoung@email.com", "parcel_access"],
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
