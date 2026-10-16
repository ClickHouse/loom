import type { CSSProperties } from "react";
import { displayAgent } from "../lib/format";

/**
 * Small badge identifying which agent wrote a memory: a Click UI opaque badge (full radius,
 * a tint of the hue behind text of the same hue) in a hue fixed by the agent's name. The
 * lightness is `.agent-badge` in styles.css. Not Click UI's Badge: its states are a
 * fixed set and this hue is per agent.
 */
export function AgentBadge({ agent }: { agent: string }) {
  return (
    <span
      className="agent-badge inline-flex items-center gap-1 px-3 py-0.5 rounded-full text-xs font-medium border tabular"
      style={{ "--agent-hue": hueFor(agent) } as CSSProperties}
    >
      <span className="agent-badge-dot inline-block w-1.5 h-1.5 rounded-full" />
      {displayAgent(agent)}
    </span>
  );
}

// Deterministic hue from the agent name.
function hueFor(agent: string): number {
  let h = 0;
  for (let i = 0; i < agent.length; i++) h = (h * 31 + agent.charCodeAt(i)) >>> 0;
  return h % 360;
}
