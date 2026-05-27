"use client";

import { useRef, useState } from "react";
import Link from "next/link";

// ─── Types ────────────────────────────────────────────────────────────────────

interface ScoringResult {
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

// ─── Helpers ──────────────────────────────────────────────────────────────────

function rowColor(score: number): string {
  if (score >= 1 && score <= 4) return "rgba(239,68,68,0.12)";
  if (score >= 5 && score <= 7) return "rgba(234,179,8,0.12)";
  if (score >= 8 && score <= 10) return "rgba(34,197,94,0.12)";
  return "transparent";
}

function scoreBorderColor(score: number): string {
  if (score >= 1 && score <= 4) return "#ef4444";
  if (score >= 5 && score <= 7) return "#eab308";
  if (score >= 8 && score <= 10) return "#22c55e";
  return "var(--border)";
}

function scoreTextColor(score: number): string {
  if (score >= 1 && score <= 4) return "#ef4444";
  if (score >= 5 && score <= 7) return "#eab308";
  if (score >= 8 && score <= 10) return "#22c55e";
  return "var(--muted)";
}

function ratingBadge(value: string) {
  const colors: Record<string, { bg: string; text: string }> = {
    good: { bg: "rgba(34,197,94,0.15)", text: "#22c55e" },
    fair: { bg: "rgba(234,179,8,0.15)", text: "#eab308" },
    poor: { bg: "rgba(239,68,68,0.15)", text: "#ef4444" },
  };
  const c = colors[value?.toLowerCase()] ?? { bg: "rgba(255,255,255,0.05)", text: "var(--muted)" };
  return (
    <span style={{
      display: "inline-block",
      padding: "2px 8px",
      borderRadius: 10,
      fontSize: 11,
      fontWeight: 600,
      background: c.bg,
      color: c.text,
      textTransform: "capitalize",
    }}>
      {value || "—"}
    </span>
  );
}

function parseCSV(text: string): string[] {
  const lines = text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  if (lines.length < 2) return [];

  const headers = lines[0].split(",").map((h) => h.trim().replace(/^"|"$/g, "").toLowerCase());
  const addrIdx = headers.findIndex((h) => h === "address");
  if (addrIdx === -1) return [];

  return lines
    .slice(1)
    .map((line) => {
      // Simple CSV parse: handle quoted fields
      const cols: string[] = [];
      let cur = "";
      let inQuote = false;
      for (let i = 0; i < line.length; i++) {
        const ch = line[i];
        if (ch === '"') { inQuote = !inQuote; continue; }
        if (ch === "," && !inQuote) { cols.push(cur.trim()); cur = ""; continue; }
        cur += ch;
      }
      cols.push(cur.trim());
      return cols[addrIdx] ?? "";
    })
    .filter(Boolean);
}

function exportToCSV(results: ScoringResult[]) {
  const headers = ["Address", "Score", "Roof", "Siding", "Windows", "Landscaping", "Vacancy Signs", "Notes", "Street View URL"];
  const rows = results.map((r) => [
    `"${r.address.replace(/"/g, '""')}"`,
    r.error ? "ERROR" : r.score,
    r.roof,
    r.siding,
    r.windows,
    r.landscaping,
    r.vacancy_signs ? "Yes" : "No",
    `"${(r.notes ?? "").replace(/"/g, '""')}"`,
    `"${r.streetViewUrl}"`,
  ]);
  const csv = [headers.join(","), ...rows.map((r) => r.join(","))].join("\n");
  const blob = new Blob([csv], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `property-scores-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

// ─── Component ────────────────────────────────────────────────────────────────

const BATCH_SIZE = 10;

export default function ScoringDashboard() {
  const fileRef = useRef<HTMLInputElement>(null);
  const [addresses, setAddresses] = useState<string[]>([]);
  const [fileName, setFileName] = useState<string>("");
  const [results, setResults] = useState<ScoringResult[]>([]);
  const [processing, setProcessing] = useState(false);
  const [progress, setProgress] = useState(0);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);

  function handleFile(file: File) {
    setError(null);
    setResults([]);
    setProgress(0);
    setTotal(0);
    const reader = new FileReader();
    reader.onload = (e) => {
      const text = e.target?.result as string;
      const parsed = parseCSV(text);
      if (!parsed.length) {
        setError("No addresses found. Make sure the CSV has an 'address' column.");
        setAddresses([]);
        setFileName("");
        return;
      }
      setAddresses(parsed);
      setFileName(file.name);
    };
    reader.readAsText(file);
  }

  function handleFileInput(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (file) handleFile(file);
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files?.[0];
    if (file && file.name.endsWith(".csv")) handleFile(file);
    else setError("Please drop a .csv file.");
  }

  async function handleProcess() {
    if (!addresses.length) return;
    setProcessing(true);
    setResults([]);
    setError(null);
    setTotal(addresses.length);
    setProgress(0);

    const allResults: ScoringResult[] = [];
    const batches: string[][] = [];
    for (let i = 0; i < addresses.length; i += BATCH_SIZE) {
      batches.push(addresses.slice(i, i + BATCH_SIZE));
    }

    for (const batch of batches) {
      try {
        const res = await fetch("/api/scoring", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ addresses: batch }),
        });
        if (!res.ok) {
          const errData = await res.json().catch(() => ({}));
          throw new Error(errData.error ?? `HTTP ${res.status}`);
        }
        const data = await res.json();
        allResults.push(...(data.results ?? []));
        setResults([...allResults]);
        setProgress(allResults.length);
      } catch (err: unknown) {
        setError(err instanceof Error ? err.message : String(err));
        break;
      }
    }

    setProcessing(false);
  }

  const progressPct = total > 0 ? Math.round((progress / total) * 100) : 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", minHeight: "100vh", background: "var(--bg)" }}>
      {/* Header */}
      <header style={{
        background: "var(--surface)",
        borderBottom: "1px solid var(--border)",
        padding: "0 24px",
        height: 60,
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        position: "sticky",
        top: 0,
        zIndex: 100,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <div style={{
            width: 32, height: 32, borderRadius: 8,
            background: "linear-gradient(135deg, #3b82f6, #8b5cf6)",
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 16,
          }}>🏠</div>
          <div>
            <div style={{ fontWeight: 700, fontSize: 16, color: "var(--text)" }}>Suffolk Leads</div>
            <div style={{ fontSize: 11, color: "var(--muted)" }}>Real Estate Intelligence</div>
          </div>
        </div>
        {/* Nav */}
        <nav style={{ display: "flex", gap: 4 }}>
          <Link href="/" style={{
            padding: "6px 14px",
            borderRadius: 8,
            fontSize: 13,
            fontWeight: 500,
            color: "var(--muted)",
            textDecoration: "none",
            transition: "background 0.1s, color 0.1s",
          }}
            onMouseEnter={(e) => { (e.currentTarget as HTMLAnchorElement).style.background = "rgba(255,255,255,0.06)"; (e.currentTarget as HTMLAnchorElement).style.color = "var(--text)"; }}
            onMouseLeave={(e) => { (e.currentTarget as HTMLAnchorElement).style.background = "transparent"; (e.currentTarget as HTMLAnchorElement).style.color = "var(--muted)"; }}
          >
            Leads
          </Link>
          <Link href="/scoring" style={{
            padding: "6px 14px",
            borderRadius: 8,
            fontSize: 13,
            fontWeight: 500,
            color: "var(--accent)",
            background: "rgba(59,130,246,0.12)",
            textDecoration: "none",
          }}>
            Scoring
          </Link>
        </nav>
      </header>

      {/* Main content */}
      <main style={{ flex: 1, padding: "32px 24px", maxWidth: 1400, margin: "0 auto", width: "100%" }}>
        {/* Page title */}
        <div style={{ marginBottom: 28 }}>
          <h1 style={{ fontSize: 22, fontWeight: 700, color: "var(--text)", margin: 0 }}>
            Property Exterior Scoring
          </h1>
          <p style={{ fontSize: 13, color: "var(--muted)", marginTop: 6 }}>
            Upload a CSV with an <code style={{ background: "var(--surface2)", padding: "1px 6px", borderRadius: 4, fontSize: 12 }}>address</code> column.
            Each property will be scored 1–10 via Google Street View + GPT-4o vision.
          </p>
        </div>

        {/* Upload card */}
        <div style={{
          background: "var(--surface)",
          border: `2px dashed ${dragOver ? "var(--accent)" : "var(--border)"}`,
          borderRadius: 12,
          padding: 32,
          textAlign: "center",
          marginBottom: 24,
          transition: "border-color 0.15s",
          cursor: "pointer",
        }}
          onClick={() => fileRef.current?.click()}
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={handleDrop}
        >
          <input
            ref={fileRef}
            type="file"
            accept=".csv"
            style={{ display: "none" }}
            onChange={handleFileInput}
          />
          <div style={{ fontSize: 36, marginBottom: 12 }}>📂</div>
          {fileName ? (
            <>
              <div style={{ fontSize: 15, fontWeight: 600, color: "var(--text)" }}>{fileName}</div>
              <div style={{ fontSize: 13, color: "var(--muted)", marginTop: 4 }}>
                {addresses.length} address{addresses.length !== 1 ? "es" : ""} found — click to replace
              </div>
            </>
          ) : (
            <>
              <div style={{ fontSize: 15, fontWeight: 600, color: "var(--text)" }}>
                Drop a CSV file here or click to browse
              </div>
              <div style={{ fontSize: 13, color: "var(--muted)", marginTop: 4 }}>
                Required column: <strong>address</strong>
              </div>
            </>
          )}
        </div>

        {error && (
          <div style={{
            background: "rgba(239,68,68,0.1)",
            border: "1px solid rgba(239,68,68,0.3)",
            borderRadius: 8,
            padding: "12px 16px",
            color: "#f87171",
            fontSize: 13,
            marginBottom: 20,
          }}>
            {error}
          </div>
        )}

        {/* Action buttons */}
        <div style={{ display: "flex", gap: 12, marginBottom: 28, alignItems: "center" }}>
          <button
            onClick={handleProcess}
            disabled={!addresses.length || processing}
            style={{
              padding: "10px 22px",
              borderRadius: 8,
              border: "none",
              background: !addresses.length || processing ? "var(--surface2)" : "var(--accent)",
              color: !addresses.length || processing ? "var(--muted)" : "#fff",
              fontWeight: 600,
              fontSize: 14,
              cursor: !addresses.length || processing ? "not-allowed" : "pointer",
              transition: "background 0.15s",
            }}
          >
            {processing ? "Processing…" : `Score ${addresses.length || ""} Addresses`}
          </button>

          {results.length > 0 && !processing && (
            <button
              onClick={() => exportToCSV(results)}
              style={{
                padding: "10px 22px",
                borderRadius: 8,
                border: "1px solid var(--border)",
                background: "transparent",
                color: "var(--text)",
                fontWeight: 600,
                fontSize: 14,
                cursor: "pointer",
                display: "flex",
                alignItems: "center",
                gap: 6,
              }}
            >
              ⬇ Export CSV
            </button>
          )}

          {results.length > 0 && (
            <div style={{ marginLeft: "auto", fontSize: 13, color: "var(--muted)" }}>
              {results.filter((r) => !r.error).length} scored
              {results.filter((r) => r.error).length > 0 && (
                <span style={{ color: "#f87171", marginLeft: 8 }}>
                  {results.filter((r) => r.error).length} error{results.filter((r) => r.error).length !== 1 ? "s" : ""}
                </span>
              )}
            </div>
          )}
        </div>

        {/* Progress bar */}
        {processing && (
          <div style={{ marginBottom: 28 }}>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, color: "var(--muted)", marginBottom: 6 }}>
              <span>Processing batch {Math.ceil(progress / BATCH_SIZE)} of {Math.ceil(total / BATCH_SIZE)}</span>
              <span>{progress} / {total} addresses ({progressPct}%)</span>
            </div>
            <div style={{
              height: 8,
              background: "var(--surface2)",
              borderRadius: 4,
              overflow: "hidden",
            }}>
              <div style={{
                height: "100%",
                width: `${progressPct}%`,
                background: "linear-gradient(90deg, #3b82f6, #8b5cf6)",
                borderRadius: 4,
                transition: "width 0.3s ease",
              }} />
            </div>
          </div>
        )}

        {/* Legend */}
        {results.length > 0 && (
          <div style={{ display: "flex", gap: 16, marginBottom: 16, fontSize: 12 }}>
            {[
              { label: "Score 1–4 (Poor)", color: "#ef4444", bg: "rgba(239,68,68,0.12)" },
              { label: "Score 5–7 (Fair)", color: "#eab308", bg: "rgba(234,179,8,0.12)" },
              { label: "Score 8–10 (Good)", color: "#22c55e", bg: "rgba(34,197,94,0.12)" },
            ].map((l) => (
              <div key={l.label} style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <div style={{ width: 12, height: 12, borderRadius: 3, background: l.bg, border: `1px solid ${l.color}` }} />
                <span style={{ color: "var(--muted)" }}>{l.label}</span>
              </div>
            ))}
          </div>
        )}

        {/* Results table */}
        {results.length > 0 && (
          <div style={{
            background: "var(--surface)",
            borderRadius: 12,
            border: "1px solid var(--border)",
            overflow: "hidden",
          }}>
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                <thead>
                  <tr style={{ background: "var(--surface2)", borderBottom: "2px solid var(--border)" }}>
                    {["Photo", "Address", "Score", "Roof", "Siding", "Windows", "Landscaping", "Vacant?", "Notes"].map((h) => (
                      <th key={h} style={{
                        padding: "10px 14px",
                        textAlign: "left",
                        color: "var(--muted)",
                        fontWeight: 600,
                        fontSize: 11,
                        textTransform: "uppercase",
                        letterSpacing: "0.05em",
                        whiteSpace: "nowrap",
                      }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {results.map((r, i) => (
                    <tr
                      key={i}
                      style={{
                        background: r.error ? "rgba(239,68,68,0.06)" : rowColor(r.score),
                        borderBottom: "1px solid var(--border)",
                        borderLeft: `3px solid ${r.error ? "#ef4444" : scoreBorderColor(r.score)}`,
                      }}
                    >
                      {/* Thumbnail */}
                      <td style={{ padding: "8px 14px" }}>
                        {r.streetViewUrl ? (
                          <a href={r.streetViewUrl} target="_blank" rel="noopener noreferrer">
                            {/* eslint-disable-next-line @next/next/no-img-element */}
                            <img
                              src={r.streetViewUrl}
                              alt={r.address}
                              style={{
                                width: 96,
                                height: 64,
                                objectFit: "cover",
                                borderRadius: 6,
                                border: "1px solid var(--border)",
                                display: "block",
                              }}
                            />
                          </a>
                        ) : (
                          <div style={{
                            width: 96, height: 64, borderRadius: 6,
                            background: "var(--surface2)",
                            display: "flex", alignItems: "center", justifyContent: "center",
                            color: "var(--muted)", fontSize: 11,
                          }}>No image</div>
                        )}
                      </td>

                      {/* Address */}
                      <td style={{ padding: "8px 14px", color: "var(--text)", maxWidth: 220 }}>
                        <div style={{ fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {r.address}
                        </div>
                        {r.error && (
                          <div style={{ color: "#f87171", fontSize: 11, marginTop: 2 }}>
                            Error: {r.error}
                          </div>
                        )}
                      </td>

                      {/* Score */}
                      <td style={{ padding: "8px 14px", textAlign: "center" }}>
                        {r.error ? (
                          <span style={{ color: "var(--muted)" }}>—</span>
                        ) : (
                          <div style={{
                            display: "inline-flex",
                            alignItems: "center",
                            justifyContent: "center",
                            width: 36,
                            height: 36,
                            borderRadius: "50%",
                            background: rowColor(r.score),
                            border: `2px solid ${scoreBorderColor(r.score)}`,
                            fontWeight: 700,
                            fontSize: 15,
                            color: scoreTextColor(r.score),
                          }}>
                            {r.score}
                          </div>
                        )}
                      </td>

                      {/* Sub-ratings */}
                      <td style={{ padding: "8px 14px" }}>{ratingBadge(r.roof)}</td>
                      <td style={{ padding: "8px 14px" }}>{ratingBadge(r.siding)}</td>
                      <td style={{ padding: "8px 14px" }}>{ratingBadge(r.windows)}</td>
                      <td style={{ padding: "8px 14px" }}>{ratingBadge(r.landscaping)}</td>

                      {/* Vacancy */}
                      <td style={{ padding: "8px 14px", textAlign: "center" }}>
                        {r.error ? (
                          <span style={{ color: "var(--muted)" }}>—</span>
                        ) : (
                          <span style={{
                            display: "inline-block",
                            padding: "2px 8px",
                            borderRadius: 10,
                            fontSize: 11,
                            fontWeight: 600,
                            background: r.vacancy_signs ? "rgba(239,68,68,0.15)" : "rgba(34,197,94,0.15)",
                            color: r.vacancy_signs ? "#ef4444" : "#22c55e",
                          }}>
                            {r.vacancy_signs ? "Yes" : "No"}
                          </span>
                        )}
                      </td>

                      {/* Notes */}
                      <td style={{ padding: "8px 14px", color: "var(--muted)", maxWidth: 260, fontSize: 12 }}>
                        {r.notes || "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Empty state */}
        {!processing && results.length === 0 && addresses.length > 0 && (
          <div style={{
            background: "var(--surface)",
            borderRadius: 12,
            border: "1px solid var(--border)",
            padding: 48,
            textAlign: "center",
            color: "var(--muted)",
          }}>
            <div style={{ fontSize: 32, marginBottom: 12 }}>🏘</div>
            <div style={{ fontSize: 15, fontWeight: 600, color: "var(--text)" }}>
              {addresses.length} address{addresses.length !== 1 ? "es" : ""} ready
            </div>
            <div style={{ fontSize: 13, marginTop: 6 }}>
              Click <strong style={{ color: "var(--text)" }}>Score Addresses</strong> to begin processing in batches of {BATCH_SIZE}.
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
