import { displayAgent } from "../lib/format";

/** Small badge identifying which agent wrote a memory. */
export function AgentBadge({ agent }: { agent: string }) {
  const palette = colorFor(agent);
  return (
    <span
      className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md text-xs font-medium border tabular"
      style={{
        background: palette.bg,
        color: palette.fg,
        borderColor: palette.border,
      }}
    >
      <span className="inline-block w-1.5 h-1.5 rounded-full" style={{ background: palette.dot }} />
      {displayAgent(agent)}
    </span>
  );
}

function colorFor(agent: string): { bg: string; fg: string; border: string; dot: string } {
  // Deterministic hue from agent name
  let h = 0;
  for (let i = 0; i < agent.length; i++) h = (h * 31 + agent.charCodeAt(i)) >>> 0;
  const hue = h % 360;
  return {
    bg: `oklch(0.97 0.02 ${hue})`,
    fg: `oklch(0.40 0.10 ${hue})`,
    border: `oklch(0.85 0.05 ${hue})`,
    dot: `oklch(0.55 0.18 ${hue})`,
  };
}
