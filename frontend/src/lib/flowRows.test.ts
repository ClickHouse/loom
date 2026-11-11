import { describe, expect, it } from "vitest";
import type { SharingSummary } from "../api";
import { sumByAgent, toolActivityRows } from "./flowRows";

// The table follows the page's namespace; a namespace outside the caller's grants is
// named as such instead of rendering as an empty table.

const ns = (namespace: string) => ({
  namespace,
  role: "admin",
  visibility: "private",
  member_count: 1,
  writes_7d: 0,
  recalls_7d: 0,
});
const row = (agent: string, namespace: string, writes: number, recalls: number, cross = 0) => ({
  agent,
  namespace,
  writes,
  recalls,
  cross_namespace_recalls: cross,
});

const summary: SharingSummary = {
  subject: "me",
  org: "o",
  window_days: 7,
  totals: { writes: 0, recalls: 0, cross_namespace_recalls: 0, namespaces: 2, agents: 3 },
  agents: [],
  namespaces: [ns("code-clickhouse"), ns("team")],
  agent_namespaces: [
    row("-", "code-clickhouse", 0, 44000),
    row("f1-drive", "code-clickhouse", 0, 3168),
    row("claude-code", "team", 12, 30, 4),
    row("claude-code", "code-clickhouse", 0, 5),
  ],
};

describe("toolActivityRows", () => {
  it("shows only the selected namespace and counts the others", () => {
    const r = toolActivityRows(summary, "team", false);
    expect(r.kind).toBe("namespace");
    if (r.kind !== "namespace") return;
    expect(r.rows).toEqual([{ agent: "claude-code", writes: 12, recalls: 30, cross_namespace_recalls: 4 }]);
    expect(r.otherPlaces).toHaveLength(1);
  });
  it("names a namespace outside the summary's scope and falls back to everything", () => {
    const r = toolActivityRows(summary, "elsewhere", false);
    expect(r.kind).toBe("outside");
    if (r.kind !== "outside") return;
    expect(r.namespace).toBe("elsewhere");
    expect(r.rows.length).toBe(4);
    expect(r.places).toHaveLength(2);
  });
  it("shows every agent × namespace row on request, most active first", () => {
    const r = toolActivityRows(summary, "team", true);
    expect(r.kind).toBe("all");
    if (r.kind !== "all") return;
    expect(r.rows[0]).toMatchObject({ agent: "-", namespace: "code-clickhouse" });
    expect(r.rows.map((x) => x.agent)).toEqual(["-", "f1-drive", "claude-code", "claude-code"]);
  });
});

describe("sumByAgent", () => {
  it("sums an agent across namespaces", () => {
    expect(sumByAgent(summary.agent_namespaces).find((a) => a.agent === "claude-code")).toEqual({
      agent: "claude-code",
      writes: 12,
      recalls: 35,
      cross_namespace_recalls: 4,
    });
  });
});
