"use client";

import type { Lead, LeadStatus } from "@/lib/types";
import SourceBadge from "./SourceBadge";
import StatusBadge from "./StatusBadge";
import ScoreBadge from "./ScoreBadge";

interface Props {
  lead: Lead;
  onClose: () => void;
  onStatusChange: (status: LeadStatus) => void;
}

function InfoRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ fontSize: 11, color: "var(--muted)", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 3 }}>
        {label}
      </div>
      <div style={{ fontSize: 13, color: "var(--text)", wordBreak: "break-word" }}>
        {value ?? <span style={{ color: "var(--muted)" }}>Not available</span>}
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 24 }}>
      <div style={{
        fontSize: 11, fontWeight: 700, color: "var(--muted)",
        textTransform: "uppercase", letterSpacing: "0.08em",
        marginBottom: 12, paddingBottom: 6,
        borderBottom: "1px solid var(--border)",
      }}>
        {title}
      </div>
      {children}
    </div>
  );
}

export default function LeadSidePanel({ lead, onClose, onStatusChange }: Props) {
  const mapsUrl = `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(lead.address)}`;

  let rawParsed: Record<string, unknown> | null = null;
  try {
    rawParsed = JSON.parse(lead.raw_data);
  } catch {
    rawParsed = null;
  }

  const sourceLabels: Record<string, string> = {
    fire: "Fire Report",
    probate: "Probate Filing",
    obituary: "Obituary",
    social: "Social Signal",
  };

  return (
    <div style={{
      width: 380,
      minWidth: 340,
      background: "var(--surface)",
      borderLeft: "1px solid var(--border)",
      display: "flex",
      flexDirection: "column",
      overflow: "hidden",
      position: "sticky",
      top: 0,
      height: "calc(100vh - 109px)",
    }}>
      {/* Panel header */}
      <div style={{
        padding: "16px 20px",
        borderBottom: "1px solid var(--border)",
        display: "flex",
        alignItems: "flex-start",
        justifyContent: "space-between",
        gap: 12,
      }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text)", marginBottom: 6, lineHeight: 1.4 }}>
            {lead.address}
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <SourceBadge source={lead.source} />
            <ScoreBadge score={lead.score} />
          </div>
        </div>
        <button
          onClick={onClose}
          style={{
            background: "transparent",
            border: "none",
            color: "var(--muted)",
            cursor: "pointer",
            fontSize: 18,
            padding: 4,
            lineHeight: 1,
            flexShrink: 0,
          }}
          title="Close"
        >✕</button>
      </div>

      {/* Scrollable body */}
      <div style={{ flex: 1, overflow: "auto", padding: "20px" }}>

        {/* Status */}
        <div style={{ marginBottom: 20, display: "flex", alignItems: "center", gap: 12 }}>
          <span style={{ fontSize: 12, color: "var(--muted)" }}>Status:</span>
          <StatusBadge status={lead.status} onChange={onStatusChange} />
        </div>

        {/* Google Maps */}
        <a
          href={mapsUrl}
          target="_blank"
          rel="noopener noreferrer"
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "10px 14px",
            background: "rgba(59,130,246,0.1)",
            border: "1px solid rgba(59,130,246,0.3)",
            borderRadius: 8,
            color: "var(--accent)",
            textDecoration: "none",
            fontSize: 13,
            fontWeight: 600,
            marginBottom: 20,
            transition: "background 0.15s",
          }}
        >
          <span style={{ fontSize: 16 }}>📍</span>
          View on Google Maps
          <span style={{ marginLeft: "auto", fontSize: 11, opacity: 0.7 }}>↗</span>
        </a>

        {/* Contact Info */}
        <Section title="Contact Information">
          <InfoRow label="Owner Name" value={lead.owner_name} />
          <InfoRow label="Phone Number" value={
            lead.phone ? (
              <a href={`tel:${lead.phone}`} style={{ color: "var(--accent)", textDecoration: "none" }}>
                {lead.phone}
              </a>
            ) : null
          } />
          <InfoRow label="Email" value={
            lead.email ? (
              <a href={`mailto:${lead.email}`} style={{ color: "var(--accent)", textDecoration: "none" }}>
                {lead.email}
              </a>
            ) : null
          } />
          <InfoRow label="Mailing Address" value={lead.owner_mailing_address} />
        </Section>

        {/* Property Info */}
        <Section title="Property Details">
          <InfoRow label="Parcel ID" value={lead.parcel_id} />
          <InfoRow label="State" value={
            lead.state ? (
              <span style={{
                display: "inline-flex", alignItems: "center", gap: 4,
                padding: "2px 8px", borderRadius: 12, fontSize: 12, fontWeight: 600,
                background: lead.state === "GA" ? "rgba(16,185,129,0.12)" : "rgba(59,130,246,0.12)",
                color: lead.state === "GA" ? "#10b981" : "#3b82f6",
              }}>
                {lead.state === "GA" ? "🍑" : "🗽"} {lead.state === "GA" ? "Georgia" : "New York"}
              </span>
            ) : null
          } />
          <InfoRow label="County" value={lead.county} />
          <InfoRow label="Assessed Value" value={
            lead.assessed_value
              ? `$${lead.assessed_value.toLocaleString("en-US")}`
              : null
          } />
          <InfoRow label="Last Sale Date" value={
            lead.last_sale_date
              ? new Date(lead.last_sale_date).toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" })
              : null
          } />
          <InfoRow label="Property Class" value={lead.property_class_code} />
        </Section>

        {/* Lead Metadata */}
        <Section title="Lead Metadata">
          <InfoRow label="Lead ID" value={`#${lead.id}`} />
          <InfoRow label="Source Type" value={sourceLabels[lead.source] ?? lead.source} />
          <InfoRow label="Date Found" value={
            lead.created_at
              ? new Date(lead.created_at).toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", year: "numeric" })
              : null
          } />
        </Section>

        {/* Raw Source Data */}
        <Section title={`Raw Source Data — ${sourceLabels[lead.source] ?? lead.source}`}>
          {rawParsed ? (
            <div style={{
              background: "var(--surface2)",
              border: "1px solid var(--border)",
              borderRadius: 8,
              padding: 12,
              fontSize: 12,
              fontFamily: "monospace",
            }}>
              {Object.entries(rawParsed).map(([k, v]) => (
                <div key={k} style={{ marginBottom: 6, display: "flex", gap: 8 }}>
                  <span style={{ color: "var(--muted)", minWidth: 120, flexShrink: 0 }}>{k}:</span>
                  <span style={{ color: "var(--text)", wordBreak: "break-all" }}>{String(v)}</span>
                </div>
              ))}
            </div>
          ) : (
            <div style={{
              background: "var(--surface2)",
              border: "1px solid var(--border)",
              borderRadius: 8,
              padding: 12,
              fontSize: 12,
              fontFamily: "monospace",
              color: "var(--text)",
              whiteSpace: "pre-wrap",
              wordBreak: "break-all",
            }}>
              {lead.raw_data || "No raw data available"}
            </div>
          )}
        </Section>
      </div>
    </div>
  );
}
