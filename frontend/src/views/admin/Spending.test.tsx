/** After a cap save the Usage page shows the server's budget for the namespace it is on:
 *  a removed cap reads the global one, and a save that lands after a namespace switch
 *  leaves the new namespace's bar alone. */
import { act, cleanup, fireEvent, render, screen, waitFor } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../api";
import type { BudgetStatus, CostRow, NamespaceInfo } from "../../api";
import { ApiError } from "../../lib/errors";
import { useStore } from "../../lib/state";
import { invalidateShared } from "../../lib/sharedReads";
import { Spending } from "./Spending";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    listCostRollup: vi.fn(async () => ({ rows: [] })),
    getOrgBudgetStatus: vi.fn(),
    getBudgetStatus: vi.fn(),
    getNamespace: vi.fn(),
    reregisterNamespace: vi.fn(),
    listMine: vi.fn(async () => []),
    getAgentSpend: vi.fn(async () => ({ rows: [], total_cost_usd: 0, total_turns: 0, unpriced_turns: 0 })),
  };
});

function deferred<T>() {
  let resolve: (v: T) => void = () => undefined;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

const budget = (namespace: string, spent: number, cap: number, source: BudgetStatus["cap_source"]): BudgetStatus => ({
  namespace,
  spent_24h_usd: spent,
  cap_usd: cap,
  cap_source: source,
  over_cap: false,
  pct_of_cap: spent / cap,
});

const nsRow = (namespace: string): NamespaceInfo => ({
  namespace,
  owner_email: "me@example.com",
  description: "",
  tags: [],
  active: true,
  daily_cost_cap_usd: 5,
  consolidation_enabled: false,
  conflict_threshold: null,
  created_at: "",
  updated_at: "",
});

const setNamespace = (namespace: string) =>
  useStore.setState({ identity: { org: "dev", namespace, agent: "loom-ui" } });

async function saveCap(value: string) {
  fireEvent.click(await screen.findByRole("button", { name: /Edit cap/ }));
  fireEvent.change(screen.getByLabelText("Daily LLM budget cap in dollars"), { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
}

beforeEach(() => {
  invalidateShared();
  // Auth off: the operator is the namespace admin, so the cap editor shows.
  useStore.setState({ whoami: { authenticated: false }, myNamespaces: null });
  setNamespace("team-a");
  vi.mocked(api.getOrgBudgetStatus).mockResolvedValue(budget("", 0, 1, "global"));
  vi.mocked(api.getNamespace).mockImplementation(async ({ namespace }) => nsRow(namespace));
});

afterEach(() => {
  cleanup();
  vi.mocked(api.getBudgetStatus).mockReset();
  vi.mocked(api.reregisterNamespace).mockReset();
});

describe("Spending budget cap editor", () => {
  it("re-reads the budget after removing a cap and shows the global cap", async () => {
    vi.mocked(api.getBudgetStatus)
      .mockResolvedValueOnce(budget("team-a", 0.5, 5, "namespace"))
      .mockResolvedValueOnce(budget("team-a", 0.5, 2, "global"));
    vi.mocked(api.reregisterNamespace).mockResolvedValue({ registered: true, namespace: "team-a", updated_at: "" });
    render(<Spending />);
    expect(await screen.findByText("$0.5000 / $5.00 (namespace)")).toBeInTheDocument();

    await saveCap("0");

    expect(await screen.findByText("$0.5000 / $2.00 (global)")).toBeInTheDocument();
    expect(screen.queryByText("$0.5000 / $5.00 (global)")).toBeNull();
    expect(api.reregisterNamespace).toHaveBeenCalledWith({ org: "dev", namespace: "team-a" }, nsRow("team-a"), {
      daily_cost_cap_usd: 0,
    });
    // The shared cache was dropped, so the second read reached the server.
    expect(api.getBudgetStatus).toHaveBeenCalledTimes(2);
  });

  it("a save that resolves after a namespace switch leaves the new namespace's budget", async () => {
    vi.mocked(api.getBudgetStatus).mockImplementation(async ({ namespace }) =>
      namespace === "team-a" ? budget("team-a", 0.1, 1, "namespace") : budget("team-b", 0.2, 7, "namespace"),
    );
    const write = deferred<Awaited<ReturnType<typeof api.reregisterNamespace>>>();
    vi.mocked(api.reregisterNamespace).mockReturnValue(write.promise);
    render(<Spending />);
    expect(await screen.findByText("$0.1000 / $1.00 (namespace)")).toBeInTheDocument();

    await saveCap("3");
    await waitFor(() => expect(api.reregisterNamespace).toHaveBeenCalled());
    act(() => setNamespace("team-b"));
    expect(await screen.findByText("$0.2000 / $7.00 (namespace)")).toBeInTheDocument();

    await act(async () => write.resolve({ registered: true, namespace: "team-a", updated_at: "" }));

    expect(await screen.findByText("$0.2000 / $7.00 (namespace)")).toBeInTheDocument();
    expect(screen.queryByText(/\$3\.00/)).toBeNull();
    expect(screen.queryByText("$0.1000 / $1.00 (namespace)")).toBeNull();
    expect(api.reregisterNamespace).toHaveBeenCalledWith({ org: "dev", namespace: "team-a" }, nsRow("team-a"), {
      daily_cost_cap_usd: 3,
    });
  });
});

describe("Spending cap editor on a restricted namespace", () => {
  // A token that names the namespace and carries ADMIN, but whose direct grant is VIEWER: it
  // reads the restricted row, and namespace.register refuses it.
  const namedAdmin = {
    authenticated: true,
    subject: "a@example.com",
    org: "dev",
    namespaces: ["team-a"],
    roles: ["ADMIN"],
  };
  const viewerGrant = {
    namespace: "team-a",
    role: "viewer" as const,
    visibility: "private" as const,
    description: "",
    joined_at: null,
    is_new: false,
  };

  it("shows Edit cap unavailable to an admin claim namespace.register would refuse, and says why", async () => {
    useStore.setState({ whoami: namedAdmin, myNamespaces: [viewerGrant] });
    vi.mocked(api.getBudgetStatus).mockResolvedValue(budget("team-a", 0.1, 1, "namespace"));
    vi.mocked(api.getNamespace).mockResolvedValue({ ...nsRow("team-a"), classification: "restricted" });
    render(<Spending />);
    expect(await screen.findByText("$0.1000 / $1.00 (namespace)")).toBeInTheDocument();
    await waitFor(() => expect(api.getNamespace).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Edit cap/ })).toHaveAccessibleDescription(
        "Changing its cap needs a direct admin grant on team-a.",
      ),
    );
    expect(screen.getByRole("button", { name: /Edit cap/ })).toHaveAttribute("aria-disabled", "true");
    await userEvent.click(screen.getByRole("button", { name: /Edit cap/ }));
    expect(screen.queryByRole("spinbutton")).toBeNull();
  });

  it("leaves a restricted namespace's cap to the server for a scoped token holding the direct grant", async () => {
    useStore.setState({
      whoami: { authenticated: true, subject: "a@example.com", org: "dev", roles: [], namespaces: ["team-a"] },
      myNamespaces: [{ ...viewerGrant, role: "admin" as never }],
    });
    vi.mocked(api.getBudgetStatus).mockResolvedValue(budget("team-a", 0.1, 1, "namespace"));
    vi.mocked(api.getNamespace).mockResolvedValue({ ...nsRow("team-a"), classification: "restricted" });
    render(<Spending />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Edit cap/ })).toHaveAccessibleDescription(
        "The server decides this token's access to team-a; it does not show here.",
      ),
    );
  });

  it("says the grants are loading on a restricted namespace for an admin claim whose list_mine is unread", async () => {
    vi.mocked(api.listMine).mockImplementation(() => new Promise<never>(() => {}));
    useStore.setState({ whoami: namedAdmin, myNamespaces: null });
    vi.mocked(api.getBudgetStatus).mockResolvedValue(budget("team-a", 0.1, 1, "namespace"));
    vi.mocked(api.getNamespace).mockResolvedValue({ ...nsRow("team-a"), classification: "restricted" });
    render(<Spending />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Edit cap/ })).toHaveAccessibleDescription(
        "Your role on this namespace is still loading.",
      ),
    );
    await userEvent.click(screen.getByRole("button", { name: /Edit cap/ }));
    expect(screen.queryByRole("spinbutton")).toBeNull();
    vi.mocked(api.listMine).mockImplementation(async () => []);
  });

  it("shows a viewer on a standard namespace Edit cap unavailable, with the role it needs", async () => {
    useStore.setState({
      whoami: { authenticated: true, subject: "v@example.com", org: "dev", roles: ["VIEWER"], namespaces: ["team-a"] },
      myNamespaces: [viewerGrant],
    });
    vi.mocked(api.getBudgetStatus).mockResolvedValue(budget("team-a", 0.1, 1, "namespace"));
    vi.mocked(api.getNamespace).mockResolvedValue({ ...nsRow("team-a"), classification: "standard" });
    render(<Spending />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Edit cap/ })).toHaveAccessibleDescription(
        "Needs admin on team-a. An admin of it can grant that.",
      ),
    );
    await userEvent.click(screen.getByRole("button", { name: /Edit cap/ }));
    expect(screen.queryByRole("spinbutton")).toBeNull();
  });

  it.each([
    ["loading", "restricted", () => new Promise<never>(() => {}), "Your role on this namespace is still loading."],
    ["failed", "restricted", () => Promise.reject(new Error("down")), "Couldn't check your access to team-a."],
    ["loading", "standard", () => new Promise<never>(() => {}), "Your role on this namespace is still loading."],
    ["failed", "standard", () => Promise.reject(new Error("down")), "Couldn't check your access to team-a."],
  ] as const)(
    "says the role is unread while list_mine is %s on a %s namespace, and opens nothing",
    async (_label, classification, read, line) => {
      vi.mocked(api.listMine).mockImplementation(read as never);
      useStore.setState({
        whoami: {
          authenticated: true,
          subject: "v@example.com",
          org: "dev",
          roles: ["VIEWER"],
          namespaces: ["team-a"],
        },
        myNamespaces: null,
      });
      vi.mocked(api.getBudgetStatus).mockResolvedValue(budget("team-a", 0.1, 1, "namespace"));
      vi.mocked(api.getNamespace).mockResolvedValue({ ...nsRow("team-a"), classification });
      render(<Spending />);
      await waitFor(() => expect(screen.getByRole("button", { name: /Edit cap/ })).toHaveAccessibleDescription(line));
      await userEvent.click(screen.getByRole("button", { name: /Edit cap/ }));
      expect(screen.queryByRole("spinbutton")).toBeNull();
      expect(api.reregisterNamespace).not.toHaveBeenCalled();
      vi.mocked(api.listMine).mockImplementation(async () => []);
    },
  );

  it("says the access could not be checked when the namespace's row cannot be read", async () => {
    useStore.setState({
      whoami: { authenticated: true, subject: "v@example.com", org: "dev", roles: ["VIEWER"], namespaces: ["team-a"] },
      myNamespaces: [viewerGrant],
    });
    vi.mocked(api.getBudgetStatus).mockResolvedValue(budget("team-a", 0.1, 1, "namespace"));
    vi.mocked(api.getNamespace).mockRejectedValue(new Error("down"));
    render(<Spending />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Edit cap/ })).toHaveAccessibleDescription(
        "Couldn't check your access to team-a.",
      ),
    );
  });

  it("reads the role from the page's own list_mine when the store holds none", async () => {
    vi.mocked(api.listMine).mockResolvedValue([viewerGrant] as never);
    useStore.setState({
      whoami: { authenticated: true, subject: "v@example.com", org: "dev", roles: ["VIEWER"], namespaces: ["team-a"] },
      myNamespaces: null,
    });
    vi.mocked(api.getBudgetStatus).mockResolvedValue(budget("team-a", 0.1, 1, "namespace"));
    vi.mocked(api.getNamespace).mockResolvedValue({ ...nsRow("team-a"), classification: "standard" });
    render(<Spending />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Edit cap/ })).toHaveAccessibleDescription(
        "Needs admin on team-a. An admin of it can grant that.",
      ),
    );
    await userEvent.click(screen.getByRole("button", { name: /Edit cap/ }));
    expect(screen.queryByRole("spinbutton")).toBeNull();
    vi.mocked(api.listMine).mockImplementation(async () => []);
  });

  it("forgets the last namespace's row on a switch until the next one is read", async () => {
    const grants = [
      { ...viewerGrant, role: "admin" as never },
      { ...viewerGrant, namespace: "team-b", role: "admin" as never },
    ];
    vi.mocked(api.listMine).mockResolvedValue(grants as never);
    useStore.setState({
      whoami: { authenticated: true, subject: "a@example.com", org: "dev", namespaces: ["*"], roles: ["ADMIN"] },
      myNamespaces: [
        { ...viewerGrant, role: "admin" as never },
        { ...viewerGrant, namespace: "team-b", role: "admin" as never },
      ],
    });
    vi.mocked(api.getBudgetStatus).mockImplementation(async ({ namespace }) =>
      namespace === "team-a" ? budget("team-a", 0.1, 1, "namespace") : budget("team-b", 0.2, 7, "namespace"),
    );
    const rowB = deferred<NamespaceInfo>();
    vi.mocked(api.getNamespace).mockImplementation(async ({ namespace }) =>
      namespace === "team-a" ? { ...nsRow("team-a"), classification: "standard" } : rowB.promise,
    );
    render(<Spending />);
    expect(await screen.findByText("$0.1000 / $1.00 (namespace)")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: /Edit cap/ })).not.toHaveAttribute("aria-disabled"));
    act(() => setNamespace("team-b"));
    expect(await screen.findByText("$0.2000 / $7.00 (namespace)")).toBeInTheDocument();
    // team-a's standard row no longer stands in for team-b's, which has not answered.
    const edit = screen.getByRole("button", { name: /Edit cap/ });
    expect(edit).toHaveAccessibleDescription("Reading the settings of team-b.");
    await userEvent.click(edit);
    expect(screen.queryByRole("spinbutton")).toBeNull();
    await act(async () => rowB.resolve({ ...nsRow("team-b"), classification: "standard" }));
    vi.mocked(api.listMine).mockImplementation(async () => []);
  });

  it("treats a namespace with no row as answered: an admin edits the cap, a viewer needs admin", async () => {
    vi.mocked(api.getBudgetStatus).mockResolvedValue(budget("team-a", 0.1, 1, "namespace"));
    vi.mocked(api.getNamespace).mockResolvedValue(null);
    useStore.setState({
      whoami: { authenticated: true, subject: "a@example.com", org: "dev", roles: ["ADMIN"], namespaces: ["team-a"] },
      myNamespaces: [],
    });
    render(<Spending />);
    await waitFor(() => expect(screen.getByRole("button", { name: /Edit cap/ })).not.toHaveAttribute("aria-disabled"));
    await userEvent.click(screen.getByRole("button", { name: /Edit cap/ }));
    expect(await screen.findByRole("spinbutton")).toBeInTheDocument();
    cleanup();
    useStore.setState({
      whoami: { authenticated: true, subject: "v@example.com", org: "dev", roles: ["VIEWER"], namespaces: ["team-a"] },
      myNamespaces: [viewerGrant],
    });
    render(<Spending />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Edit cap/ })).toHaveAccessibleDescription(
        "Needs admin on team-a. An admin of it can grant that.",
      ),
    );
  });

  it("keeps it for the owner of an auto-registered namespace whose role the grant list cannot show", async () => {
    vi.mocked(api.listMine).mockResolvedValue([] as never);
    useStore.setState({
      whoami: { authenticated: true, subject: "me@example.com", org: "dev", roles: ["WRITER"], namespaces: ["*"] },
      myNamespaces: [],
    });
    vi.mocked(api.getBudgetStatus).mockResolvedValue(budget("team-a", 0.1, 1, "namespace"));
    vi.mocked(api.getNamespace).mockResolvedValue({
      ...nsRow("team-a"),
      classification: "standard",
      tags: ["auto-registered"],
    });
    render(<Spending />);
    await waitFor(() => expect(screen.getByRole("button", { name: /Edit cap/ })).not.toHaveAttribute("aria-disabled"));
    await userEvent.click(screen.getByRole("button", { name: /Edit cap/ }));
    expect(screen.getByRole("spinbutton")).toBeInTheDocument();
    vi.mocked(api.listMine).mockImplementation(async () => []);
  });

  it("closes an open cap editor when the role falls short, and sends nothing", async () => {
    vi.mocked(api.getBudgetStatus).mockResolvedValue(budget("team-a", 0.1, 1, "namespace"));
    vi.mocked(api.getNamespace).mockResolvedValue({ ...nsRow("team-a"), classification: "standard" });
    render(<Spending />);
    fireEvent.click(await screen.findByRole("button", { name: /Edit cap/ }));
    expect(screen.getByLabelText("Daily LLM budget cap in dollars")).toBeInTheDocument();
    act(() =>
      useStore.setState({
        whoami: {
          authenticated: true,
          subject: "v@example.com",
          org: "dev",
          roles: ["VIEWER"],
          namespaces: ["team-a"],
        },
        myNamespaces: [viewerGrant],
      }),
    );
    await waitFor(() => expect(screen.queryByLabelText("Daily LLM budget cap in dollars")).toBeNull());
    expect(screen.getByRole("button", { name: /Edit cap/ })).toHaveAccessibleDescription(
      "Needs admin on team-a. An admin of it can grant that.",
    );
    expect(api.reregisterNamespace).not.toHaveBeenCalled();
  });

  it("offers Edit cap on a restricted namespace with auth off, reading no grants", async () => {
    vi.mocked(api.listMine).mockClear();
    useStore.setState({ whoami: { authenticated: false }, myNamespaces: null });
    vi.mocked(api.getBudgetStatus).mockResolvedValue(budget("team-a", 0.1, 1, "namespace"));
    vi.mocked(api.getNamespace).mockResolvedValue({ ...nsRow("team-a"), classification: "restricted" });
    render(<Spending />);
    await waitFor(() => expect(api.getNamespace).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByRole("button", { name: /Edit cap/ })).not.toHaveAttribute("aria-disabled"));
    expect(api.listMine).not.toHaveBeenCalled();
  });

  it("keeps it for the same admin on a standard namespace", async () => {
    useStore.setState({ whoami: namedAdmin, myNamespaces: [viewerGrant] });
    vi.mocked(api.getBudgetStatus).mockResolvedValue(budget("team-a", 0.1, 1, "namespace"));
    vi.mocked(api.getNamespace).mockResolvedValue({ ...nsRow("team-a"), classification: "standard" });
    render(<Spending />);
    expect(await screen.findByRole("button", { name: /Edit cap/ })).toBeInTheDocument();
  });
});

