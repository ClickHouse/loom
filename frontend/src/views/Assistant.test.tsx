import { act, cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  InspectRow,
  InspectView,
  MemoryInspection,
  MyNamespaceEntry,
  NamespaceInventory,
  NamespaceTune,
  TuneProposal,
  WhoAmI,
} from "../api";
import { ApiError } from "../lib/errors";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { axeViolations } from "../test/axe";
import { Assistant } from "./Assistant";

// A row version is a nanosecond stamp (server/memory/store/rows.py), past what a JavaScript
// number holds exactly: the page never prints it.
const NS_VERSION = Number("1762156800123456789");

// The Assistant page reads only on a click, through three read-only ops, and never
// applies a draft: it renders what namespace.inventory, memory.inspect and namespace.tune
// answer, with what they could not read said so. The fixtures are the shapes the server
// sends.

const INVENTORY: NamespaceInventory = {
  scope: { org: "dev", namespace: "team", read_scope: "namespace", user_id: null, session_id: null },
  sections: [
    { plane: "memories", count: 42, latest_at: "2026-11-01T10:00:00", complete: true, reason: "" },
    { plane: "records", count: null, latest_at: null, complete: false, reason: "read failed: TimeoutError" },
    { plane: "documents", count: 3, latest_at: null, complete: true, reason: "" },
    { plane: "runs", count: 0, latest_at: null, complete: true, reason: "" },
    { plane: "integrations", count: 1, latest_at: null, complete: true, reason: "" },
  ],
  populations: [
    {
      population: "members",
      total: 2,
      top: [],
      complete: false,
      reason: "shared with everyone, a group or an agent: those members are not counted",
    },
    { population: "agents", total: 1, top: [{ name: "claude-code", rows: 40 }], complete: true, reason: "" },
    { population: "authors", total: 1, top: [{ name: "human:o@example.com", rows: 42 }], complete: true, reason: "" },
    { population: "subjects", total: 1, top: [{ name: "ada", rows: 12 }], complete: true, reason: "" },
  ],
  capabilities: [
    { name: "llm", enabled: false },
    { name: "embedder", enabled: true },
  ],
  computed_at: "2026-11-01T10:00:00",
};

const ROW: InspectRow = {
  memory_key: "billing/invoices",
  memory_id: "id-billing",
  version: NS_VERSION,
  content_sha256: "ab12",
  memory_type: "semantic",
  status: "active",
  source_class: "direct",
  agent: "claude-code",
  user_id: "ada",
  session_id: "-",
  actor: "human:o@example.com",
  updated_at: "2026-11-01T10:00:00",
  sensitivity: "normal",
  versions: 3,
  excerpt: "Invoices go out on Fridays",
};

// server/memory/app/inspect.py _DEFINITIONS["user"].
const OWN_USER =
  "Live rows filed under this user_id, whoever wrote them: what a token pinned to this user (read_scope own_user) reads. Promoted rows (memory.promote) are not counted.";
const OTHERS_USER =
  "Every other live row of the namespace. A member reads these too; a token pinned to this user does not. Promoted rows (memory.promote) are not counted.";

// live_rows is the sum of the same count rows by_type and by_status come from.
const view = (definition: string, rows: InspectRow[], liveRows = rows.length): InspectView => ({
  definition,
  live_rows: liveRows,
  by_type: liveRows ? [{ name: "semantic", rows: liveRows }] : [],
  by_status: liveRows ? [{ name: "active", rows: liveRows }] : [],
  by_source_class: [],
  top_tags: [],
  categories: [],
  first_updated_at: null,
  last_updated_at: null,
  newest: rows,
  complete: true,
});

const inspection = (subject: MemoryInspection["subject"], id: string): MemoryInspection => ({
  org: "dev",
  namespace: "team",
  subject,
  id,
  computed_at: "2026-11-01T10:00:00",
  own: view(OWN_USER, [ROW]),
  others: view(OTHERS_USER, []),
  activity: { window_seconds: 7 * 86_400, calls: [], first_at: null, last_at: null, gap: "" },
  settings: {
    guide_revision: 2,
    guide_summary: "key layout",
    categories_registered: 1,
    extraction_policy: null,
    extraction_policy_unavailable: "only an org admin reads the operator extraction policy",
    extraction_instructions:
      "Per request only: memory.set_from_messages takes `extraction_instructions`; no user, agent or session setting stores them.",
  },
  skipped: [],
});

