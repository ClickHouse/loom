import { cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryListEntry, MyNamespaceEntry, NamespaceAuditResult, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { setFoldOpen } from "../lib/folds";
import { Recent } from "./Recent";

// The Memories view carries a "Usage audit" panel. One click runs `namespace.audit` for
// the current org/namespace over the chosen window; the findings render critical →
// warning → info with the code humanised, a severity badge, count, share and the first
// sample (a key sample opens that memory in the inspector). Nothing runs on mount.

const entry = (memory_key: string): MemoryListEntry => ({
  memory_key,
  memory_type: "semantic",
  confidence: 1,
  version: 1,
  created_at: "2026-09-18T09:00:00Z",
  expires_at: null,
  pinned: false,
  tags: [],
  agent: "claude-desktop",
  value: `value of ${memory_key}`,
});

const AUDIT: NamespaceAuditResult = {
  org: "dev",
  namespace: "team-alerts",
  computed_at: "2026-09-19T08:00:00Z",
  window_seconds: 7 * 86_400,
  summary: {
    live_rows: 786,
    live_bytes: 21_000_000,
    excerpt_bytes: 900_000,
    version_rows: 3_100,
    version_bytes: 51_000_000,
    unembedded_rows: 179,
    gets: 6606,
    searches: 52,
    sets: 910,
    get_search_ratio: 127.0,
    inspector_calls: 12,
  },
  findings: [
    // Deliberately out of order: the panel sorts by severity.
    {
      code: "version_churn",
      severity: "info",
      count: 3100,
      share: 0.9,
      bytes: 51_000_000,
      samples: [{ memory_key: "sre/case/1", versions: 41 }],
      detail: "Version rows outnumber live rows 4×.",
      remedy: "Write less often; memory.update on change.",
      kind: "usage_heuristic" as const,
      rule_version: 1,
      source_op: "namespace.audit",
      window_seconds: null,
      population: 100,
      sample_limit: 5,
      samples_complete: false,
      evidence: [],
      exceptions: "",
      uncertainty: "A legitimate design matches too.",
    },
    {
      code: "oversized_values",
      severity: "warning",
      count: 3,
      share: 0.02,
      bytes: 3_000_000,
      samples: [
        { memory_key: "sre/case/1/notes", memory_id: "00000000-0000-0000-0000-000000000001", value_chars: 40_000 },
      ],
      detail: "Three values past the advisory.",
      remedy: "knowledge.upsert for documents.",
      kind: "usage_heuristic" as const,
      rule_version: 1,
      source_op: "namespace.audit",
      window_seconds: null,
      population: 100,
      sample_limit: 5,
      samples_complete: false,
      evidence: [],
      exceptions: "",
      uncertainty: "A legitimate design matches too.",
    },
    {
      code: "exact_key_store",
      severity: "critical",
      count: 6606,
      share: 0.992,
      bytes: 0,
      samples: [
        { memory_key: "sre/lease/main", reads: 116 },
        { memory_key: "sre/index/cases", reads: 40 },
      ],
      detail: "6606 gets against 52 searches.",
      remedy: "Use memory.search for recall; keep leases out of memory.",
      kind: "usage_heuristic" as const,
      rule_version: 1,
      source_op: "namespace.audit",
      window_seconds: 7 * 86_400,
      population: 100,
      sample_limit: 5,
      samples_complete: false,
      evidence: [],
      exceptions: "",
      uncertainty: "A legitimate design matches too.",
    },
  ],
  skipped: ["hand_rolled_index_rows: 24,000 rows exceed the 20k scan cap"],
  healthy: false,
  criticals: 1,
  warnings: 1,
  infos: 1,
};

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listMemories: vi.fn(async () => ({ entries: [entry("coffee_pref"), entry("tea_pref")], total: 2, today: 2 })),
    bulkGetMemories: vi.fn(async () => ({ results: {}, missing: [] })),
    listConflicts: vi.fn(async () => ({ rows: [] })),
    conflictKeys: vi.fn(async (): Promise<string[]> => []),
    listMine: vi.fn(async () => []),
    isAuthDisabled: () => false,
    auditNamespace: vi.fn(async () => AUDIT),
  };
});
import { auditNamespace, listMemories } from "../api";

const row = (namespace: string, role: MyNamespaceEntry["role"]): MyNamespaceEntry => ({
  namespace,
  role,
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
});

const owner: WhoAmI = {
  authenticated: true,
  subject: "o@example.com",
  org: "dev",
  roles: ["USER"],
  namespaces: ["team-alerts"],
};

