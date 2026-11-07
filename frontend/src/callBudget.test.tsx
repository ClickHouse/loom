// A per-page call budget.
//
// Every dashboard page renders here against a stubbed network; the test
// records each POST /v1/<op> the page issues while loading and fails when a
// page asks the same question twice or exceeds its budget. A new 30-second
// poll, a per-row fetch or a duplicate read cannot land unnoticed. Budgets are the calls
// a page NEEDS on load — raise one only with a reason in the commit message, never to
// make a test pass.
import { act, cleanup, render } from "./test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry, WhoAmI } from "./api";
import { useStore, type View } from "./lib/state";
import { resetNamespaceRoleCache } from "./lib/namespaceRole";
import { invalidateShared } from "./lib/sharedReads";
import { Setup } from "./views/Setup";
import { Sharing } from "./views/Sharing";
import { Recent } from "./views/Recent";
import { Documents } from "./views/Documents";
import { CodeView } from "./views/Code";
import { Tasks } from "./views/Tasks";
import { Sidebar } from "./layout/Sidebar";
import { Header } from "./layout/Header";

const ORG = "acme";
const NS = "team";
const whoami: WhoAmI = { authenticated: true, subject: "alice", org: ORG, roles: ["ADMIN"], namespaces: [NS] };
const mine: MyNamespaceEntry[] = [
  {
    namespace: NS,
    role: "admin",
    status: "active",
    visibility: "private",
    description: "",
    joined_at: null,
    is_new: false,
    kind: "memory",
  },
  {
    namespace: "code-x",
    role: "admin",
    status: "active",
    visibility: "private",
    description: "",
    joined_at: null,
    is_new: false,
    kind: "code",
  },
];

// A generic body that satisfies every list-shaped reader.
const GENERIC = {
  rows: [],
  entries: [],
  repos: [],
  results: {},
  missing: [],
  total: 0,
  next_cursor: null,
  steps: [],
  status: "ok",
  problems: [],
  namespaces: [],
  agents: [],
  totals: { namespaces: 0, agents: 0, cross_namespace_recalls: 0, writes: 0, recalls: 0 },
  // One inbox, so the Tasks page goes on to read its tasks
  inboxes: [
    {
      org: "acme",
      inbox: "team-bot",
      owner_agent: "bot",
      owner_namespace: "team",
      description: "",
      posters: [],
      accept_disclosure: ["internal"],
      default_lease_seconds: 1800,
      max_attempts: 3,
      max_defers: 5,
      triage_escalate_after_seconds: 14400,
      webhook_url: "",
      created_by: "",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      version: 1,
    },
  ],
  tasks: [],
  counts: [],
};

function stubNetwork(): () => Map<string, number> {
  const calls = new Map<string, number>();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      const path = url.replace(/^https?:\/\/[^/]+/, "").split("?")[0];
      calls.set(path, (calls.get(path) ?? 0) + 1);
      return new Response(JSON.stringify(GENERIC), { status: 200, headers: { "Content-Type": "application/json" } });
    }),
  );
  return () => calls;
}

function seed(view: View) {
  useStore.setState({
    authed: true,
    whoami,
    myNamespaces: mine,
    identity: { org: ORG, namespace: NS, agent: "loom-ui" },
    view,
    memoriesVersion: 0,
    paletteRequest: null,
    paletteOpens: 0,
  });
}

async function settle() {
  // three macrotasks: effects, their fetches, and the state they set
  for (let i = 0; i < 3; i++)
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30));
    });
}

/** page → the POST paths it may issue on load, and how many times each. */
const BUDGET: Record<string, { render: () => JSX.Element; view: View; allow: Record<string, number> }> = {
  Setup: {
    render: () => <Setup />,
    view: "setup",
    allow: {
      "/v1/me/service_accounts": 1,
      "/v1/memory.list": 1,
      "/v1/namespace.list_mine": 1,
      "/v1/code.repos": 1,
      "/v1/blacklist.list": 1,
    },
  },
  Sharing: {
    render: () => <Sharing />,
    view: "sharing",
    allow: { "/v1/namespace.list_mine": 1, "/v1/code.repos": 1, "/v1/me/sharing-summary": 1 },
  },
  Memories: {
    render: () => <Recent />,
    view: "memories",
    // The header's Guide button reads the guide once.
    allow: { "/v1/memory.list": 1, "/v1/conflicts.list": 1, "/v1/namespace.guide_get": 1 },
  },
  Documents: {
    render: () => <Documents />,
    view: "knowledge",
    // The select above the list reads the collections the person can open, once.
    allow: { "/v1/knowledge.list": 1, "/v1/collection.list": 1 },
  },
  Repositories: {
    render: () => <CodeView />,
    view: "code",
    // Health says whether the page can add repositories; the problems banner shares the read.
    allow: { "/v1/code.repos": 1, "/v1/tracker.source.list": 1, "/v1/health": 1 },
  },
  // The Tasks page reads the namespace's inboxes once, their counts once (the rail's "N
  // to do · M running" and the tiles need no call of their own), and the picked inbox's
  // tasks once (the list's projection). The panel's reads happen on a click.
  Tasks: {
    render: () => <Tasks />,
    view: "tasks",
    allow: { "/v1/inbox.list": 1, "/v1/task.counts": 1, "/v1/task.list": 1 },
  },
  "Sidebar + Header (every page)": {
    render: () => (
      <>
        <Sidebar />
        <Header />
      </>
    ),
    view: "memories",
    // The task inbox bell asks for counts once per two minutes.
    allow: {
      "/v1/conflicts.list": 1,
      // Fifty retirement records: whether the Review queue's Auto-resolved tab has anything.
      "/v1/memory.auto_retirements": 1,
      // One proposal: whether an agent's proposal waits in the Review queue.
      "/v1/memory.proposals": 1,
      "/v1/cost.budget_status": 1,
      "/v1/agents.connected": 1,
      "/v1/grants.pending_for_me": 1,
      "/v1/code.repos": 1,
      "/v1/health": 1,
      "/v1/task.counts": 1,
    },
  },
};

describe("call budget: each page asks each question once on load", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    resetNamespaceRoleCache();
    invalidateShared();
    vi.spyOn(console, "error").mockImplementation(() => {});
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null });
  });

  for (const [page, spec] of Object.entries(BUDGET)) {
    it(`${page}`, async () => {
      const calls = stubNetwork();
      seed(spec.view);
      render(spec.render());
      await settle();
      const seen = Object.fromEntries(Array.from(calls().entries()).filter(([p]) => p.startsWith("/v1/")));
      const over: string[] = [];
      for (const [path, n] of Object.entries(seen)) {
        const cap = spec.allow[path];
        if (cap === undefined) over.push(`${path} ×${n} (not in this page's budget)`);
        else if (n > cap) over.push(`${path} ×${n} (budget ${cap})`);
      }
      expect(over, `${page} exceeded its call budget:\n  ${over.join("\n  ")}\nseen: ${JSON.stringify(seen)}`).toEqual(
        [],
      );
    });
  }
});