const CITED = {
  memory_key: "billing/invoices",
  memory_id: "id-billing",
  version: NS_VERSION,
  content_sha256: "ab12",
  agent: "claude-code",
  user_id: "ada",
  session_id: "-",
  memory_type: "semantic",
  source_class: "direct",
  updated_at: "2026-11-01T10:00:00",
};

// A category draft cites the tagged rows and counts those another category holds.
const CATEGORY: TuneProposal = {
  id: "2ef4b21f4c7eb774",
  target: "namespace.category",
  scope: "namespace dev/team",
  title: "Register a category for the tag `billing`",
  rationale:
    "12 live rows carry the tag `billing` and no category is named after it; 4 of them are filed under another category. A registered category is what namespace.search_categories finds, and extraction offers the first 32 by name as tags for new rows.",
  current: "",
  current_revision: null,
  proposed: "billing",
  evidence: { finding_codes: [], supporting_count: 12, supporting: [CITED], counter_count: 4 },
  affected_agents: ["claude-code"],
  affected_agents_total: 3,
  effect: "future",
  historical:
    "Registering files no row: rows already tagged join the category only through memory.assign_category, a separate action per row.",
  apply: {
    op: "namespace.register_category",
    args: { org: "dev", namespace: "team", name: "billing", description: "Rows tagged `billing`." },
    args_complete: true,
    required_role: "writer",
    guard:
      "None: registering a name that exists updates that category, so read namespace.list_categories again just before applying.",
  },
  limits: "A tag used for two meanings would need two categories, not one.",
};

// _guide_lines appends the prefixes an existing guide leaves out.
const GUIDE_CURRENT = "# team\n\n## Key layout\n\n- `notes/…`: 9 rows\n";
const GUIDE_PROPOSED = `${GUIDE_CURRENT}\n## Key layout\n\n- \`billing/…\`: 12 rows\n`;

// A guide draft cites no row and counts none; here the writers could not be read.
const GUIDE: TuneProposal = {
  id: "12b965a8ed32f1da",
  target: "namespace.guide",
  scope: "namespace dev/team",
  title: "Name the key prefixes the guide leaves out",
  rationale:
    "1 key prefixes holding rows are not named in the guide. A writer reads the guide before its first write, so it names the layout new rows should follow; the counts are today's live rows.",
  current: GUIDE_CURRENT,
  current_revision: 2,
  proposed: GUIDE_PROPOSED,
  evidence: { finding_codes: [], supporting_count: 12, supporting: [], counter_count: null },
  affected_agents: [],
  affected_agents_total: null,
  effect: "future",
  historical: "A guide changes no stored row; it shapes the writes that read it.",
  apply: {
    op: "namespace.guide_set",
    args: {
      org: "dev",
      namespace: "team",
      content: GUIDE_PROPOSED,
      expected_revision: 2,
      note: "Drafted by namespace.tune from the key layout and tags.",
    },
    args_complete: true,
    required_role: "writer",
    guard:
      "expected_revision: a guide written since the draft refuses the write and answers with its current text to merge into.",
  },
  limits: "The draft lists what the rows show, not what they mean; a person writes what each prefix and tag is for.",
};

// The extraction draft answers audit findings and reaches the agents extraction wrote for.
const INSTRUCTIONS: TuneProposal = {
  id: "67a699bd202bbfee",
  target: "extraction_instructions.request",
  scope: "each memory.set_from_messages request into dev/team that sends them",
  title: "Send extraction instructions for the shapes the audit found",
  rationale:
    "The audit flagged episodic_blobs: 5 of 30 rows (counts over every writer). Extraction wrote 30 of the live rows here; 1 of the audit's cited rows came from it. No user, agent or session setting stores instructions, so a writer sends them per request.",
  current: "",
  current_revision: null,
  proposed: "- Keep an episode to a short summary; a run's full output belongs in context.offload.",
  evidence: { finding_codes: ["episodic_blobs"], supporting_count: 5, supporting: [CITED], counter_count: 25 },
  affected_agents: ["claude-code"],
  affected_agents_total: 1,
  effect: "future",
  historical:
    "Rows already stored keep their shape. Reworking them is the loom-optimize procedure (memory.move per row, keeping a redirect), a separate action with its own approval.",
  apply: {
    op: "memory.set_from_messages",
    args: {
      extraction_instructions: "- Keep an episode to a short summary; a run's full output belongs in context.offload.",
    },
    args_complete: false,
    required_role: "writer",
    guard: "None needed: the instructions apply only to the request that carries them.",
  },
  limits:
    "Each writer has to send them; a request without them extracts as before. The flagged counts include rows written by hand (memory.set), which never pass through extraction; a row counts as extraction's by its source trace. They are not drafted as the operator extraction policy: a policy overrides every other instruction and turns off the recovery passes that keep preferences, costs, ages and corrections.",
};

