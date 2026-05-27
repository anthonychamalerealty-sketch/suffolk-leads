"use client";

import type { LeadSource } from "@/lib/types";

const SOURCE_CONFIG: Record<LeadSource, { label: string; emoji: string; color: string; bg: string }> = {
  fire:     { label: "Fire",     emoji: "🔥", color: "#ef4444", bg: "rgba(239,68,68,0.12)" },
  probate:  { label: "Probate",  emoji: "⚖️", color: "#f59e0b", bg: "rgba(245,158,11,0.12)" },
  obituary: { label: "Obituary", emoji: "📰", color: "#8b5cf6", bg: "rgba(139,92,246,0.12)" },
  social:   { label: "Social",   emoji: "📱", color: "#10b981", bg: "rgba(16,185,129,0.12)" },
};

export default function SourceBadge({ source }: { source: LeadSource }) {
  const cfg = SOURCE_CONFIG[source] ?? { label: source, emoji: "•", color: "#6b7280", bg: "rgba(107,114,128,0.12)" };
  return (
    <span style={{
      display: "inline-flex",
      alignItems: "center",
      gap: 4,
      padding: "3px 8px",
      borderRadius: 12,
      background: cfg.bg,
      color: cfg.color,
      fontSize: 12,
      fontWeight: 600,
      border: `1px solid ${cfg.color}33`,
      whiteSpace: "nowrap",
    }}>
      {cfg.emoji} {cfg.label}
    </span>
  );
}
