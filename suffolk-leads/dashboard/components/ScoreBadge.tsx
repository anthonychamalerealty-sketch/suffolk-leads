"use client";

function getScoreColor(score: number): { color: string; bg: string } {
  if (score >= 8) return { color: "#10b981", bg: "rgba(16,185,129,0.12)" };
  if (score >= 6) return { color: "#f59e0b", bg: "rgba(245,158,11,0.12)" };
  return { color: "#ef4444", bg: "rgba(239,68,68,0.12)" };
}

export default function ScoreBadge({ score }: { score: number }) {
  const { color, bg } = getScoreColor(score);
  const pct = Math.min(100, Math.max(0, (score / 10) * 100));

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
      <span style={{
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        width: 34,
        height: 22,
        borderRadius: 6,
        background: bg,
        color,
        fontSize: 12,
        fontWeight: 700,
        border: `1px solid ${color}33`,
      }}>
        {score.toFixed(1)}
      </span>
      <div style={{ width: 40, height: 4, background: "var(--border)", borderRadius: 2, overflow: "hidden" }}>
        <div style={{ width: `${pct}%`, height: "100%", background: color, borderRadius: 2 }} />
      </div>
    </div>
  );
}
