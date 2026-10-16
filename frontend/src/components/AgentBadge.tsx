import { Badge } from "@clickhouse/click-ui/Badge";
import { displayAgent } from "../lib/format";

/** Which agent wrote a memory: Click UI's neutral Badge, as "Recalled by" draws the agents that read it. */
export function AgentBadge({ agent }: { agent: string }) {
  return <Badge text={displayAgent(agent)} state="neutral" size="sm" />;
}