// The panel lives on the Memories page's Insights tab (`?section=insights`), not above
// the list.
async function openMemories() {
  window.history.replaceState(null, "", "/memories?section=insights");
  render(<Recent />);
  await screen.findByTestId("usage-audit");
  expect(screen.getByRole("tab", { name: "Insights" })).toHaveAttribute("aria-selected", "true");
  // Insights reads no rows.
  expect(listMemories).not.toHaveBeenCalled();
}

describe("Memories: usage audit panel", () => {
  beforeEach(() => {
    // The panel starts folded; these tests are about what it shows once opened.
    setFoldOpen("usage-audit", true);
    vi.clearAllMocks();
    resetNamespaceRoleCache();
    useStore.setState({
      authed: true,
      whoami: owner,
      myNamespaces: [row("team-alerts", "admin")],
      identity: { org: "dev", namespace: "team-alerts", agent: "loom-ui" },
      view: "memories",
      inspectedMemoryKey: null,
    });
  });
  afterEach(() => {
    cleanup();
    window.history.replaceState(null, "", "/");
    useStore.setState({ whoami: null, myNamespaces: null, identity: null, inspectedMemoryKey: null });
  });

  it("runs on a click, not on mount, with the window and five samples; findings sort critical → warning → info", async () => {
    await openMemories();
    const panel = screen.getByTestId("usage-audit");
    expect(within(panel).getByRole("heading", { name: "Usage audit" })).toBeInTheDocument();
    expect(auditNamespace).not.toHaveBeenCalled();

    await userEvent.click(within(panel).getByLabelText("Audit window"));
    await userEvent.click(await screen.findByText("30 days"));
    await userEvent.click(within(panel).getByRole("button", { name: /Run audit/ }));

    await waitFor(() => expect(auditNamespace).toHaveBeenCalledTimes(1));
    const [scope, opts] = vi.mocked(auditNamespace).mock.calls[0];
    expect(scope).toEqual({ org: "dev", namespace: "team-alerts" });
    expect(opts).toMatchObject({ windowSeconds: 30 * 86_400, sampleLimit: 5 });

    // Summary line.
    const summary = await screen.findByTestId("audit-summary");
    expect(summary).toHaveTextContent("Needs attention");
    expect(summary).toHaveTextContent("1 critical, 1 warning, 1 info");
    expect(summary).toHaveTextContent("live 786 rows");
    expect(summary).toHaveTextContent("gets 6,606 / searches 52 / sets 910");
    expect(summary).toHaveTextContent("ratio 127");
    expect(summary).toHaveTextContent("12 dashboard and assistant calls not counted");

    // Sorted by severity, humanised codes, share as a percentage.
    const rows = screen.getAllByTestId("audit-finding");
    expect(rows.map((r) => r.getAttribute("data-severity"))).toEqual(["critical", "warning", "info"]);
    expect(rows.map((r) => r.getAttribute("data-code"))).toEqual([
      "exact_key_store",
      "oversized_values",
      "version_churn",
    ]);
    expect(within(rows[0]).getByText("Exact-key store")).toBeInTheDocument();
    expect(within(rows[0]).getByText("99%")).toBeInTheDocument();
    expect(within(rows[0]).getByText("6,606")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Oversized values")).toBeInTheDocument();

    // The skipped check is a muted note; the remedy points at the recipe.
    expect(screen.getByTestId("audit-skipped")).toHaveTextContent(
      "hand_rolled_index_rows: 24,000 rows exceed the 20k scan cap",
    );
    expect(panel).toHaveTextContent("run loom-optimize with your agent");
    expect(panel).toHaveTextContent("docs/guides/optimize-namespace.md");
    // Never an "optimize now" control (the agent applies).
    expect(within(panel).queryByRole("button", { name: /optimi[sz]e/i })).toBeNull();
  });

  it("a key sample opens that memory; the expandable row shows detail, remedy and every sample", async () => {
    await openMemories();
    await userEvent.click(screen.getByRole("button", { name: /Run audit/ }));
    const rows = await screen.findAllByTestId("audit-finding");

    await userEvent.click(within(rows[0]).getByRole("button", { name: "Open memory sre/lease/main" }));
    expect(useStore.getState().inspectedMemoryKey).toBe("sre/lease/main");

    expect(screen.queryByTestId("audit-finding-detail")).toBeNull();
    const toggle = within(rows[0]).getByRole("button", { name: "Show details for Exact-key store" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(toggle);
    const detail = await screen.findByTestId("audit-finding-detail");
    expect(detail).toHaveTextContent("6606 gets against 52 searches.");
    expect(detail).toHaveTextContent("Remedy: Use memory.search for recall; keep leases out of memory.");
    expect(within(detail).getByRole("button", { name: "Open memory sre/lease/main" })).toBeInTheDocument();
    expect(within(detail).getByRole("button", { name: "Open memory sre/index/cases" })).toBeInTheDocument();
    expect(detail).toHaveTextContent("reads 116");
    expect(detail).toHaveTextContent("not every match is listed");
    expect(within(detail).getByTestId("audit-finding-kind")).toHaveTextContent(
      "Usage heuristic: a hit is not proof of misuse · rule v1 · May still be fine: A legitimate design matches too.",
    );
    expect(within(rows[0]).getByRole("button", { name: "Hide details for Exact-key store" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );

    // A sample's memory_id joins it to its evidence entry; the detail does not print it.
    await userEvent.click(within(rows[0]).getByRole("button", { name: "Hide details for Exact-key store" }));
    await userEvent.click(within(rows[1]).getByRole("button", { name: /Show details for/ }));
    const oversized = await screen.findByTestId("audit-finding-detail");
    expect(oversized).toHaveTextContent("value_chars 40000");
    expect(oversized).not.toHaveTextContent("00000000-0000-0000-0000-000000000001");
  });

  it("no findings → the empty copy; a healthy summary", async () => {
    vi.mocked(auditNamespace).mockResolvedValueOnce({
      ...AUDIT,
      findings: [],
      skipped: [],
      healthy: true,
      criticals: 0,
      warnings: 0,
      infos: 0,
    });
    await openMemories();
    await userEvent.click(screen.getByRole("button", { name: /Run audit/ }));
    expect(await screen.findByText("Nothing to flag in this window.")).toBeInTheDocument();
    expect(screen.getByTestId("audit-summary")).toHaveTextContent("Healthy");
    expect(screen.queryAllByTestId("audit-finding")).toHaveLength(0);
    expect(screen.queryByTestId("audit-skipped")).toBeNull();
  });

  it("a failed audit is an inline error, and the button offers another run", async () => {
    vi.mocked(auditNamespace).mockRejectedValueOnce(new Error("upstream timed out"));
    await openMemories();
    await userEvent.click(screen.getByRole("button", { name: /Run audit/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Audit failed");
    expect(screen.getByRole("button", { name: /Run audit/ })).toBeEnabled();
  });

  it("joins the kind line with separators, and an older server's finding shows none of it", async () => {
    const [churn, oversized, exact] = AUDIT.findings;
    vi.mocked(auditNamespace).mockResolvedValueOnce({
      ...AUDIT,
      findings: [
        { ...exact, kind: "contract_violation", uncertainty: "", exceptions: "Non-searchable types are not checked." },
        // A finding from a server one release older carries none of the evidence fields.
        {
          code: oversized.code,
          severity: oversized.severity,
          count: oversized.count,
          share: oversized.share,
          bytes: oversized.bytes,
          samples: oversized.samples,
          detail: oversized.detail,
          remedy: oversized.remedy,
        },
        churn,
      ],
    });
    await openMemories();
    await userEvent.click(screen.getByRole("button", { name: /Run audit/ }));
    const rows = await screen.findAllByTestId("audit-finding");

    await userEvent.click(within(rows[0]).getByRole("button", { name: "Show details for Exact-key store" }));
    expect(screen.getByTestId("audit-finding-kind")).toHaveTextContent(
      "Contract violation · rule v1 · Scope: Non-searchable types are not checked.",
    );
    await userEvent.click(within(rows[0]).getByRole("button", { name: "Hide details for Exact-key store" }));

    await userEvent.click(within(rows[1]).getByRole("button", { name: /Show details for/ }));
    const detail = await screen.findByTestId("audit-finding-detail");
    expect(within(detail).queryByTestId("audit-finding-kind")).toBeNull();
    expect(detail).not.toHaveTextContent("not every match is listed");
  });

  it("an unknown role on the namespace shows no panel and sends nothing", async () => {
    useStore.setState({ myNamespaces: [] });
    window.history.replaceState(null, "", "/memories?section=insights");
    render(<Recent />);
    await screen.findByRole("tab", { name: "Insights" });
    expect(screen.queryByTestId("usage-audit")).toBeNull();
    expect(auditNamespace).not.toHaveBeenCalled();
  });
});
