"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import type { Lead, LeadSource, LeadStatus } from "@/lib/types";
import LeadSidePanel from "./LeadSidePanel";
import ScoreBadge from "./ScoreBadge";
import SourceBadge from "./SourceBadge";
import StatusBadge from "./StatusBadge";

const SOURCES: { key: LeadSource | "all"; label: string }[] = [
  { key: "all", label: "All Sources" },
  { key: "fire", label: "🔥 Fire" },
  { key: "probate", label: "⚖️ Probate" },
  { key: "obituary", label: "📰 Obituary" },
  { key: "social", label: "📱 Social" },
];

export default function LeadsDashboard() {
  const [leads, setLeads] = useState<Lead[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [source, setSource] = useState<LeadSource | "all">("all");
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [selectedLead, setSelectedLead] = useState<Lead | null>(null);
  const [exporting, setExporting] = useState(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Debounce search input
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => setDebouncedSearch(search), 300);
    return () => { if (debounceRef.current) clearTimeout(debounceRef.current); };
  }, [search]);

  const fetchLeads = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      if (source !== "all") params.set("source", source);
      if (debouncedSearch) params.set("search", debouncedSearch);
      const res = await fetch(`/api/leads?${params.toString()}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setLeads(data.leads ?? []);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [source, debouncedSearch]);

  useEffect(() => { fetchLeads(); }, [fetchLeads]);

  const handleExport = async () => {
    setExporting(true);
    try {
      const params = new URLSearchParams();
      if (source !== "all") params.set("source", source);
      if (debouncedSearch) params.set("search", debouncedSearch);
      const res = await fetch(`/api/leads/export?${params.toString()}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `suffolk-leads-${new Date().toISOString().slice(0, 10)}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    } finally {
      setExporting(false);
    }
  };

  const handleStatusChange = async (id: number, newStatus: LeadStatus) => {
    await fetch(`/api/leads/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: newStatus }),
    });
    setLeads((prev) =>
      prev.map((l) => (l.id === id ? { ...l, status: newStatus } : l))
    );
    if (selectedLead?.id === id) {
      setSelectedLead((prev) => prev ? { ...prev, status: newStatus } : prev);
    }
  };

  const stats = {
    total: leads.length,
    new: leads.filter((l) => l.status === "new").length,
    contacted: leads.filter((l) => l.status === "contacted").length,
    qualified: leads.filter((l) => l.status === "qualified").length,
  };

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
        <div style={{ display: "flex", alignItems: "center", gap: 24 }}>
          <div style={{ display: "flex", gap: 20 }}>
            {[
              { label: "Total", value: stats.total, color: "#3b82f6" },
              { label: "New", value: stats.new, color: "#3b82f6" },
              { label: "Contacted", value: stats.contacted, color: "#f59e0b" },
              { label: "Qualified", value: stats.qualified, color: "#10b981" },
            ].map((s) => (
              <div key={s.label} style={{ textAlign: "center" }}>
                <div style={{ fontSize: 18, fontWeight: 700, color: s.color }}>{s.value}</div>
                <div style={{ fontSize: 11, color: "var(--muted)" }}>{s.label}</div>
              </div>
            ))}
          </div>
          <nav style={{ display: "flex", gap: 4, borderLeft: "1px solid var(--border)", paddingLeft: 20 }}>
            <Link href="/" style={{
              padding: "6px 14px",
              borderRadius: 8,
              fontSize: 13,
              fontWeight: 500,
              color: "var(--accent)",
              background: "rgba(59,130,246,0.12)",
              textDecoration: "none",
            }}>Leads</Link>
            <Link href="/scoring" style={{
              padding: "6px 14px",
              borderRadius: 8,
              fontSize: 13,
              fontWeight: 500,
              color: "var(--muted)",
              textDecoration: "none",
            }}>Scoring</Link>
          </nav>
        </div>
      </header>

      {/* Toolbar */}
      <div style={{
        background: "var(--surface)",
        borderBottom: "1px solid var(--border)",
        padding: "12px 24px",
        display: "flex",
        alignItems: "center",
        gap: 12,
        flexWrap: "wrap",
      }}>
        {/* Source filters */}
        <div style={{ display: "flex", gap: 6 }}>
          {SOURCES.map((s) => (
            <button
              key={s.key}
              onClick={() => setSource(s.key)}
              style={{
                padding: "6px 14px",
                borderRadius: 20,
                border: "1px solid",
                borderColor: source === s.key ? "var(--accent)" : "var(--border)",
                background: source === s.key ? "rgba(59,130,246,0.15)" : "transparent",
                color: source === s.key ? "var(--accent)" : "var(--muted)",
                cursor: "pointer",
                fontSize: 13,
                fontWeight: source === s.key ? 600 : 400,
                transition: "all 0.15s",
              }}
            >
              {s.label}
            </button>
          ))}
        </div>

        {/* Search */}
        <div style={{ flex: 1, minWidth: 200, position: "relative" }}>
          <span style={{
            position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)",
            color: "var(--muted)", fontSize: 14, pointerEvents: "none",
          }}>🔍</span>
          <input
            type="text"
            placeholder="Search by address..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            style={{
              width: "100%",
              padding: "7px 12px 7px 32px",
              background: "var(--surface2)",
              border: "1px solid var(--border)",
              borderRadius: 8,
              color: "var(--text)",
              fontSize: 13,
              outline: "none",
            }}
          />
        </div>

        {/* Export */}
        <button
          onClick={handleExport}
          disabled={exporting || leads.length === 0}
          style={{
            padding: "7px 16px",
            background: exporting ? "var(--surface2)" : "rgba(59,130,246,0.15)",
            border: "1px solid",
            borderColor: "var(--accent)",
            borderRadius: 8,
            color: "var(--accent)",
            cursor: exporting || leads.length === 0 ? "not-allowed" : "pointer",
            fontSize: 13,
            fontWeight: 600,
            display: "flex",
            alignItems: "center",
            gap: 6,
            opacity: leads.length === 0 ? 0.5 : 1,
          }}
        >
          {exporting ? "⏳ Exporting..." : "⬇️ Export CSV"}
        </button>
      </div>

      {/* Main content */}
      <div style={{ display: "flex", flex: 1, overflow: "hidden" }}>
        {/* Table area */}
        <div style={{ flex: 1, overflow: "auto", padding: "0 0 24px 0" }}>
          {loading ? (
            <div style={{ padding: 48, textAlign: "center", color: "var(--muted)" }}>
              <div style={{ fontSize: 32, marginBottom: 12 }}>⏳</div>
              Loading leads...
            </div>
          ) : error ? (
            <div style={{ padding: 48, textAlign: "center", color: "#ef4444" }}>
              <div style={{ fontSize: 32, marginBottom: 12 }}>⚠️</div>
              {error}
            </div>
          ) : leads.length === 0 ? (
            <div style={{ padding: 48, textAlign: "center", color: "var(--muted)" }}>
              <div style={{ fontSize: 48, marginBottom: 12 }}>🏚️</div>
              <div style={{ fontSize: 16, fontWeight: 600 }}>No leads found</div>
              <div style={{ fontSize: 13, marginTop: 4 }}>Try adjusting your filters or search query.</div>
            </div>
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr style={{ background: "var(--surface)", borderBottom: "2px solid var(--border)" }}>
                  {["Address", "Owner Name", "Phone", "Source", "Score", "Date Found", "Status"].map((h) => (
                    <th key={h} style={{
                      padding: "10px 16px",
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
                {leads.map((lead, i) => (
                  <tr
                    key={lead.id}
                    onClick={() => setSelectedLead(lead)}
                    style={{
                      background: selectedLead?.id === lead.id
                        ? "rgba(59,130,246,0.08)"
                        : i % 2 === 0 ? "transparent" : "rgba(255,255,255,0.02)",
                      borderBottom: "1px solid var(--border)",
                      cursor: "pointer",
                      transition: "background 0.1s",
                    }}
                    onMouseEnter={(e) => {
                      if (selectedLead?.id !== lead.id)
                        (e.currentTarget as HTMLTableRowElement).style.background = "rgba(255,255,255,0.04)";
                    }}
                    onMouseLeave={(e) => {
                      if (selectedLead?.id !== lead.id)
                        (e.currentTarget as HTMLTableRowElement).style.background =
                          i % 2 === 0 ? "transparent" : "rgba(255,255,255,0.02)";
                    }}
                  >
                    <td style={{ padding: "12px 16px", maxWidth: 260 }}>
                      <div style={{ fontWeight: 500, color: "var(--text)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {lead.address}
                      </div>
                    </td>
                    <td style={{ padding: "12px 16px", color: "var(--text)", whiteSpace: "nowrap" }}>
                      {lead.owner_name ?? <span style={{ color: "var(--muted)" }}>—</span>}
                    </td>
                    <td style={{ padding: "12px 16px", color: "var(--muted)", whiteSpace: "nowrap", fontFamily: "monospace" }}>
                      {lead.phone ?? <span>—</span>}
                    </td>
                    <td style={{ padding: "12px 16px" }}>
                      <SourceBadge source={lead.source} />
                    </td>
                    <td style={{ padding: "12px 16px" }}>
                      <ScoreBadge score={lead.score} />
                    </td>
                    <td style={{ padding: "12px 16px", color: "var(--muted)", whiteSpace: "nowrap" }}>
                      {lead.created_at ? new Date(lead.created_at).toLocaleDateString("en-US", {
                        month: "short", day: "numeric", year: "numeric",
                      }) : "—"}
                    </td>
                    <td style={{ padding: "12px 16px" }}>
                      <StatusBadge
                        status={lead.status}
                        onChange={(s) => handleStatusChange(lead.id, s)}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {/* Side panel */}
        {selectedLead && (
          <LeadSidePanel
            lead={selectedLead}
            onClose={() => setSelectedLead(null)}
            onStatusChange={(s) => handleStatusChange(selectedLead.id, s)}
          />
        )}
      </div>
    </div>
  );
}
