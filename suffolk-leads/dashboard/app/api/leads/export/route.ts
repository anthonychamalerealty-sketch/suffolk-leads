import { NextRequest, NextResponse } from "next/server";
import { getDb } from "@/lib/db";

export const dynamic = "force-dynamic";

function escapeCSV(val: unknown): string {
  if (val === null || val === undefined) return "";
  const str = String(val);
  if (str.includes(",") || str.includes('"') || str.includes("\n")) {
    return `"${str.replace(/"/g, '""')}"`;
  }
  return str;
}

export async function GET(req: NextRequest) {
  try {
    const db = getDb();
    const { searchParams } = new URL(req.url);
    const source = searchParams.get("source") || "";
    const search = searchParams.get("search") || "";
    const status = searchParams.get("status") || "";

    let sql = `
      SELECT
        l.id,
        l.address,
        l.source,
        ROUND(l.score, 1) as score,
        l.created_at,
        l.status,
        c.owner_name,
        c.phone,
        c.email,
        p.owner_mailing_address,
        p.assessed_value,
        p.last_sale_date
      FROM leads l
      LEFT JOIN contacts c ON c.lead_id = l.id
      LEFT JOIN properties p ON p.parcel_id = l.parcel_id
      WHERE 1=1
    `;
    const params: string[] = [];

    if (source) {
      sql += " AND l.source = ?";
      params.push(source);
    }
    if (search) {
      sql += " AND l.address LIKE ?";
      params.push(`%${search}%`);
    }
    if (status) {
      sql += " AND l.status = ?";
      params.push(status);
    }

    sql += " ORDER BY l.created_at DESC";

    const rows = db.prepare(sql).all(...params) as Record<string, unknown>[];

    const headers = [
      "ID", "Address", "Source", "Score", "Date Found", "Status",
      "Owner Name", "Phone", "Email", "Mailing Address", "Assessed Value", "Last Sale Date",
    ];

    const csvLines = [
      headers.join(","),
      ...rows.map((r) =>
        [
          r.id, r.address, r.source, r.score, r.created_at, r.status,
          r.owner_name, r.phone, r.email, r.owner_mailing_address, r.assessed_value, r.last_sale_date,
        ]
          .map(escapeCSV)
          .join(",")
      ),
    ];

    const csv = csvLines.join("\n");
    const timestamp = new Date().toISOString().slice(0, 10);

    return new NextResponse(csv, {
      status: 200,
      headers: {
        "Content-Type": "text/csv",
        "Content-Disposition": `attachment; filename="suffolk-leads-${timestamp}.csv"`,
      },
    });
  } catch (err) {
    console.error("GET /api/leads/export error:", err);
    return NextResponse.json({ error: String(err) }, { status: 500 });
  }
}
