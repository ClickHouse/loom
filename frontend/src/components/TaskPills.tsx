import type { PolicyMode } from "../api/tasks";

/** The trust-table mode pill: label first, colour second. */
export function ModePill({ mode }: { mode: PolicyMode }) {
  const cls =
    mode === "auto"
      ? "text-success border-success/40"
      : mode === "deny"
        ? "text-danger border-danger/40"
        : "text-warn border-warn/40";
  return <span className={`inline-flex items-center text-2xs px-2 py-0.5 rounded-full border ${cls}`}>{mode}</span>;
}
