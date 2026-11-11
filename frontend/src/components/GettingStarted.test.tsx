import { act, cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryListEntry, MyNamespaceEntry, TokenEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { onboardingKey } from "../lib/onboarding";
import { axeViolations } from "../test/axe";
import { Recent } from "../views/Recent";

// The first-run checklist sits on Memories, the bare root's landing. It shows for an
// account with only its personal namespace and no memories, in place of the empty state;
// each step ticks from what Loom holds (a memory an agent wrote, a token's first call,
// the memory count), never from pressing its button; Hide holds per namespace; and the
// whole thing passes axe.

const auth = vi.hoisted(() => ({ off: false }));
const rows = vi.hoisted(() => ({ entries: [] as MemoryListEntry[] }));
const tokens = vi.hoisted(() => ({ list: [] as TokenEntry[] }));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    isAuthDisabled: () => auth.off,
    listTokens: vi.fn(async (): Promise<TokenEntry[]> => tokens.list),
    listMine: vi.fn(async (): Promise<MyNamespaceEntry[]> => []),
    listMemories: vi.fn(async () => ({
      entries: rows.entries,
      truncated: false,
      total: rows.entries.length,
      today: 0,
    })),
    countMemories: vi.fn(async () => rows.entries.length),
    bulkGetMemories: vi.fn(async () => ({ results: {}, missing: [] })),
    conflictKeys: vi.fn(async (): Promise<string[]> => []),
    listGrants: vi.fn(async () => ({ rows: [] })),
  };
});
vi.mock("../api/models", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/models")>();
  return { ...actual, getModelStatus: vi.fn(async () => ({ requires_setup: true })) };
});
vi.mock("../views/Tokens", () => ({
  // The token form itself is the Tokens page's, tested there; here only that it opens in place.
  QuickConnect: () => <div>Token form</div>,
}));

const SUBJECT = "n@x";
const NS = "personal-n";
const writer: WhoAmI = { authenticated: true, subject: SUBJECT, org: "dev", roles: ["WRITER"], namespaces: [NS] };
const personal: MyNamespaceEntry = {
  namespace: NS,
  role: "admin",
  status: "active",
  visibility: "private",
  description: "",
  joined_at: "2026-09-16T00:00:00Z",
  is_new: true,
  kind: "memory",
};
const team: MyNamespaceEntry = { ...personal, namespace: "team", role: "writer" };

const memory = (agent: string): MemoryListEntry => ({
  memory_key: "user_editor",
  memory_type: "semantic",
  confidence: 1,
  version: 1,
  created_at: "2026-09-18T09:00:00Z",
  expires_at: null,
  pinned: false,
  tags: [],
  agent,
  value: "uses vim",
});

const token = (over: Partial<TokenEntry> = {}): TokenEntry => ({
  name: "laptop",
  sa_subject: "sa:laptop",
  namespaces: ["*"],
  roles: ["writer"],
  ttl_days: 90,
  created_by: SUBJECT,
  created_at: "2026-09-18T09:00:00",
  expires_at: null,
  revoked_at: null,
  revoked_by: "",
  last_used_at: null,
  yours: true,
  ...over,
});

function seed(mine: MyNamespaceEntry[] | null, who: WhoAmI = writer, namespace = NS) {
  useStore.setState({
    authed: true,
    whoami: who,
    myNamespaces: mine,
    identity: { org: "dev", namespace, agent: "loom-ui" },
    view: "memories",
    memoriesVersion: 0,
  });
}

const checklist = () => screen.queryByRole("region", { name: "Getting started" });
const WAIT = { timeout: 5000 };

beforeEach(() => {
  localStorage.clear();
  resetNamespaceRoleCache();
  auth.off = false;
  rows.entries = [];
  tokens.list = [];
});
afterEach(cleanup);