const TUNE: NamespaceTune = {
  org: "dev",
  namespace: "team",
  computed_at: "2026-11-01T10:00:00",
  proposals: [CATEGORY, GUIDE, INSTRUCTIONS],
  guide_revision: 2,
  categories_registered: 1,
  extraction_policy: null,
  extraction_policy_unavailable: "only an org admin reads the operator extraction policy",
  skipped: ["audit.telemetry_unavailable: window within the ledger lag"],
  model_used: false,
};

const namespaceInventory = vi.fn(async () => INVENTORY);
const inspectMemories = vi.fn(async (_s: unknown, subject: MemoryInspection["subject"], id: string) =>
  inspection(subject, id),
);
const tuneNamespace = vi.fn(async () => TUNE);
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listMine: vi.fn(async () => []),
    isAuthDisabled: () => false,
    namespaceInventory: (...a: unknown[]) => namespaceInventory(...(a as [])),
    inspectMemories: (...a: [unknown, MemoryInspection["subject"], string]) => inspectMemories(...a),
    tuneNamespace: (...a: unknown[]) => tuneNamespace(...(a as [])),
  };
});

const row = (namespace: string, role: MyNamespaceEntry["role"]): MyNamespaceEntry => ({
  namespace,
  role,
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
});

const viewer: WhoAmI = {
  authenticated: true,
  subject: "o@example.com",
  org: "dev",
  roles: ["USER"],
  namespaces: ["team", "other"],
};

beforeEach(() => {
  vi.clearAllMocks();
  resetNamespaceRoleCache();
  useStore.setState({
    authed: true,
    whoami: viewer,
    myNamespaces: [row("team", "viewer"), row("other", "viewer")],
    identity: { org: "dev", namespace: "team", agent: "loom-ui" },
  });
});
afterEach(() => {
  cleanup();
  useStore.setState({ identity: null, whoami: null, myNamespaces: null, inspectedMemoryKey: null, inspectedAgent: "" });
});

const switchNamespace = () =>
  act(() => {
    useStore.setState({ identity: { org: "dev", namespace: "other", agent: "loom-ui" } });
  });

describe("Assistant: starters and scope", () => {
  it("explains itself, offers the starters and reads nothing on mount", async () => {
    const { container } = render(<Assistant />);
    expect(screen.getByRole("heading", { level: 1, name: "Assistant" })).toBeInTheDocument();
    for (const name of [
      "Show relationships between facts",
      "What is in this project?",
      "Analyze one user's memories",
      "Who are the users in this project?",
      "Configure this project",
    ])
      expect(screen.getByRole("button", { name })).toBeInTheDocument();
    expect(screen.getByText(/No drafts yet/)).toBeInTheDocument();
    expect(namespaceInventory).not.toHaveBeenCalled();
    expect(inspectMemories).not.toHaveBeenCalled();
    expect(tuneNamespace).not.toHaveBeenCalled();
    expect(await axeViolations(container)).toEqual([]);
  });

  it("offers no read to a caller whose role on the namespace is unknown", async () => {
    useStore.setState({ myNamespaces: [] });
    render(<Assistant />);
    const starter = screen.getByRole("button", { name: "Configure this project" });
    expect(starter).toHaveAttribute("aria-disabled", "true");
    expect(starter).toHaveAccessibleDescription("Your role on this namespace is not known.");
    await userEvent.click(starter);
    expect(tuneNamespace).not.toHaveBeenCalled();
  });
});

describe("Assistant: relationships", () => {
  it("opens the graph only from an entity", async () => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Show relationships between facts" }));
    const open = screen.getByRole("button", { name: "Open the graph" });
    expect(open).toHaveAccessibleDescription("Enter an entity to walk from.");
    await userEvent.type(screen.getByLabelText("Entity"), "invoices");
    expect(screen.getByRole("button", { name: "Open the graph" })).not.toHaveAttribute("aria-disabled");
    await userEvent.click(screen.getByRole("button", { name: "Open the graph" }));
    expect(useStore.getState().view).toBe("graph");
  });
});

