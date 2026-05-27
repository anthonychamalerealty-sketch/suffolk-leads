import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";
export const maxDuration = 60;

const GOOGLE_MAPS_API_KEY = process.env.GOOGLE_MAPS_API_KEY ?? "";
const OPENAI_API_KEY = process.env.OPENAI_API_KEY ?? "";

const GPT_PROMPT =
  'You are a real estate investor scoring property exterior condition. Score this property 1 to 10. Return only valid JSON: {"score": number, "roof": "good/fair/poor", "siding": "good/fair/poor", "windows": "good/fair/poor", "landscaping": "good/fair/poor", "vacancy_signs": true/false, "notes": "one sentence"}';

export interface ScoringResult {
  address: string;
  streetViewUrl: string;
  score: number;
  roof: string;
  siding: string;
  windows: string;
  landscaping: string;
  vacancy_signs: boolean;
  notes: string;
  error?: string;
}

async function scoreAddress(address: string): Promise<ScoringResult> {
  const encodedAddress = encodeURIComponent(address);
  const streetViewUrl = `https://maps.googleapis.com/maps/api/streetview?size=640x480&location=${encodedAddress}&key=${GOOGLE_MAPS_API_KEY}`;

  try {
    // Fetch the Street View image as base64
    const imgRes = await fetch(streetViewUrl);
    if (!imgRes.ok) {
      throw new Error(`Street View fetch failed: ${imgRes.status}`);
    }
    const imgBuffer = await imgRes.arrayBuffer();
    const base64Image = Buffer.from(imgBuffer).toString("base64");
    const mimeType = imgRes.headers.get("content-type") ?? "image/jpeg";

    // Call GPT-4o vision
    const openaiRes = await fetch("https://api.openai.com/v1/chat/completions", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${OPENAI_API_KEY}`,
      },
      body: JSON.stringify({
        model: "gpt-4o",
        max_tokens: 300,
        messages: [
          {
            role: "user",
            content: [
              { type: "text", text: GPT_PROMPT },
              {
                type: "image_url",
                image_url: { url: `data:${mimeType};base64,${base64Image}` },
              },
            ],
          },
        ],
      }),
    });

    if (!openaiRes.ok) {
      const errText = await openaiRes.text();
      throw new Error(`OpenAI error ${openaiRes.status}: ${errText}`);
    }

    const openaiData = await openaiRes.json();
    const rawContent: string = openaiData.choices?.[0]?.message?.content ?? "{}";

    // Strip markdown code fences if present
    const jsonStr = rawContent.replace(/```json\s*/gi, "").replace(/```\s*/gi, "").trim();
    const parsed = JSON.parse(jsonStr);

    return {
      address,
      streetViewUrl,
      score: Number(parsed.score),
      roof: String(parsed.roof ?? ""),
      siding: String(parsed.siding ?? ""),
      windows: String(parsed.windows ?? ""),
      landscaping: String(parsed.landscaping ?? ""),
      vacancy_signs: Boolean(parsed.vacancy_signs),
      notes: String(parsed.notes ?? ""),
    };
  } catch (err: unknown) {
    return {
      address,
      streetViewUrl,
      score: 0,
      roof: "",
      siding: "",
      windows: "",
      landscaping: "",
      vacancy_signs: false,
      notes: "",
      error: err instanceof Error ? err.message : String(err),
    };
  }
}

export async function POST(req: NextRequest) {
  try {
    const body = await req.json();
    const addresses: string[] = body.addresses ?? [];

    if (!addresses.length) {
      return NextResponse.json({ error: "No addresses provided" }, { status: 400 });
    }

    if (!GOOGLE_MAPS_API_KEY) {
      return NextResponse.json({ error: "GOOGLE_MAPS_API_KEY not configured" }, { status: 500 });
    }
    if (!OPENAI_API_KEY) {
      return NextResponse.json({ error: "OPENAI_API_KEY not configured" }, { status: 500 });
    }

    const results = await Promise.all(addresses.map(scoreAddress));
    return NextResponse.json({ results });
  } catch (err: unknown) {
    return NextResponse.json(
      { error: err instanceof Error ? err.message : String(err) },
      { status: 500 }
    );
  }
}
