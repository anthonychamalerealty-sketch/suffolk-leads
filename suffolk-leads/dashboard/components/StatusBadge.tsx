"use client";

import { useState } from "react";
import type { LeadStatus } from "@/lib/types";

const STATUS_CONFIG: Record<LeadStatus, { label: string; color: string; bg: string }> = {
  new:           { label: "New",           color: "#3b82f6", bg: "rgba(59,130,246,0.12)" },
  contacted:     { label: "Contacted",     color: "#f59e0b", bg: "rgba(245,158,11,0.12)" },
  qualified:     { label: "Qualified",     color: "#10b981", bg: "rgba(16,185,129,0.12)" },
  disqualified:  { label: "Disqualified",  color: "#6b7280", bg: "rgba(107,114,128,0.12)" },
  closed:        { label: "Closed",        color: "#8b5cf6", bg: "rgba(139,92,246,0.12)" },
};

const ALL_STATUSES: LeadStatus[] = ["new", "contacted", "qualified", "disqualified", "closed"];

interface Props {
  status: LeadStatus;
  onChange?: (status: LeadStatus) => void;
}

export default function StatusBadge({ status, onChange }: Props) {
  const [open, setOpen] = useState(false);
  const cfg = STATUS_CONFIG[status] ?? { label: status, color: "#6b7280", bg: "rgba(107,114,128,0.12)" };

  if (!onChange) {
    return (
      <span style={{
        display: "inline-flex", alignItems: "center",
        padding: "3px 8px", borderRadius: 12,
        background: cfg.bg, color: cfg.color,
        fontSize: 12, fontWeight: 600,
        border: `1px solid ${cfg.color}33`,
      }}>
        {cfg.label}
      </span>
    );
  }

  return (
    <div style={{ position: "relative", display: "inline-block" }}>
      <button
        onClick={(e) => { e.stopPropagation(); setOpen((o) => !o); }}
        style={{
          display: "inline-flex", alignItems: "center", gap: 4,
          padding: "3px 8px", borderRadius: 12,
          background: cfg.bg, color: cfg.color,
          fontSize: 12, fontWeight: 600,
          border: `1px solid ${cfg.color}33`,
          cursor: "pointer",
        }}
      >
        {cfg.label}
        <span style={{ fontSize: 9, opacity: 0.7 }}>▼</span>
      </button>
      {open && (
        <>
          <div
            style={{ position: "fixed", inset: 0, zIndex: 49 }}
            onClick={() => setOpen(false)}
          />
          <div style={{
            position: "absolute", top: "calc(100% + 4px)", left: 0,
            background: "var(--surface)",
            border: "1px solid var(--border)",
            borderRadius: 8, padding: 4,
            zIndex: 50, minWidth: 140,
            boxShadow: "0 8px 24px rgba(0,0,0,0.4)",
          }}>
            {ALL_STATUSES.map((s) => {
              const c = STATUS_CONFIG[s];
              return (
                <button
                  key={s}
                  onClick={(e) => {
                    e.stopPropagation();
                    onChange(s);
                    setOpen(false);
                  }}
                  style={{
                    display: "block", width: "100%",
                    padding: "6px 10px", textAlign: "left",
                    background: s === status ? c.bg : "transparent",
                    border: "none", borderRadius: 6,
                    color: c.color, fontSize: 12, fontWeight: 600,
                    cursor: "pointer",
                  }}
                >
                  {c.label}
                </button>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}
