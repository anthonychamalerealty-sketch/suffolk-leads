import { NextRequest, NextResponse } from "next/server";
import { getDb } from "@/lib/db";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  try {
    const db = getDb();
    const { searchParams } = new URL(req.url);
    const source = searchParams.get("source") || "";
    const search = searchParams.get("search") || "";
    const status = searchParams.get("status") || "";

    const state = searchParams.get("state") || "";
    const county = searchParams.get("county") || "";

    let sql = `
      SELECT
        l.id,
        l.address,
        l.source,
        l.score,
        l.created_at,
        l.status,
        l.raw_data,
        l.parcel_id,
        l.state,
        l.county,
        c.owner_name,
        c.phone,
        c.email,
        p.owner_mailing_address,
        p.assessed_value,
        p.last_sale_date,
        p.property_class_code
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
    if (state) {
      sql += " AND l.state = ?";
      params.push(state);
    }
    if (county) {
      sql += " AND l.county = ?";
      params.push(county);
    }

    sql += " ORDER BY l.created_at DESC";

    const rows = db.prepare(sql).all(...params);
    return NextResponse.json({ leads: rows });
  } catch (err) {
    console.error("GET /api/leads error:", err);
    return NextResponse.json({ error: String(err) }, { status: 500 });
  }
}