describe("Assistant: inventory", () => {
  it("shows what the namespace holds, with a section it could not count said so", async () => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "What is in this project?" }));
    const table = await screen.findByRole("table", { name: "What this namespace holds" });
    expect(within(table).getByText("42")).toBeInTheDocument();
    expect(within(table).getByTestId("inventory-incomplete")).toHaveTextContent("read failed: TimeoutError");
    expect(screen.getByText(/Model: not enabled/)).toBeInTheDocument();
    expect(namespaceInventory).toHaveBeenCalledWith({ org: "dev", namespace: "team" }, expect.anything());
  });

  it("lists members apart from the identities observed on rows, and says when members are incomplete", async () => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Who are the users in this project?" }));
    const list = await screen.findByTestId("assistant-populations");
    const members = within(list)
      .getAllByRole("listitem")
      .find((li) => li.getAttribute("data-population") === "members");
    expect(members).toHaveTextContent(/people holding an active grant/);
    expect(members).toHaveTextContent(
      "Not complete: shared with everyone, a group or an agent: those members are not counted",
    );
    const subjects = within(list)
      .getAllByRole("listitem")
      .find((li) => li.getAttribute("data-population") === "subjects");
    expect(subjects).toHaveTextContent("ada12");
  });

  it("says a population lists only its largest identities", async () => {
    namespaceInventory.mockResolvedValueOnce({
      ...INVENTORY,
      populations: INVENTORY.populations.map((p) => (p.population === "subjects" ? { ...p, total: 340 } : p)),
    });
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Who are the users in this project?" }));
    const list = await screen.findByTestId("assistant-populations");
    const subjects = within(list)
      .getAllByRole("listitem")
      .find((li) => li.getAttribute("data-population") === "subjects");
    expect(subjects).toHaveTextContent("The 1 with the most rows are shown.");
  });

  it("says the counts are bound to the user the token is pinned to", async () => {
    namespaceInventory.mockResolvedValueOnce({
      ...INVENTORY,
      scope: { ...INVENTORY.scope, read_scope: "own_user", user_id: "ada", session_id: null },
    });
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Who are the users in this project?" }));
    expect(await screen.findByTestId("inventory-scope")).toHaveTextContent(
      "Memories, runs and the agents, authors and subjects on rows are counted for user ada only, as your token reads; documents and members are the namespace's.",
    );
  });

  it("offers no inspection or drafts once the inventory shows a token pinned to one user", async () => {
    namespaceInventory.mockResolvedValueOnce({
      ...INVENTORY,
      scope: { ...INVENTORY.scope, read_scope: "own_user", user_id: "ada", session_id: null },
    });
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "What is in this project?" }));
    await screen.findByTestId("inventory-scope");
    const configure = screen.getByRole("button", { name: "Configure this project" });
    expect(configure).toHaveAccessibleDescription(/bound to one user or session/);
    await userEvent.click(configure);
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    expect(tuneNamespace).not.toHaveBeenCalled();
    expect(inspectMemories).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "What is in this project?" })).not.toHaveAttribute("aria-disabled");
  });

  it("offers no inspection or drafts to a token whose own claim pins it, before any read", async () => {
    const b64 = (o: object) => btoa(JSON.stringify(o)).replace(/=+$/, "");
    localStorage.setItem("loom_token_v1", `${b64({ alg: "HS256" })}.${b64({ sub: "ada", read_user_id: "ada" })}.sig`);
    try {
      render(<Assistant />);
      const configure = screen.getByRole("button", { name: "Configure this project" });
      expect(configure).toHaveAccessibleDescription(/bound to one user or session/);
      await userEvent.click(configure);
      await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
      expect(tuneNamespace).not.toHaveBeenCalled();
      expect(inspectMemories).not.toHaveBeenCalled();
    } finally {
      localStorage.removeItem("loom_token_v1");
    }
  });

  it.each([
    ["own_session", { read_scope: "own_session", read_user_id: "ada" }],
    ["own_user without a pinned user", { read_scope: "own_user" }],
  ])("offers no inventory to a token scoped %s, which this page cannot complete", async (_label, claims) => {
    const b64 = (o: object) => btoa(JSON.stringify(o)).replace(/=+$/, "");
    localStorage.setItem("loom_token_v1", `${b64({ alg: "HS256" })}.${b64({ sub: "ada", ...claims })}.sig`);
    try {
      render(<Assistant />);
      for (const name of ["What is in this project?", "Who are the users in this project?"]) {
        const starter = screen.getByRole("button", { name });
        expect(starter).toHaveAccessibleDescription(
          "Your token's read scope needs a user or session id this page does not send.",
        );
        await userEvent.click(starter);
      }
      expect(namespaceInventory).not.toHaveBeenCalled();
    } finally {
      localStorage.removeItem("loom_token_v1");
    }
  });

  it("adds no scope note when the counts cover the namespace", async () => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "What is in this project?" }));
    await screen.findByTestId("assistant-inventory");
    expect(screen.queryByTestId("inventory-scope")).toBeNull();
  });

  it("reads again on every starter click", async () => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "What is in this project?" }));
    await screen.findByTestId("assistant-inventory");
    await userEvent.click(screen.getByRole("button", { name: "Who are the users in this project?" }));
    await screen.findByTestId("assistant-populations");
    expect(namespaceInventory).toHaveBeenCalledTimes(2);
  });

  it("says when the inventory cannot be read", async () => {
    namespaceInventory.mockRejectedValueOnce(new ApiError({ status: 500, code: "INTERNAL", message: "boom" }));
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "What is in this project?" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load the inventory");
  });

  it("shows nothing of the old namespace's inventory on the first render after a switch", async () => {
    const { rerender } = render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "What is in this project?" }));
    await screen.findByTestId("assistant-inventory");
    useStore.setState({ identity: { org: "dev", namespace: "other", agent: "loom-ui" } });
    rerender(<Assistant />);
    expect(screen.queryByTestId("assistant-inventory")).toBeNull();
  });

  it("closes the inventory panel when the namespace changes", async () => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "What is in this project?" }));
    await screen.findByTestId("assistant-inventory");
    switchNamespace();
    await waitFor(() => expect(screen.queryByRole("heading", { name: "What this namespace holds" })).toBeNull());
  });
});

