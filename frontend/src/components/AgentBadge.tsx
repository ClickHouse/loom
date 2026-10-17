import { useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { displayAgent } from "../lib/format";
import { useClamped } from "./ui/TextClamp";
import { inRowOrControl } from "./ui/TipTarget";
import { Tip } from "./ui/Tooltip";

/**
 * Which agent wrote a memory: Click UI's neutral Badge, as "Recalled by" draws the agents
 * that read it. At most 180 px with an ellipsis (styles.css `.agent-badge`); a cut name is
 * whole in a tooltip. In a list row or inside a control the badge takes no tab stop
 * (`inRowOrControl`) and the tooltip opens on hover; elsewhere it is focusable.
 */
export function AgentBadge({ agent }: { agent: string }) {
  const name = displayAgent(agent);
  const [text, setText] = useState<HTMLElement | null>(null);
  const [box, setBox] = useState<HTMLElement | null>(null);
  // Badge's own ellipsis measures once, at mount; a row laid out later (content-visibility)
  // would never get its tooltip. This one is re-measured as the box resizes.
  const cut = useClamped(text, name);
  const focusable = cut && !!box && !inRowOrControl(box);
  return (
    <Tip content={name} disabled={!cut}>
      <span
        ref={setBox}
        tabIndex={focusable ? 0 : undefined}
        role={focusable ? "note" : undefined}
        className={`agent-badge inline-flex min-w-0 rounded ${cut && !focusable ? "relative z-[1]" : ""}`}
      >
        <Badge
          text={
            <span ref={setText} className="block truncate">
              {name}
            </span>
          }
          ellipsisContent={false}
          state="neutral"
          size="sm"
        />
      </span>
    </Tip>
  );
}
