import { NextRequest, NextResponse } from "next/server";
import { getDb } from "@/lib/db";

export const dynamic = "force-dynamic";

export async function GET(
  _req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  try {
    const db = getDb();
    const { id } = await params;

    const lead = db
      .prepare(
        `SELECT
          l.id, l.address, l.source, l.score, l.created_at, l.status, l.raw_data, l.parcel_id,
          c.owner_name, c.phone, c.email,
          p.owner_mailing_address, p.assessed_value, p.last_sale_date, p.property_class_code
        FROM leads l
        LEFT JOIN contacts c ON c.lead_id = l.id
        LEFT JOIN properties p ON p.parcel_id = l.parcel_id
        WHERE l.id = ?`
      )
      .get(id);

    if (!lead) {
      return NextResponse.json({ error: "Lead not found" }, { status: 404 });
    }
    return NextResponse.json({ lead });
  } catch (err) {
    console.error("GET /api/leads/[id] error:", err);
    return NextResponse.json({ error: String(err) }, { status: 500 });
  }
}

export async function PATCH(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  try {
    const db = getDb();
    const { id } = await params;
    const body = await req.json();
    const { status } = body;

    if (!status) {
      return NextResponse.json({ error: "status is required" }, { status: 400 });
    }

    db.prepare("UPDATE leads SET status = ? WHERE id = ?").run(status, id);
    return NextResponse.json({ success: true });
  } catch (err) {
    console.error("PATCH /api/leads/[id] error:", err);
    return NextResponse.json({ error: String(err) }, { status: 500 });
  }
}