describe("Spending daily budget meter", () => {
  it("is named once, clamps its value at the cap and speaks the real figure", async () => {
    vi.mocked(api.getBudgetStatus).mockResolvedValue({ ...budget("team-a", 1.3, 1, "namespace"), over_cap: true });
    render(<Spending />);
    const meter = await screen.findByRole("progressbar", { name: "Daily LLM budget used" });
    expect(meter).toHaveAttribute("aria-valuenow", "100");
    expect(meter).toHaveAttribute("aria-valuetext", "130% — $1.3000 of the $1.00 cap");
  });
});

const cost = (operation: string, cost_usd: number): CostRow => ({
  day: "2026-09-20",
  agent: "claude-code",
  namespace: "team-a",
  user_id: "-",
  operation,
  ops: 3,
  errors: 0,
  tokens: cost_usd > 0 ? 100 : 0,
  embed_tokens: 0,
  cost_usd,
  latency_p50_ms: 10,
  latency_p95_ms: 20,
  latency_p99_ms: 30,
});

describe("Spending table count and failure", () => {
  beforeEach(() => {
    vi.mocked(api.getBudgetStatus).mockResolvedValue(budget("team-a", 0, 1, "uncapped"));
  });

  it("counts the rows the table shows at the end of the toolbar", async () => {
    vi.mocked(api.listCostRollup).mockResolvedValueOnce({
      rows: [cost("memory.set", 0.01), cost("memory.ask", 0.02), cost("memory.list", 0)],
    });
    render(<Spending />);
    await screen.findByText("memory.set");
    const bar = document.querySelector<HTMLElement>('[role="group"][aria-label="Usage breakdown"]');
    expect(bar).toHaveTextContent("2 rows");
  });

  it("keeps the toolbar and its Select while another breakdown reads, and draws none of the old rows", async () => {
    vi.mocked(api.listCostRollup)
      .mockResolvedValueOnce({ rows: [cost("memory.set", 0.01)] })
      .mockReturnValueOnce(new Promise(() => {}));
    render(<Spending />);
    await screen.findByText("memory.set");
    fireEvent.click(screen.getByRole("button", { name: "Break down by" }));
    await waitFor(() => expect(document.querySelector('[cui-select-item][data-value="agent"]')).not.toBeNull());
    fireEvent.click(document.querySelector('[cui-select-item][data-value="agent"]') as HTMLElement);
    await waitFor(() => expect(screen.queryByText("memory.set")).toBeNull());
    expect(screen.getByRole("button", { name: "Break down by" })).toBeInTheDocument();
    expect(document.querySelector('[role="group"][aria-label="Usage breakdown"]')).not.toHaveTextContent(/\d+ rows?/);
  });

  it("says a failed read failed, with Retry, never 'No operations recorded'", async () => {
    vi.mocked(api.listCostRollup).mockRejectedValueOnce(
      new ApiError({ status: 503, message: "down", requestId: "r-1" }),
    );
    render(<Spending />);
    expect(await screen.findByText("Couldn't load usage")).toBeInTheDocument();
    expect(screen.getByText(/r-1/)).toBeInTheDocument();
    expect(screen.queryByText("No operations recorded")).toBeNull();
    vi.mocked(api.listCostRollup).mockResolvedValueOnce({ rows: [cost("memory.set", 0.01)] });
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("memory.set")).toBeInTheDocument();
  });
});
