import { Badge } from "@clickhouse/click-ui/Badge";
import type { PolicyMode } from "../api/tasks";

/** The trust-table mode pill: label first, colour second. */
export function ModePill({ mode }: { mode: PolicyMode }) {
  const state = mode === "auto" ? "success" : mode === "deny" ? "danger" : "warning";
  return <Badge text={mode} state={state} size="sm" />;
}
