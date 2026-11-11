import { cleanup, render, screen, waitFor, within } from "../../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry, NamespaceAuditResult, WhoAmI } from "../../../api";
import { useStore } from "../../../lib/state";
import { resetNamespaceRoleCache } from "../../../lib/namespaceRole";
import { InsightsHarness, resetInsights } from "../../../test/insights";

// The audit starter's card. Choosing "How is this namespace being used?" runs
// `namespace.audit` for the current org/namespace over the last 7 days (the window select
// reads again); the findings render critical → warning → info with the code humanised, a
// severity badge, count, share and the first sample (a key sample opens that memory in the
// drawer). Nothing runs until the starter is chosen.

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

vi.mock("../../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../api")>();
  return {
    ...actual,
    listMine: vi.fn(async () => []),
    isAuthDisabled: () => false,
    auditNamespace: vi.fn(async () => AUDIT),
  };
});
import { auditNamespace } from "../../../api";

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

const STARTER = "How is this namespace being used?";

/** Choose the audit starter; the card, once its read landed. */
async function openAudit() {
  render(<InsightsHarness />);
  await userEvent.click(await screen.findByRole("button", { name: STARTER }));
  await screen.findByTestId("audit-summary");
  return screen.getByTestId("assistant-card");
}

describe("Assistant: the audit card", () => {
  beforeEach(() => {
    resetInsights();
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
    resetInsights();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null, inspectedMemoryKey: null });
  });

  it("runs when chosen, not on mount, over 7 days and five samples, the window select reading again; findings sort critical → warning → info", async () => {
    render(<InsightsHarness />);
    await screen.findByRole("button", { name: STARTER });
    expect(auditNamespace).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: STARTER }));
    await waitFor(() => expect(auditNamespace).toHaveBeenCalledTimes(1));
    const [scope, opts] = vi.mocked(auditNamespace).mock.calls[0];
    expect(scope).toEqual({ org: "dev", namespace: "team-alerts" });
    expect(opts).toMatchObject({ windowSeconds: 7 * 86_400, sampleLimit: 5 });
    const panel = await screen.findByTestId("assistant-card");
    expect(within(panel).getByRole("heading", { name: "Audit" })).toBeInTheDocument();
    // The card's own provenance: Loom's read, not the model's.
    expect(await within(panel).findByTestId("assistant-card-provenance")).toHaveTextContent("Loom's own read");

    await userEvent.click(within(panel).getByLabelText("Audit window"));
    await userEvent.click(await screen.findByText("30 days"));
    await waitFor(() => expect(auditNamespace).toHaveBeenCalledTimes(2));
    expect(vi.mocked(auditNamespace).mock.calls[1][1]).toMatchObject({ windowSeconds: 30 * 86_400 });

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
    // Never an "optimize now" control (the agent applies), and the page that owns it.
    expect(within(panel).queryByRole("button", { name: /optimi[sz]e/i })).toBeNull();
    expect(within(panel).getByRole("button", { name: "Open in Memories" })).toBeInTheDocument();
  });

  it("a key sample opens that memory; the expandable row shows detail, remedy and every sample", async () => {
    await openAudit();
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
    await openAudit();
    expect(await screen.findByText("Nothing to flag in this window.")).toBeInTheDocument();
    expect(screen.getByTestId("audit-summary")).toHaveTextContent("Healthy");
    expect(screen.queryAllByTestId("audit-finding")).toHaveLength(0);
    expect(screen.queryByTestId("audit-skipped")).toBeNull();
  });

  it("a failed audit says so in the card, which offers another read", async () => {
    vi.mocked(auditNamespace).mockRejectedValueOnce(new Error("upstream timed out"));
    render(<InsightsHarness />);
    await userEvent.click(await screen.findByRole("button", { name: STARTER }));
    expect(await screen.findByTestId("assistant-card-refused")).toHaveTextContent("Couldn't read the audit");
    await userEvent.click(screen.getByRole("button", { name: "Read again" }));
    expect(await screen.findByTestId("audit-summary")).toBeInTheDocument();
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
    await openAudit();
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

  it("labels the deployment's state, and says how many procedure reads the ratio leaves out", async () => {
    const [churn] = AUDIT.findings;
    vi.mocked(auditNamespace).mockResolvedValueOnce({
      ...AUDIT,
      summary: { ...AUDIT.summary, typed_gets: 250 },
      findings: [
        {
          ...churn,
          code: "index_backlog",
          severity: "warning",
          kind: "system_state" as const,
          exceptions: "",
          uncertainty: "",
        },
      ],
    });
    await openAudit();
    const [backlog] = await screen.findAllByTestId("audit-finding");
    expect(screen.getByText(/250 procedure reads left out of the ratio/)).toBeInTheDocument();
    await userEvent.click(within(backlog).getByRole("button", { name: "Show details for Indexing backlog" }));
    expect(screen.getByTestId("audit-finding-kind")).toHaveTextContent(
      "The deployment's state, not the writer's doing",
    );
  });

  it("an unknown role on the namespace offers no read and sends nothing", async () => {
    useStore.setState({ myNamespaces: [] });
    render(<InsightsHarness />);
    const starter = await screen.findByRole("button", { name: STARTER });
    expect(starter).toHaveAttribute("aria-disabled", "true");
    expect(starter).toHaveAccessibleDescription("Your role on this namespace is not known.");
    await userEvent.click(starter);
    expect(screen.queryByTestId("assistant-card")).toBeNull();
    expect(auditNamespace).not.toHaveBeenCalled();
  });

  it("asks the assistant beside the card: the card is still read, and refused shows in its place", async () => {
    useStore.setState({ myNamespaces: [] });
    // The assistant can answer, but this person's role does not allow the audit read.
    render(<InsightsHarness cannotAnswer={null} />);
    expect(screen.getByRole("checkbox", { name: "Also ask the assistant" })).toBeChecked();
    expect(await screen.findByRole("button", { name: STARTER })).not.toHaveAttribute("aria-disabled");
  });
});