describe("GettingStarted on Memories", () => {
  it("replaces the empty state for a new account, says what Loom is, and passes axe", async () => {
    seed([personal]);
    const { container } = render(<Recent />);
    const panel = await screen.findByRole("region", { name: "Getting started" }, WAIT);
    expect(within(panel).getByText(/Loom keeps memories and documents for your AI agents/)).toBeInTheDocument();
    expect(within(panel).getByRole("heading", { name: /Connect an agent/ })).toBeInTheDocument();
    expect(within(panel).getByRole("heading", { name: /First memory/ })).toBeInTheDocument();
    expect(screen.queryByText("No memories yet")).toBeNull();
    expect(await axeViolations(container)).toEqual([]);
  });

  it("is not shown to an account that already belongs to another namespace", async () => {
    seed([personal, team]);
    render(<Recent />);
    expect(await screen.findByText("No memories yet", undefined, WAIT)).toBeInTheDocument();
    expect(checklist()).toBeNull();
  });

  it("is not shown to a viewer, who cannot connect a writing agent here", async () => {
    seed([{ ...personal, role: "viewer" }], { ...writer, roles: ["VIEWER"] });
    render(<Recent />);
    expect(await screen.findByText("No memories yet", undefined, WAIT)).toBeInTheDocument();
    expect(checklist()).toBeNull();
  });

  it("opens the token form in place, not on another page", async () => {
    seed([personal]);
    render(<Recent />);
    const panel = await screen.findByRole("region", { name: "Getting started" }, WAIT);
    await userEvent.click(within(panel).getByRole("button", { name: "Create a token" }));
    expect(await within(panel).findByText("Token form", undefined, WAIT)).toBeInTheDocument();
    expect(useStore.getState().view).toBe("memories");
  });

  it("waits on a token of mine that has made no call, and ticks once it has", async () => {
    tokens.list = [token()];
    seed([personal]);
    const first = render(<Recent />);
    const panel = await screen.findByRole("region", { name: "Getting started" }, WAIT);
    expect(
      await within(panel).findByText(/Waiting for your agent's first memory/, undefined, WAIT),
    ).toBeInTheDocument();
    expect(within(panel).getByText("0 of 2 done")).toBeInTheDocument();
    first.unmount();
    tokens.list = [token({ last_used_at: "2026-09-18T09:05:00" })];
    render(<Recent />);
    expect(await screen.findByText("An agent is saving memories here.", undefined, WAIT)).toBeInTheDocument();
  });

  it("ticks both steps when an agent's first memory arrives, in place", async () => {
    seed([personal]);
    render(<Recent />);
    await screen.findByRole("region", { name: "Getting started" }, WAIT);
    rows.entries = [memory("claude-code")];
    act(() => useStore.getState().bumpMemoriesVersion());
    const panel = await screen.findByRole("region", { name: "Getting started" }, WAIT);
    expect(await within(panel).findByText("2 of 2 done", undefined, WAIT)).toBeInTheDocument();
    expect(within(panel).getByText(/All set: your agent is saving memories here/)).toBeInTheDocument();
  });

  it("does not count the dashboard's own memory as an agent connecting", async () => {
    seed([personal]);
    render(<Recent />);
    await screen.findByRole("region", { name: "Getting started" }, WAIT);
    rows.entries = [memory("loom-ui")];
    act(() => useStore.getState().bumpMemoriesVersion());
    const panel = await screen.findByRole("region", { name: "Getting started" }, WAIT);
    expect(await within(panel).findByText("1 of 2 done", undefined, WAIT)).toBeInTheDocument();
  });

  it("is one line under the toolbar on a namespace with memories, and Continue opens it", async () => {
    localStorage.setItem(
      onboardingKey(SUBJECT, `dev/${NS}`),
      JSON.stringify({ done: {}, started: true, dismissed: false }),
    );
    rows.entries = [memory("loom-ui")];
    seed([personal]);
    render(<Recent />);
    const bar = await screen.findByTestId("getting-started-bar", {}, WAIT);
    expect(within(bar).getByText(/1 of 2 set up/)).toBeInTheDocument();
    await userEvent.click(within(bar).getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("region", { name: "Getting started" }, WAIT)).toBeInTheDocument();
    // The bar it was pressed in is gone: focus is on the checklist's heading.
    expect(screen.getByRole("heading", { name: "Getting started" })).toHaveFocus();
  });

  it("names the bar's Hide for what it hides, and Hide hands focus to the search", async () => {
    localStorage.setItem(
      onboardingKey(SUBJECT, `dev/${NS}`),
      JSON.stringify({ done: {}, started: true, dismissed: false }),
    );
    rows.entries = [memory("loom-ui")];
    seed([personal]);
    render(<Recent />);
    const bar = await screen.findByTestId("getting-started-bar", {}, WAIT);
    await userEvent.click(within(bar).getByRole("button", { name: "Hide getting started" }));
    await waitFor(() => expect(screen.queryByTestId("getting-started-bar")).toBeNull());
    expect(document.activeElement?.tagName).toBe("INPUT");
  });

  it("offers a new token, not a wait, when the only token of mine has expired", async () => {
    tokens.list = [token({ expires_at: "2020-01-01T00:00:00" })];
    seed([personal]);
    render(<Recent />);
    const panel = await screen.findByRole("region", { name: "Getting started" }, WAIT);
    expect(await within(panel).findByRole("button", { name: "Create a token" }, WAIT)).toBeInTheDocument();
    expect(within(panel).queryByText(/Waiting for/)).toBeNull();
  });

  it("shows no models row to an account that cannot set the models", async () => {
    seed([personal]);
    render(<Recent />);
    const panel = await screen.findByRole("region", { name: "Getting started" }, WAIT);
    expect(within(panel).getAllByRole("listitem")).toHaveLength(2);
    expect(within(panel).queryByRole("button", { name: "Open Models & keys" })).toBeNull();
  });

  it("starts another namespace's checklist as its own, with no token form left open", async () => {
    seed([personal]);
    render(<Recent />);
    const panel = await screen.findByRole("region", { name: "Getting started" }, WAIT);
    await userEvent.click(within(panel).getByRole("button", { name: "Create a token" }));
    expect(await within(panel).findByText("Token form", undefined, WAIT)).toBeInTheDocument();
    const other = { ...personal, namespace: "personal-other" };
    act(() =>
      useStore.setState({
        myNamespaces: [other],
        identity: { org: "dev", namespace: other.namespace, agent: "loom-ui" },
      }),
    );
    await waitFor(() => expect(screen.queryByText("Token form")).toBeNull(), WAIT);
    const next = await screen.findByRole("region", { name: "Getting started" }, WAIT);
    expect(within(next).getByRole("button", { name: "Create a token" })).toBeInTheDocument();
  });

  it("stays hidden once hidden, for that namespace only", async () => {
    seed([personal]);
    const first = render(<Recent />);
    const panel = await screen.findByRole("region", { name: "Getting started" }, WAIT);
    await userEvent.click(within(panel).getByRole("button", { name: "Hide" }));
    await waitFor(() => expect(checklist()).toBeNull());
    first.unmount();
    render(<Recent />);
    expect(await screen.findByText("No memories yet", undefined, WAIT)).toBeInTheDocument();
    expect(checklist()).toBeNull();
    expect(JSON.parse(localStorage.getItem(onboardingKey(SUBJECT, `dev/${NS}`)) ?? "{}")).toMatchObject({
      dismissed: true,
    });
    // Another new namespace still shows its own.
    const other = { ...personal, namespace: "personal-other" };
    act(() =>
      useStore.setState({
        myNamespaces: [other],
        identity: { org: "dev", namespace: other.namespace, agent: "loom-ui" },
      }),
    );
    await waitFor(() => expect(checklist()).not.toBeNull(), WAIT);
  });

  it("starts no checklist in a namespace with memories reached by a switch from a new one", async () => {
    const bob = { ...personal, namespace: "personal-bob" };
    seed([personal, bob]);
    render(<Recent />);
    await screen.findByRole("region", { name: "Getting started" }, WAIT);
    rows.entries = [memory("claude-code")];
    act(() => useStore.setState({ identity: { org: "dev", namespace: bob.namespace, agent: "loom-ui" } }));
    expect(await screen.findByText("uses vim", undefined, WAIT)).toBeInTheDocument();
    // The last namespace's empty page made nothing of this one new.
    expect(checklist()).toBeNull();
    expect(screen.queryByTestId("getting-started-bar")).toBeNull();
    expect(localStorage.getItem(onboardingKey(SUBJECT, `dev/${bob.namespace}`))).toBeNull();
  });

  it("is the one-line bar, not the full list, after a switch to a started namespace with memories", async () => {
    const bob = { ...personal, namespace: "personal-bob" };
    localStorage.setItem(
      onboardingKey(SUBJECT, `dev/${bob.namespace}`),
      JSON.stringify({ done: { write: true }, started: true, dismissed: false }),
    );
    seed([personal, bob]);
    render(<Recent />);
    await screen.findByRole("region", { name: "Getting started" }, WAIT);
    rows.entries = [memory("loom-ui")];
    act(() => useStore.setState({ identity: { org: "dev", namespace: bob.namespace, agent: "loom-ui" } }));
    // Once this namespace's rows are read, its started checklist is the bar.
    expect(await screen.findByText("uses vim", undefined, WAIT)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByTestId("getting-started-bar")).not.toBeNull(), WAIT);
    expect(checklist()).toBeNull();
  });

  it("does not come back on a visit after every step was done", async () => {
    localStorage.setItem(
      onboardingKey(SUBJECT, `dev/${NS}`),
      JSON.stringify({ done: { connect: true, write: true }, started: true, dismissed: false }),
    );
    rows.entries = [memory("claude-code")];
    seed([personal]);
    render(<Recent />);
    await screen.findByText("uses vim", undefined, WAIT);
    expect(checklist()).toBeNull();
    expect(screen.queryByTestId("getting-started-bar")).toBeNull();
  });

  it("with auth off shows the plugin setup that needs no token, and the models step", async () => {
    auth.off = true;
    seed(null, { ...writer, subject: "local", roles: ["PLATFORM_ADMIN"] });
    render(<Recent />);
    const panel = await screen.findByRole("region", { name: "Getting started" }, WAIT);
    expect(within(panel).getByText(/claude plugin install loom@loom --config server_url=/)).toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "Create a token" })).toBeNull();
    await userEvent.click(await within(panel).findByRole("button", { name: "Open Models & keys" }, WAIT));
    expect(useStore.getState().view).toBe("setup");
    expect(new URLSearchParams(window.location.search).get("tab")).toBe("models");
  });
});