describe("Assistant: inspection", () => {
  it("inspects a user: the rows filed under the user, apart from the rest", async () => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Analyze one user's memories" }));
    expect(screen.getByLabelText("User id")).toHaveFocus();
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    const result = await screen.findByTestId("assistant-inspection");
    expect(inspectMemories).toHaveBeenCalledWith({ org: "dev", namespace: "team" }, "user", "ada", expect.anything());
    expect(within(result).getByRole("heading", { name: /Filed under this user/ })).toBeInTheDocument();
    expect(within(result).getByText("ada")).toHaveClass("break-all");
    expect(within(result).getByText(OWN_USER)).toBeInTheDocument();
    expect(within(result).getByRole("heading", { name: /Every other live row of the namespace/ })).toBeInTheDocument();
    // A listed row opens in the inspector under the agent that wrote it.
    await userEvent.click(within(result).getByRole("button", { name: "Open memory billing/invoices" }));
    expect(useStore.getState().inspectedMemoryKey).toBe("billing/invoices");
    expect(useStore.getState().inspectedAgent).toBe("claude-code");
    expect(within(result).getByText("Statuses: active 1")).toBeInTheDocument();
    expect(within(result).getByText("3 versions")).toBeInTheDocument();
    expect(result).not.toHaveTextContent(String(NS_VERSION).slice(0, 10));
    expect(within(result).getByText("No live rows.")).toBeInTheDocument();
    expect(within(result).getByTestId("inspect-policy")).toHaveTextContent(
      "not shown (only an org admin reads the operator extraction policy)",
    );
    expect(within(result).getByText("No calls in the last 7 days.")).toBeInTheDocument();
  });

  it.each([
    ["Agent", "agent", "claude-code", "Written by this agent"],
    ["Session", "session", "s-1", "Written in this session"],
  ] as const)("inspects a %s with its own heading", async (label, subject, id, heading) => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: label }));
    await userEvent.type(screen.getByLabelText(`${label} id`), id);
    await userEvent.click(screen.getByRole("button", { name: "Inspect" }));
    const result = await screen.findByTestId("assistant-inspection");
    expect(inspectMemories).toHaveBeenCalledWith(expect.anything(), subject, id, expect.anything());
    expect(within(result).getByRole("heading", { name: new RegExp(heading) })).toBeInTheDocument();
  });

  it("says a view lists only its newest rows, and a view it could not count is not empty", async () => {
    inspectMemories.mockResolvedValueOnce({
      ...inspection("user", "ada"),
      own: view(OWN_USER, [ROW], 40),
      others: view(OTHERS_USER, []),
      skipped: ["others.counts: TimeoutError"],
    });
    render(<Assistant />);
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    const result = await screen.findByTestId("assistant-inspection");
    expect(within(result).getByText("Newest 1 of 40 listed.")).toBeInTheDocument();
    expect(within(result).getByText("The rows could not be counted.")).toBeInTheDocument();
    expect(within(result).queryByText("No live rows.")).toBeNull();
  });

  it("states the activity window, and the ledger's retention beside the calls", async () => {
    inspectMemories.mockResolvedValueOnce({
      ...inspection("user", "ada"),
      activity: {
        window_seconds: 30 * 86_400,
        calls: [{ operation: "memory.search", calls: 9, succeeded: 8 }],
        first_at: "2026-10-20T10:00:00",
        last_at: "2026-11-01T10:00:00",
        gap: "The ledger keeps calls for a 7-day window, so older ones are not counted.",
      },
    });
    render(<Assistant />);
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    const result = await screen.findByTestId("assistant-inspection");
    expect(within(result).getByText(/Last 30 days, succeeded of called: memory.search 8\/9/)).toBeInTheDocument();
    expect(
      within(result).getByText("The ledger keeps calls for a 7-day window, so older ones are not counted."),
    ).toBeInTheDocument();
  });

  it("says which settings and calls could not be read, never their defaults", async () => {
    inspectMemories.mockResolvedValueOnce({
      ...inspection("user", "ada"),
      own: view(OWN_USER, [], 40),
      settings: {
        ...inspection("user", "ada").settings,
        guide_revision: 0,
        guide_summary: "",
        categories_registered: 0,
      },
      skipped: [
        "own.newest: TimeoutError",
        "settings.guide: TimeoutError",
        "settings.categories: TimeoutError",
        "activity: TimeoutError",
      ],
    });
    render(<Assistant />);
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    const result = await screen.findByTestId("assistant-inspection");
    expect(within(result).getByText("The rows could not be listed.")).toBeInTheDocument();
    expect(within(result).queryByText(/Newest 0 of/)).toBeNull();
    expect(within(result).getByText("The guide could not be read.")).toBeInTheDocument();
    expect(within(result).queryByText(/Guide revision none/)).toBeNull();
    expect(within(result).getByText("The registered categories could not be read.")).toBeInTheDocument();
    expect(within(result).getByText("The calls could not be read.")).toBeInTheDocument();
    expect(within(result).queryByText(/No calls in the last/)).toBeNull();
  });

  it("says a view's tags and categories, and the ledger's retention, could not be read", async () => {
    inspectMemories.mockResolvedValueOnce({
      ...inspection("user", "ada"),
      skipped: ["own.tags: TimeoutError", "own.categories: TimeoutError", "activity.retention: TimeoutError"],
    });
    render(<Assistant />);
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    const result = await screen.findByTestId("assistant-inspection");
    expect(within(result).getByText("Tags: could not be read")).toBeInTheDocument();
    expect(within(result).getByText("Categories: could not be read")).toBeInTheDocument();
    expect(within(result).getByText(/The ledger's retention could not be read/)).toBeInTheDocument();
  });

  it.each([
    ["", "none set"],
    ["Keep costs.\nKeep ages.", "Keep costs.\nKeep ages."],
  ])("shows an org admin the operator policy %j as %j", async (policy, shown) => {
    const base = inspection("user", "ada");
    inspectMemories.mockResolvedValueOnce({
      ...base,
      settings: { ...base.settings, extraction_policy: policy, extraction_policy_unavailable: "" },
    });
    render(<Assistant />);
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    const line = await screen.findByTestId("inspect-policy");
    if (policy) expect(within(line).getByTestId("inspect-policy-text").textContent).toBe(shown);
    else expect(line).toHaveTextContent(`Operator extraction policy: ${shown}`);
  });

  it("keeps the rows, tags and categories it read when the count read failed", async () => {
    inspectMemories.mockResolvedValueOnce({
      ...inspection("user", "ada"),
      own: { ...view(OWN_USER, [ROW], 0), top_tags: [{ name: "billing", rows: 1 }] },
      skipped: ["own.counts: TimeoutError"],
    });
    render(<Assistant />);
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    const own = (await screen.findByTestId("assistant-inspection")).querySelector(
      '[data-view="inspect-own"]',
    ) as HTMLElement;
    expect(within(own).getByText("The rows could not be counted.")).toBeInTheDocument();
    expect(within(own).getByText("Tags: billing 1")).toBeInTheDocument();
    expect(within(own).getByRole("button", { name: "Open memory billing/invoices" })).toBeInTheDocument();
    expect(within(own).queryByText(/Newest/)).toBeNull();
  });

  it("labels a view's tags as the top 20 when the server's cap is reached", async () => {
    const tags = Array.from({ length: 20 }, (_, n) => ({ name: `t${n}`, rows: 1 }));
    inspectMemories.mockResolvedValueOnce({
      ...inspection("user", "ada"),
      own: { ...view(OWN_USER, [ROW]), top_tags: tags },
    });
    render(<Assistant />);
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    const result = await screen.findByTestId("assistant-inspection");
    expect(within(result).getByText(/^Top 20 tags: t0 1/)).toBeInTheDocument();
  });

  it("shows a guide that was never written as none written", async () => {
    const base = inspection("user", "ada");
    inspectMemories.mockResolvedValueOnce({
      ...base,
      settings: { ...base.settings, guide_revision: 0, guide_summary: "" },
    });
    render(<Assistant />);
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    expect(await screen.findByText("No guide written")).toBeInTheDocument();
  });

  it("does not inspect without an id", async () => {
    render(<Assistant />);
    const inspect = screen.getByRole("button", { name: "Inspect" });
    expect(inspect).toHaveAttribute("aria-disabled", "true");
    await userEvent.click(inspect);
    await userEvent.type(screen.getByLabelText("User id"), "{Enter}");
    expect(inspectMemories).not.toHaveBeenCalled();
  });

  it("says when the inspection cannot be read", async () => {
    inspectMemories.mockRejectedValueOnce(new ApiError({ status: 500, code: "INTERNAL", message: "boom" }));
    render(<Assistant />);
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load the inspection");
  });

  it("drops the inspection when the namespace changes", async () => {
    render(<Assistant />);
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    await screen.findByTestId("assistant-inspection");
    switchNamespace();
    await waitFor(() => expect(screen.queryByTestId("assistant-inspection")).toBeNull());
  });
});

describe("Assistant: review changes", () => {
  it("renders each draft with its diff, evidence and the call that would apply it, and never applies", async () => {
    const { container } = render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    const review = await screen.findByTestId("assistant-review");
    const card = within(review).getByRole("listitem", { name: /Register a category for the tag/ });
    expect(within(card).getByText("Draft, not approved")).toBeInTheDocument();
    expect(card.querySelector('[data-side="current"]')).toHaveTextContent("(empty)");
    expect(card.querySelector('[data-side="proposed"]')).toHaveTextContent("billing");
    expect(within(card).getByText(/12 supporting · 4 already filed under another category/)).toBeInTheDocument();
    await userEvent.click(within(card).getByRole("button", { name: "Open memory billing/invoices" }));
    expect(useStore.getState().inspectedMemoryKey).toBe("billing/invoices");
    expect(useStore.getState().inspectedAgent).toBe("claude-code");
    expect(within(card).getByTestId("affected-agents")).toHaveTextContent("claude-codeand 2 more");
    expect(within(card).getByText("namespace.register_category")).toBeInTheDocument();
    expect(within(card).getByTestId("proposal-args")).toHaveTextContent('"name": "billing"');
    const apply = within(card).getByRole("button", { name: "Apply" });
    expect(apply).toHaveAttribute("aria-disabled", "true");
    expect(apply).toHaveAccessibleDescription(/A writer on this namespace sends namespace.register_category/);
    await userEvent.click(apply);
    expect(within(review).getByTestId("tune-skipped")).toHaveTextContent(
      "audit.telemetry_unavailable: window within the ledger lag",
    );
    expect(tuneNamespace).toHaveBeenCalledTimes(1);
    expect(await axeViolations(container)).toEqual([]);
  });

  it("shows a guide draft's revision and arguments, and says when the writers could not be read", async () => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    const review = await screen.findByTestId("assistant-review");
    const card = within(review).getByRole("listitem", { name: /Name the key prefixes the guide leaves out/ });
    expect(within(card).getByText(/Current \(revision 2\)/)).toBeInTheDocument();
    expect(within(card).getByText(/^Evidence: 12 supporting$/)).toBeInTheDocument();
    expect(within(card).getByTestId("proposal-args")).toHaveTextContent('"expected_revision": 2');
    expect(within(card).getByText(/the namespace's writers could not be read/)).toBeInTheDocument();
  });

  it("captions a guide with no revision yet without a revision", async () => {
    tuneNamespace.mockResolvedValueOnce({ ...TUNE, proposals: [{ ...GUIDE, current: "", current_revision: 0 }] });
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    const review = await screen.findByTestId("assistant-review");
    expect(within(review).getByText("Current")).toBeInTheDocument();
    expect(within(review).queryByText(/revision 0/)).toBeNull();
  });

  it("says that each writer sends the extraction instructions itself", async () => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    const review = await screen.findByTestId("assistant-review");
    const card = within(review).getByRole("listitem", {
      name: /Send extraction instructions for the shapes the audit found/,
    });
    expect(
      within(card).getByText("Each writer adds these arguments to its own memory.set_from_messages requests:"),
    ).toBeInTheDocument();
    expect(
      within(card).getByText(/episodic_blobs · 5 supporting · at least 25 unflagged rows it must keep working for/),
    ).toBeInTheDocument();
    expect(within(card).getByTestId("proposal-args")).toHaveTextContent('"extraction_instructions"');
    expect(within(card).getByRole("button", { name: "Apply" })).toHaveAccessibleDescription(
      /no single call applies this/,
    );
    expect(within(card).getByTestId("affected-agents")).toHaveTextContent("Affected agents:claude-code");
  });

  it("does not claim the evidence supports no change when a read failed", async () => {
    tuneNamespace.mockResolvedValueOnce({ ...TUNE, proposals: [], skipped: ["audit: TimeoutError"] });
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    const review = await screen.findByTestId("assistant-review");
    expect(within(review).getByRole("status")).toHaveTextContent(
      "No drafts. What was skipped, and why, is listed below.",
    );
    expect(within(review).getByTestId("tune-skipped")).toHaveTextContent("audit: TimeoutError");
  });

  it("lists what was not drafted, and why, when the evidence rules every draft out", async () => {
    const reason = "extraction: memory.set_from_messages writes no row here, so no rule is drafted for episodic_blobs";
    tuneNamespace.mockResolvedValueOnce({ ...TUNE, proposals: [], skipped: [reason] });
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    const review = await screen.findByTestId("assistant-review");
    expect(within(review).getByRole("status")).toHaveTextContent(
      "No drafts. What was skipped, and why, is listed below.",
    );
    expect(within(review).getByTestId("tune-skipped")).toHaveTextContent(reason);
  });

  it("says the evidence supports no change when nothing was left out", async () => {
    tuneNamespace.mockResolvedValueOnce({ ...TUNE, proposals: [], skipped: [] });
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    const review = await screen.findByTestId("assistant-review");
    expect(within(review).getByRole("status")).toHaveTextContent("The evidence supports no change.");
  });

  it("lists a read skipped for a draft it still shows under Skipped, not as undrafted", async () => {
    tuneNamespace.mockResolvedValueOnce({
      ...TUNE,
      proposals: [{ ...CATEGORY, evidence: { ...CATEGORY.evidence, supporting: [] } }],
      skipped: ["category.billing.cited: TimeoutError"],
    });
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    const review = await screen.findByTestId("assistant-review");
    expect(within(review).getByRole("listitem", { name: /Register a category for the tag/ })).toBeInTheDocument();
    expect(within(review).getByTestId("tune-skipped")).toHaveTextContent(
      "Skipped:category.billing.cited: TimeoutError",
    );
    expect(within(review).queryByText(/Not drafted/)).toBeNull();
  });

  it("keeps the drafts on screen, marked busy, while they are drafted again", async () => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    await screen.findByTestId("assistant-review");
    tuneNamespace.mockImplementationOnce(() => new Promise<NamespaceTune>(() => {}));
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    const review = screen.getByTestId("assistant-review");
    expect(review.parentElement).toHaveAttribute("aria-busy", "true");
    expect(within(review).getByText("Register a category for the tag `billing`")).toBeInTheDocument();
  });

  it("says when the drafts cannot be read", async () => {
    tuneNamespace.mockRejectedValueOnce(new ApiError({ status: 500, code: "INTERNAL", message: "boom" }));
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load the drafts");
  });

  it("is not left loading when the page comes back to a namespace it left mid-read", async () => {
    tuneNamespace.mockImplementationOnce(() => new Promise<NamespaceTune>(() => {}));
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    switchNamespace();
    act(() => {
      useStore.setState({ identity: { org: "dev", namespace: "team", agent: "loom-ui" } });
    });
    await waitFor(() => expect(screen.getByText(/No drafts yet/)).toBeInTheDocument());
    expect(screen.queryByText("Loading the drafts")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    expect(await screen.findByTestId("assistant-review")).toBeInTheDocument();
  });

  it("drops a draft that answers after the namespace changed", async () => {
    let answer: (t: NamespaceTune) => void = () => {};
    tuneNamespace.mockImplementationOnce(() => new Promise<NamespaceTune>((r) => (answer = r)));
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    switchNamespace();
    await act(async () => answer(TUNE));
    await waitFor(() => expect(screen.getByText(/No drafts yet/)).toBeInTheDocument());
    expect(screen.queryByTestId("assistant-review")).toBeNull();
  });
});
