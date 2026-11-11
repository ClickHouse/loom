// Boot order.
//
// The request that fills the page must not wait on three serial network stages (whoami,
// then list_mine, then the view mounting and reading). This test boots the whole App on a
// deep link against a stubbed network where list_mine never answers until told, and
// asserts that
//   - list_mine is requested before whoami answers (concurrent start),
//   - the view's chunk is preloaded for the URL before any answer,
//   - the page's first data read leaves while list_mine is still pending,
//   - the late list_mine answer does not replay the page's reads.
import { act, cleanup, fireEvent, render, screen } from "./test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { preloadView } = vi.hoisted(() => ({ preloadView: vi.fn() }));
vi.mock("./views/registry", () => ({ preloadView, VIEW_LOADERS: {} }));
vi.mock("./lib/navigation", () => ({ goToSso: vi.fn(), reloadPage: vi.fn() }));

import App from "./App";
import { goToSso, reloadPage } from "./lib/navigation";
import { useStore } from "./lib/state";
import { resetNamespaceRoleCache } from "./lib/namespaceRole";
import { invalidateShared } from "./lib/sharedReads";
import { claimsFromToken } from "./lib/boot";

const ORG = "acme";
const NS = "team";
const GENERIC = {
  rows: [],
  entries: [],
  repos: [],
  results: {},
  missing: [],
  total: 0,
  today: 0,
  truncated: false,
  next_cursor: null,
  steps: [],
  status: "ok",
  problems: [],
  namespaces: [],
  agents: [],
  count: 0,
  totals: { namespaces: 0, agents: 0, cross_namespace_recalls: 0, writes: 0, recalls: 0 },
};
const MINE = [
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
];

function jwt(payload: Record<string, unknown>): string {
  const b64 = (s: string) =>
    btoa(String.fromCharCode(...new TextEncoder().encode(s)))
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
  return `${b64(JSON.stringify({ alg: "HS256" }))}.${b64(JSON.stringify(payload))}.sig`;
}

/** The mark the sign-in gate writes as this tab leaves for SSO; its nonce comes back in the
 *  landing's query, `/me?n=<nonce>#token=…`. */
const TAB_NONCE = "tab-nonce-0123456789abcdef";
function markSignIn(at = Date.now()): string {
  sessionStorage.setItem("loom_sign_in_pending_v1", JSON.stringify({ nonce: TAB_NONCE, at }));
  return `?n=${TAB_NONCE}`;
}

type Deferred = { resolve: (r: Response) => void; promise: Promise<Response> };
function deferred(): Deferred {
  let resolve!: (r: Response) => void;
  const promise = new Promise<Response>((r) => {
    resolve = r;
  });
  return { resolve, promise };
}
const json = (body: unknown) =>
  new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });

async function settle(rounds = 3) {
  for (let i = 0; i < rounds; i++)
    await act(async () => {
      await new Promise((r) => setTimeout(r, 25));
    });
}

/** Wait for a condition instead of a fixed number of ticks: the lazy view
 *  chunk and the effects behind it take longer on a loaded CI runner than
 *  on a laptop.
 *  The held-back promises still guarantee what the test is about — a gated
 *  read cannot have answered while the condition is awaited. */
async function until(cond: () => boolean, timeoutMs = 5000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!cond()) {
    if (Date.now() > deadline) throw new Error("condition not met in time");
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
  }
  await settle(1);
}

describe("boot order", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    invalidateShared();
    resetNamespaceRoleCache();
    useStore.setState({ authed: true, whoami: null, myNamespaces: null, identity: null, view: "setup" });
    // jsdom has no scrollTo; the shell resets the main scroll on a view change
    Element.prototype.scrollTo = vi.fn();
    window.history.replaceState(null, "", "/memories");
    localStorage.setItem("loom_token_v1", jwt({ sub: "alice", org: ORG, namespaces: [NS], roles: ["ADMIN"] }));
    // a returning visitor: the last membership answer said `team` is a memory namespace
    localStorage.setItem("loom_kinds_v1", JSON.stringify({ [NS]: "memory", joined: "memory" }));
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null });
    window.history.replaceState(null, "", "/");
  });

  it("starts list_mine beside whoami, preloads the URL's view, and reads the page before list_mine answers", async () => {
    const order: string[] = [];
    const whoamiGate = deferred();
    const mineGate = deferred();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        const path = url
          .replace(/^https?:\/\/[^/]+/, "")
          .split("?")[0]
          .replace("/v1/", "");
        order.push(path);
        if (path === "whoami") return whoamiGate.promise;
        if (path === "namespace.list_mine") return mineGate.promise;
        return json(GENERIC);
      }),
    );

    render(<App />);
    await until(() => order.includes("whoami") && order.includes("namespace.list_mine"));

    // Stage 0: both boot reads left together; the chunk was asked for at once.
    expect(order).toContain("whoami");
    expect(order).toContain("namespace.list_mine");
    expect(preloadView).toHaveBeenCalledWith("memories");
    expect(order.filter((p) => p === "namespace.list_mine")).toHaveLength(1);

    // whoami confirms; list_mine is still pending.
    whoamiGate.resolve(json({ authenticated: true, subject: "alice", org: ORG, namespaces: [NS], roles: ["ADMIN"] }));
    // list_mine is still gated, so whenever memory.list appears it was issued
    // before the membership answer — the timing of the lazy chunk does not matter.
    await until(() => order.includes("memory.list"));

    // Stage 1: the shell painted on the claimed namespace and the page's own
    // read left — without waiting for the membership list.
    expect(useStore.getState().identity).toEqual({ org: ORG, namespace: NS, agent: "loom-ui" });
    expect(useStore.getState().view).toBe("memories");
    const before = order.filter((p) => p === "memory.list").length;
    expect(before).toBeGreaterThanOrEqual(1);
    expect(order.filter((p) => p === "namespace.list_mine")).toHaveLength(1);

    // Stage 2: the membership answer agrees, so nothing is replayed.
    const identityBefore = useStore.getState().identity;
    mineGate.resolve(json({ ...GENERIC, rows: MINE }));
    await until(() => useStore.getState().myNamespaces !== null);
    await settle(2);
    expect(useStore.getState().myNamespaces).toEqual(MINE);
    expect(useStore.getState().identity).toBe(identityBefore);
    expect(order.filter((p) => p === "memory.list").length).toBe(before);
    expect(order.filter((p) => p === "namespace.list_mine")).toHaveLength(1);
  });

  it("waits for membership on a first visit, when no kind is remembered", async () => {
    localStorage.removeItem("loom_kinds_v1");
    const mineGate = deferred();
    const order: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        const path = url
          .replace(/^https?:\/\/[^/]+/, "")
          .split("?")[0]
          .replace("/v1/", "");
        order.push(path);
        if (path === "whoami")
          return json({ authenticated: true, subject: "alice", org: ORG, namespaces: [NS], roles: ["ADMIN"] });
        if (path === "namespace.list_mine") return mineGate.promise;
        return json(GENERIC);
      }),
    );
    render(<App />);
    await until(() => order.includes("namespace.list_mine") && useStore.getState().whoami !== null);
    await settle(3);
    // list_mine still left beside whoami and the chunk was preloaded …
    expect(order.filter((p) => p === "namespace.list_mine")).toHaveLength(1);
    expect(preloadView).toHaveBeenCalledWith("memories");
    // … but nothing painted or read on a namespace whose kind is unknown
    expect(useStore.getState().identity).toBeNull();
    expect(order.filter((p) => p === "memory.list")).toHaveLength(0);
    mineGate.resolve(json({ ...GENERIC, rows: MINE }));
    await until(() => order.includes("memory.list"));
    expect(useStore.getState().identity?.namespace).toBe(NS);
    expect(order.filter((p) => p === "memory.list").length).toBeGreaterThanOrEqual(1);
    expect(JSON.parse(localStorage.getItem("loom_kinds_v1") || "{}")).toEqual({ [NS]: "memory" });
  });

  it("records pending requests under the whoami subject's cache key", async () => {
    // The key is persisted in users' browsers; the literal is the format as shipped
    // (Browse writes the same key when a join goes pending).
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        const path = url
          .replace(/^https?:\/\/[^/]+/, "")
          .split("?")[0]
          .replace("/v1/", "");
        if (path === "whoami")
          return json({ authenticated: true, subject: "alice", org: ORG, namespaces: [NS], roles: ["ADMIN"] });
        if (path === "namespace.list_mine")
          return json({ ...GENERIC, rows: [{ ...MINE[0], role: "viewer", pending_role: "writer" }] });
        return json(GENERIC);
      }),
    );
    render(<App />);
    await until(() => localStorage.getItem("loom_pending_v1_alice") !== null);
    expect(localStorage.getItem("loom_pending_v1_alice")).toBe(JSON.stringify([`${NS}::writer`]));
  });

  it("corrects the namespace when membership disagrees with the claims, once", async () => {
    // Saved pick is a joined namespace the claims do not carry: the provisional
    // paint uses the first claim; list_mine then says the pick is reachable.
    localStorage.setItem("loom_namespace_v1", "joined");
    const mineGate = deferred();
    const order: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        const path = url
          .replace(/^https?:\/\/[^/]+/, "")
          .split("?")[0]
          .replace("/v1/", "");
        order.push(path);
        if (path === "whoami")
          return json({ authenticated: true, subject: "alice", org: ORG, namespaces: [NS], roles: ["ADMIN"] });
        if (path === "namespace.list_mine") return mineGate.promise;
        return json(GENERIC);
      }),
    );
    render(<App />);
    await until(() => useStore.getState().identity !== null);
    expect(useStore.getState().identity?.namespace).toBe(NS);
    mineGate.resolve(
      json({
        ...GENERIC,
        rows: [
          ...MINE,
          {
            namespace: "joined",
            role: "writer",
            status: "active",
            visibility: "private",
            description: "",
            joined_at: null,
            is_new: false,
            kind: "memory",
          },
        ],
      }),
    );
    await until(() => useStore.getState().identity?.namespace === "joined");
    expect(useStore.getState().identity?.namespace).toBe("joined");
  });

  it("opens a code-only account's Library on no namespace, and there the collection listed", async () => {
    const CODE = "code-repo";
    localStorage.setItem("loom_token_v1", jwt({ sub: "alice", org: ORG, namespaces: [CODE], roles: ["ADMIN"] }));
    localStorage.setItem("loom_kinds_v1", JSON.stringify({ [CODE]: "code" }));
    window.history.replaceState(null, "", "/library");
    const order: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        const path = url
          .replace(/^https?:\/\/[^/]+/, "")
          .split("?")[0]
          .replace("/v1/", "");
        order.push(path);
        if (path === "whoami")
          return json({ authenticated: true, subject: "alice", org: ORG, namespaces: [CODE], roles: ["ADMIN"] });
        if (path === "namespace.list_mine")
          return json({ ...GENERIC, rows: [{ ...MINE[0], namespace: CODE, kind: "code" }] });
        if (path === "collection.list")
          return json({
            collections: [{ namespace: "col-q3", parent: "", personal: false, role: "writer", description: "" }],
            next_cursor: null,
          });
        return json(GENERIC);
      }),
    );
    render(<App />);
    await until(() => order.includes("collection.list"));
    // No memory namespace to show: the Library opens the collection listed instead.
    await until(() => new URLSearchParams(window.location.search).get("collection") === "col-q3");
    await until(() => order.includes("knowledge.list"));
    const reads = vi.mocked(fetch).mock.calls.filter(([u]) => String(u).includes("knowledge.list"));
    for (const [, init] of reads) expect(JSON.parse(String(init?.body)).namespace).toBe("col-q3");
  });
});

// A link names its namespace (`?namespace=`): one the account can read opens for this tab
// and is never saved as the pick; one it cannot read shows a page saying so, over the
// namespace a visit without the link opens, and nothing is read there or saved.
describe("a link's namespace", () => {
  const JOINED = { ...MINE[0], namespace: "joined", role: "writer" };
  let calls: { path: string; body: Record<string, unknown> | null }[];
  function serve(rows: unknown[] | "failing") {
    calls = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        const path = url
          .replace(/^https?:\/\/[^/]+/, "")
          .split("?")[0]
          .replace("/v1/", "");
        calls.push({ path, body: typeof init?.body === "string" ? JSON.parse(init.body) : null });
        if (path === "whoami")
          return json({ authenticated: true, subject: "alice", org: ORG, namespaces: [NS], roles: ["ADMIN"] });
        if (path === "namespace.list_mine")
          return rows === "failing" ? new Response("{}", { status: 500 }) : json({ ...GENERIC, rows });
        return json(GENERIC);
      }),
    );
  }
  const address = () => window.location.pathname + window.location.search;
  const savedDefault = () => calls.some((c) => c.path === "me/set-default-namespace");

  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    invalidateShared();
    resetNamespaceRoleCache();
    useStore.setState({
      authed: true,
      whoami: null,
      myNamespaces: null,
      identity: null,
      deniedNamespace: null,
      view: "setup",
    });
    Element.prototype.scrollTo = vi.fn();
    localStorage.setItem("loom_token_v1", jwt({ sub: "alice", org: ORG, namespaces: [NS], roles: ["ADMIN"] }));
    localStorage.setItem("loom_namespace_v1", NS);
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null, deniedNamespace: null });
    window.history.replaceState(null, "", "/");
  });

  it("opens a namespace the account can read for this tab, and saves nothing", async () => {
    window.history.replaceState(null, "", "/memories?namespace=joined");
    serve([...MINE, JOINED]);
    render(<App />);
    await until(() => calls.some((c) => c.path === "memory.list"));
    expect(useStore.getState()).toMatchObject({ identity: { namespace: "joined" }, deniedNamespace: null });
    expect(address()).toBe("/memories?namespace=joined");
    expect(calls.filter((c) => c.path === "memory.list").every((c) => c.body?.namespace === "joined")).toBe(true);
    expect(localStorage.getItem("loom_namespace_v1")).toBe(NS);
    expect(savedDefault()).toBe(false);
  });

  it("opens a Library link to a collection on the reader's own namespace, though the sender's denies them", async () => {
    window.history.replaceState(null, "", "/library?namespace=group-b-shared&collection=col-q3");
    serve(MINE);
    // The reader holds a grant on the collection itself.
    const serveRest = vi.mocked(fetch).getMockImplementation();
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/v1/collection.list"))
        return json({
          collections: [{ namespace: "col-q3", parent: "", personal: false, role: "viewer", description: "" }],
          next_cursor: null,
        });
      return serveRest ? serveRest(input, init) : json(GENERIC);
    });
    render(<App />);
    await until(() => useStore.getState().identity !== null && useStore.getState().view === "knowledge");
    expect(useStore.getState()).toMatchObject({ identity: { namespace: NS }, deniedNamespace: null });
    expect(screen.queryByRole("heading", { level: 1, name: "No access to group-b-shared" })).toBeNull();
    // The Library then asks collection.list whether the reader can open the collection.
    await until(() => vi.mocked(fetch).mock.calls.some(([u]) => String(u).includes("/v1/collection.list")));
    await until(() =>
      vi
        .mocked(fetch)
        .mock.calls.some(([u, init]) => String(u).includes("knowledge.list") && String(init?.body).includes("col-q3")),
    );
    expect(new URLSearchParams(window.location.search).get("collection")).toBe("col-q3");
    expect(new URLSearchParams(window.location.search).get("namespace")).toBe(NS);
  });

  it("opens a document link whose namespace is its collection on that document", async () => {
    window.history.replaceState(null, "", "/library?namespace=col-q3&doc=kb%3A%2F%2Fq3%2Fplan.md");
    serve([...MINE, { ...MINE[0], namespace: "col-q3", role: "viewer", kind: "collection" }]);
    const serveRest = vi.mocked(fetch).getMockImplementation();
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/v1/collection.list"))
        return json({
          collections: [{ namespace: "col-q3", parent: "", personal: false, role: "viewer", description: "" }],
          next_cursor: null,
        });
      return serveRest ? serveRest(input, init) : json(GENERIC);
    });
    render(<App />);
    await until(() => useStore.getState().identity !== null && useStore.getState().view === "knowledge");
    expect(new URLSearchParams(window.location.search).get("collection")).toBe("col-q3");
    // The Library reads that document in that collection (this fake server has no such
    // document, so the page then says it is gone).
    await until(() =>
      vi
        .mocked(fetch)
        .mock.calls.some(
          ([, init]) => String(init?.body).includes("kb://q3/plan.md") && String(init?.body).includes("col-q3"),
        ),
    );
  });

  it("opens a memory link whose namespace is a collection in the Library, on that collection", async () => {
    window.history.replaceState(null, "", "/memories?namespace=col-q3&memory=k");
    serve([...MINE, { ...MINE[0], namespace: "col-q3", role: "viewer", kind: "collection" }]);
    const serveRest = vi.mocked(fetch).getMockImplementation();
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/v1/collection.list"))
        return json({
          collections: [{ namespace: "col-q3", parent: "", personal: false, role: "viewer", description: "" }],
          next_cursor: null,
        });
      return serveRest ? serveRest(input, init) : json(GENERIC);
    });
    render(<App />);
    await until(() => useStore.getState().identity !== null && useStore.getState().view === "knowledge");
    // The header keeps a namespace; the collection is the Library's.
    expect(useStore.getState()).toMatchObject({ identity: { namespace: NS }, deniedNamespace: null });
    expect(window.location.pathname).toBe("/library");
    const params = new URLSearchParams(window.location.search);
    await until(() =>
      vi
        .mocked(fetch)
        .mock.calls.some(([u, init]) => String(u).includes("knowledge.list") && String(init?.body).includes("col-q3")),
    );
    expect(params.get("collection")).toBe("col-q3");
    expect(params.get("namespace")).toBe(NS);
    expect(params.get("memory")).toBeNull();
    expect(calls.some((c) => c.path === "memory.list" || c.path === "memory.get")).toBe(false);
  });

  it("lands an account whose own list names a collection on the Library, without the collection read", async () => {
    window.history.replaceState(null, "", "/");
    localStorage.removeItem("loom_namespace_v1");
    localStorage.setItem("loom_token_v1", jwt({ sub: "alice", org: ORG, namespaces: [], roles: ["WRITER"] }));
    calls = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        const path = url
          .replace(/^https?:\/\/[^/]+/, "")
          .split("?")[0]
          .replace("/v1/", "");
        calls.push({ path, body: null });
        if (path === "whoami")
          return json({ authenticated: true, subject: "alice", org: ORG, namespaces: [], roles: ["WRITER"] });
        if (path === "namespace.list_mine")
          return json({ ...GENERIC, rows: [{ ...MINE[0], namespace: "col-a", role: "viewer", kind: "collection" }] });
        // Even a failing collection read changes nothing: list_mine already said.
        if (path === "collection.list") return new Response("{}", { status: 500 });
        return json(GENERIC);
      }),
    );
    render(<App />);
    await until(() => useStore.getState().view === "knowledge");
    expect(useStore.getState().identity?.namespace).toBe("");
    expect(screen.queryByText(/can reach no namespace yet/)).toBeNull();
  });

  it("lands an account that reads only shared collections on the Library, with no toast to join", async () => {
    window.history.replaceState(null, "", "/");
    localStorage.removeItem("loom_namespace_v1");
    localStorage.setItem("loom_token_v1", jwt({ sub: "alice", org: ORG, namespaces: [], roles: ["WRITER"] }));
    calls = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        const path = url
          .replace(/^https?:\/\/[^/]+/, "")
          .split("?")[0]
          .replace("/v1/", "");
        calls.push({ path, body: null });
        if (path === "whoami")
          return json({ authenticated: true, subject: "alice", org: ORG, namespaces: [], roles: ["WRITER"] });
        if (path === "namespace.list_mine") return json({ ...GENERIC, rows: [] });
        if (path === "collection.list")
          return json({
            collections: [{ namespace: "col-research-0123456789abcdef", parent: "", personal: false, role: "viewer" }],
            next_cursor: null,
          });
        return json(GENERIC);
      }),
    );
    render(<App />);
    await until(() => useStore.getState().view === "knowledge");
    expect(useStore.getState().identity?.namespace).toBe("");
    expect(screen.queryByText(/can reach no namespace yet/)).toBeNull();
    // The Library opens the collection by itself; the namespace menu holds nothing to send to.
    await until(
      () => new URLSearchParams(window.location.search).get("collection") === "col-research-0123456789abcdef",
    );
    expect(screen.queryByRole("button", { name: "Open the namespace menu" })).toBeNull();
  });

  it("lands an account whose only collection is its own on Setup", async () => {
    window.history.replaceState(null, "", "/");
    localStorage.removeItem("loom_namespace_v1");
    localStorage.setItem("loom_token_v1", jwt({ sub: "alice", org: ORG, namespaces: [], roles: ["WRITER"] }));
    calls = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        const path = url
          .replace(/^https?:\/\/[^/]+/, "")
          .split("?")[0]
          .replace("/v1/", "");
        calls.push({ path, body: null });
        if (path === "whoami")
          return json({ authenticated: true, subject: "alice", org: ORG, namespaces: [], roles: ["WRITER"] });
        if (path === "namespace.list_mine") return json({ ...GENERIC, rows: [] });
        if (path === "collection.list")
          return json({
            collections: [{ namespace: "col-me-0123456789abcdef", parent: "", personal: true, role: "admin" }],
            next_cursor: null,
          });
        return json(GENERIC);
      }),
    );
    render(<App />);
    // The no-namespace toast comes only after the collection read decides.
    await until(() => screen.queryByText(/can reach no namespace yet/) !== null);
    expect(calls.some((c) => c.path === "collection.list")).toBe(true);
    expect(useStore.getState().view).toBe("setup");
    expect(useStore.getState().identity?.namespace).toBe("");
  });

  it("still says no access when the link's collection is no collection's name", async () => {
    window.history.replaceState(null, "", "/library?namespace=group-b-shared&collection=x");
    serve(MINE);
    render(<App />);
    await until(() => screen.queryByRole("heading", { level: 1, name: "No access to group-b-shared" }) !== null);
    expect(useStore.getState().deniedNamespace).toBe("group-b-shared");
  });

  it("says it has no access to one the account cannot read, reads nothing there, and saves nothing", async () => {
    window.history.replaceState(null, "", "/memories?namespace=group-b-shared&memory=k");
    serve(MINE);
    render(<App />);
    await until(() => screen.queryByRole("heading", { level: 1, name: "No access to group-b-shared" }) !== null);
    expect(useStore.getState()).toMatchObject({
      identity: { namespace: NS },
      deniedNamespace: "group-b-shared",
      inspectedMemoryKey: null,
    });
    expect(address()).toBe("/memories?namespace=group-b-shared&memory=k");
    // The picker is there, on the namespace under the page.
    expect(screen.getByRole("button", { name: `Namespace: ${NS}` })).toBeInTheDocument();
    await until(() => calls.some((c) => c.path === "memory.auto_retirements"));
    expect(calls.some((c) => c.body?.namespace === "group-b-shared")).toBe(false);
    expect(
      calls.filter((c) => c.path.startsWith("memory.")).map((c) => ({ path: c.path, namespace: c.body?.namespace })),
    ).toEqual([{ path: "memory.auto_retirements", namespace: NS }]);
    expect(localStorage.getItem("loom_namespace_v1")).toBe(NS);
    expect(savedDefault()).toBe(false);
    // Continue: the page the link asked for, on the namespace the picker shows.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: `Continue in ${NS}` }));
    });
    await until(() => calls.some((c) => c.path === "memory.list"));
    expect(screen.queryByRole("heading", { name: /No access/ })).toBeNull();
    expect(address()).toBe(`/memories?namespace=${NS}`);
    expect(calls.filter((c) => c.path === "memory.list").every((c) => c.body?.namespace === NS)).toBe(true);
  });

  it("says when the account's request to join it is still waiting", async () => {
    window.history.replaceState(null, "", "/memories?namespace=group-c-shared");
    serve([...MINE, { ...MINE[0], namespace: "group-c-shared", role: "writer", status: "pending" }]);
    render(<App />);
    await until(() => screen.queryByRole("heading", { level: 1, name: "No access to group-c-shared" }) !== null);
    expect(
      screen.getByRole("heading", { level: 2, name: "Your request to join is waiting for approval" }),
    ).toBeTruthy();
  });

  it("takes the page from ?view= on the bare root and moves to its path", async () => {
    window.history.replaceState(null, "", `/?view=memories&namespace=${NS}`);
    serve(MINE);
    render(<App />);
    await until(() => calls.some((c) => c.path === "memory.list"));
    expect(useStore.getState().view).toBe("memories");
    expect(address()).toBe(`/memories?namespace=${NS}`);
  });

  it("when the membership read fails, a link opens no namespace the claims lack: its access could not be checked", async () => {
    window.history.replaceState(null, "", "/memories?namespace=joined&memory=k");
    serve("failing");
    render(<App />);
    await until(
      () => screen.queryByRole("heading", { level: 1, name: "Couldn't check your access to joined" }) !== null,
    );
    expect(useStore.getState()).toMatchObject({ identity: { namespace: NS }, deniedNamespace: "joined" });
    expect(screen.queryByText(/cannot read/)).toBeNull();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    await until(() => calls.some((c) => c.path === "memory.auto_retirements"));
    expect(calls.some((c) => c.body?.namespace === "joined")).toBe(false);
    expect(
      calls.filter((c) => c.path.startsWith("memory.")).map((c) => ({ path: c.path, namespace: c.body?.namespace })),
    ).toEqual([{ path: "memory.auto_retirements", namespace: NS }]);
  });

  it("when the membership read fails, a reload still opens the saved pick: the server answers its reads", async () => {
    // Every address names its namespace, so a reload of the saved pick is a link to it.
    localStorage.setItem("loom_namespace_v1", "joined");
    window.history.replaceState(null, "", "/memories?namespace=joined");
    serve("failing");
    render(<App />);
    await until(() => calls.some((c) => c.path === "memory.list"));
    expect(useStore.getState()).toMatchObject({ identity: { namespace: "joined" }, deniedNamespace: null });
    expect(calls.filter((c) => c.path === "memory.list").every((c) => c.body?.namespace === "joined")).toBe(true);
  });

  it("takes and scrubs a link's token even when the return's code fails to load, and boots on", async () => {
    vi.doMock("./lib/linkToken", () => {
      throw new Error("Failed to fetch dynamically imported module");
    });
    try {
      sessionStorage.setItem(
        "loom_sign_in_from_v1",
        JSON.stringify({ path: "/memories?namespace=joined", at: Date.now() }),
      );
      const n = markSignIn();
      localStorage.removeItem("loom_token_v1");
      useStore.setState({ authed: false });
      const token = jwt({ sub: "alice", org: ORG, namespaces: [NS], roles: ["ADMIN"] });
      window.history.replaceState(null, "", `/${n}#token=${token}`);
      serve(MINE);
      render(<App />);
      await until(() => useStore.getState().identity !== null && useStore.getState().view === "setup");
      expect(localStorage.getItem("loom_token_v1")).toBe(token);
      expect(window.location.href).not.toContain("token=");
      // No way back to the page the sign-in started on: the bare root's landing instead.
      expect(address()).toBe(`/settings?namespace=${NS}`);
      expect(screen.queryByText(/Auth bootstrap failed/)).toBeNull();
    } finally {
      vi.doUnmock("./lib/linkToken");
    }
  });

  it("a signed-in boot of the session-ended card's address drops the ask from it", async () => {
    window.__LOOM_OWN_ORIGIN = true;
    try {
      window.history.replaceState(null, "", "/me?sign_in=sso");
      serve(MINE);
      render(<App />);
      await until(() => useStore.getState().identity !== null);
      expect(window.location.search).not.toContain("sign_in");
      expect(goToSso).not.toHaveBeenCalled();
    } finally {
      delete window.__LOOM_OWN_ORIGIN;
    }
  });

  it("a move inside one view after a switch brings the address back to the page's namespace", async () => {
    window.history.replaceState(null, "", `/memories?namespace=${NS}`);
    serve(MINE);
    render(<App />);
    await until(() => calls.some((c) => c.path === "memory.list"));
    // An entry of the same page made in another namespace, with no stamp of the app's (as the
    // Graph pushes its walks), and Back onto it.
    window.history.pushState(null, "", "/memories?namespace=other&memory=k");
    await act(async () => {
      window.dispatchEvent(new PopStateEvent("popstate", { state: null }));
    });
    expect(address()).toBe(`/memories?namespace=${NS}`);
    expect(useStore.getState().identity?.namespace).toBe(NS);
  });

  for (const root of ["/", "/me"]) {
    it(`a deep link survives the sign-in redirect: the token comes back to ${root}, the page opens`, async () => {
      // The gate remembered the link before it sent the browser to the identity provider.
      sessionStorage.setItem(
        "loom_sign_in_from_v1",
        JSON.stringify({ path: "/memories?namespace=joined", at: Date.now() }),
      );
      const n = markSignIn();
      localStorage.removeItem("loom_token_v1");
      useStore.setState({ authed: false });
      window.history.replaceState(
        null,
        "",
        `${root}${n}#token=${jwt({ sub: "alice", org: ORG, namespaces: [NS], roles: ["ADMIN"] })}`,
      );
      serve([...MINE, JOINED]);
      render(<App />);
      await until(() => calls.some((c) => c.path === "memory.list"));
      expect(useStore.getState()).toMatchObject({ view: "memories", identity: { namespace: "joined" } });
      expect(address()).toBe("/memories?namespace=joined");
      expect(window.location.hash).toBe("");
      expect(sessionStorage.getItem("loom_sign_in_from_v1")).toBeNull();
      expect(localStorage.getItem("loom_token_v1")).not.toBeNull();
    });
  }
});

describe("login link", () => {
  let stored: string[];
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    useStore.setState({
      authed: false,
      gateReason: null,
      whoami: null,
      myNamespaces: null,
      identity: null,
      view: "setup",
    });
    Element.prototype.scrollTo = vi.fn();
    // whoami accepts any bearer whose payload names a subject, and answers with its claims;
    // every other call gets the generic answer.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (!String(input).includes("/v1/whoami")) return json(GENERIC);
        const bearer = new Headers(init?.headers).get("Authorization")?.replace(/^Bearer /, "") ?? "";
        const claims = claimsFromToken(bearer);
        return claims?.subject
          ? json({ authenticated: true, subject: claims.subject, org: claims.org, namespaces: claims.namespaces })
          : json({ authenticated: false });
      }),
    );
    stored = [];
    const setItem = Storage.prototype.setItem;
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, k: string, v: string) {
      if (k === "loom_token_v1") stored.push(v);
      setItem.call(this, k, v);
    });
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null });
    window.history.replaceState(null, "", "/");
  });

  it("stores nothing from a ?token= query and scrubs it from the URL", async () => {
    window.history.replaceState(null, "", "/memories?token=query.jwt.value");
    render(<App />);
    await settle();
    expect(stored).toEqual([]);
    expect(window.location.href).not.toContain("query.jwt.value");
  });

  it("stores a #token= fragment from a sign-in this tab started, without asking, and scrubs it", async () => {
    window.history.replaceState(null, "", `/memories${markSignIn()}#token=frag.jwt.value`);
    render(<App />);
    await settle();
    expect(stored).toEqual(["frag.jwt.value"]);
    expect(window.location.href).not.toContain("frag.jwt.value");
    expect(window.location.search).toBe("");
    expect(sessionStorage.getItem("loom_sign_in_pending_v1")).toBeNull();
    expect(screen.queryByText(/Sign in from a link/)).toBeNull();
  });

  const mallory = jwt({ sub: "mallory", org: ORG, namespaces: ["mallory-ns"], roles: ["ADMIN"] });
  const alice = jwt({ sub: "alice", org: ORG, namespaces: [NS], roles: ["ADMIN"] });

  it("holds a link token this tab did not ask for, unstored, and names the account the server says it is", async () => {
    localStorage.setItem("loom_token_v1", alice);
    stored = [];
    const serve = vi.mocked(fetch).getMockImplementation();
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const bearer = new Headers(init?.headers).get("Authorization");
      if (String(input).includes("/v1/whoami") && bearer === `Bearer ${mallory}`)
        return json({ authenticated: true, subject: "mallory@server", org: "server-org" });
      return serve ? serve(input, init) : json(GENERIC);
    });
    window.history.replaceState(null, "", `/settings#token=${mallory}`);
    render(<App />);
    // The server's answer, not the token's unverified claims.
    await screen.findByRole("button", { name: "Sign in as mallory@server" });
    expect(screen.getByText("server-org")).toBeTruthy();
    expect(stored).toEqual([]);
    expect(localStorage.getItem("loom_token_v1")).toBe(alice);
    expect(window.location.href).not.toContain("token=");
    expect(JSON.stringify({ ...localStorage, ...sessionStorage })).not.toContain(mallory);
  });

  it("stores the link token once the person confirms the account", async () => {
    localStorage.setItem("loom_token_v1", alice);
    stored = [];
    window.history.replaceState(null, "", `/settings#token=${mallory}`);
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Sign in as mallory" }));
    await settle();
    expect(stored).toEqual([mallory]);
    expect(screen.queryByRole("button", { name: "Sign in as mallory" })).toBeNull();
  });

  it("cancelling drops the link token and keeps the browser's own session", async () => {
    localStorage.setItem("loom_token_v1", alice);
    stored = [];
    window.history.replaceState(null, "", `/settings#token=${mallory}`);
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    await settle();
    expect(stored).toEqual([]);
    expect(screen.queryByRole("button", { name: "Sign in as mallory" })).toBeNull();
    // The link's token was only asked about; boot went on with the token the browser held.
    const sent = vi.mocked(fetch).mock.calls.map(([, init]) => new Headers(init?.headers).get("Authorization"));
    expect(sent).toContain(`Bearer ${alice}`);
    expect(localStorage.getItem("loom_token_v1")).toBe(alice);
  });

  it("drops a link token the server refuses without asking, storing nothing", async () => {
    localStorage.setItem("loom_token_v1", alice);
    stored = [];
    const forged = "eyJhbGciOiJIUzI1NiJ9.eyJub3N1YiI6MX0.forged"; // no subject: whoami refuses it
    window.history.replaceState(null, "", `/settings#token=${forged}`);
    render(<App />);
    await until(() => useStore.getState().whoami !== null);
    expect(screen.queryByText(/Sign in from a link/)).toBeNull();
    expect(stored).toEqual([]);
    expect(localStorage.getItem("loom_token_v1")).toBe(alice);
    expect(window.location.href).not.toContain("token=");
  });

  it("asks about a link whose token claims the held account when the server names another", async () => {
    localStorage.setItem("loom_token_v1", alice);
    stored = [];
    // The payload says alice; only the server's answer is verified.
    const claimsAlice = jwt({ sub: "alice", org: ORG, namespaces: [NS], roles: ["ADMIN"], iat: 5 });
    const serve = vi.mocked(fetch).getMockImplementation();
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const bearer = new Headers(init?.headers).get("Authorization");
      if (String(input).includes("/v1/whoami") && bearer === `Bearer ${claimsAlice}`)
        return json({ authenticated: true, subject: "mallory", org: ORG });
      return serve ? serve(input, init) : json(GENERIC);
    });
    window.history.replaceState(null, "", `/settings#token=${claimsAlice}`);
    render(<App />);
    await screen.findByRole("button", { name: "Sign in as mallory" });
    expect(stored).toEqual([]);
  });

  it("asks whoami again after the confirmation: a token that expired meanwhile shows the refused gate", async () => {
    localStorage.setItem("loom_token_v1", alice);
    stored = [];
    let confirmed = false;
    const serve = vi.mocked(fetch).getMockImplementation();
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const bearer = new Headers(init?.headers).get("Authorization");
      if (String(input).includes("/v1/whoami") && bearer === `Bearer ${mallory}`)
        return json(confirmed ? { authenticated: false } : { authenticated: true, subject: "mallory", org: ORG });
      return serve ? serve(input, init) : json(GENERIC);
    });
    window.history.replaceState(null, "", `/settings#token=${mallory}`);
    render(<App />);
    const button = await screen.findByRole("button", { name: "Sign in as mallory" });
    confirmed = true; // the token expires while the person reads the question
    fireEvent.click(button);
    await until(() => useStore.getState().gateReason !== null);
    expect(useStore.getState().gateReason).toBe("refused");
    expect(useStore.getState().authed).toBe(false);
    expect(localStorage.getItem("loom_token_v1")).toBeNull();
  });

  it("a refused link with no sign-in mark and no held token shows the gate saying so", async () => {
    window.history.replaceState(null, "", `/me#token=${"eyJhbGciOiJIUzI1NiJ9.eyJub3N1YiI6MX0.forged"}`);
    render(<App />);
    await until(() => useStore.getState().gateReason !== null);
    expect(useStore.getState().gateReason).toBe("refused");
    expect(useStore.getState().authed).toBe(false);
    expect(stored).toEqual([]);
    expect(screen.queryByText(/Sign in from a link/)).toBeNull();
  });

  it("strips whitespace from a link's token before asking whoami about it and storing it", async () => {
    const mid = Math.floor(mallory.length / 2);
    const spaced = `${mallory.slice(0, mid)}%20%0A${mallory.slice(mid)}`;
    window.history.replaceState(null, "", `/settings#token=${spaced}`);
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Sign in as mallory" }));
    await settle();
    expect(stored).toEqual([mallory]);
  });

  it("asks again when another tab stores a token while the screen is up", async () => {
    localStorage.setItem("loom_token_v1", alice);
    stored = [];
    window.history.replaceState(null, "", `/settings#token=${mallory}`);
    render(<App />);
    const first = await screen.findByRole("button", { name: "Sign in as mallory" });
    expect(screen.getByText(/You are signed in as/).textContent).toContain("alice");
    const bob = jwt({ sub: "bob", org: ORG, namespaces: [NS], roles: ["ADMIN"] });
    localStorage.setItem("loom_token_v1", bob); // another tab signs in as bob
    stored = [];
    fireEvent.click(first);
    // The question is put again, naming the account now held.
    await until(() => /bob/.test(screen.queryByText(/You are signed in as/)?.textContent ?? ""));
    expect(stored).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Sign in as mallory" }));
    await settle();
    expect(stored).toEqual([mallory]);
  });

  it("a cancel stands though another tab stores a token while the screen is up", async () => {
    localStorage.setItem("loom_token_v1", alice);
    stored = [];
    window.history.replaceState(null, "", `/settings#token=${mallory}`);
    render(<App />);
    await screen.findByRole("button", { name: "Sign in as mallory" });
    const bob = jwt({ sub: "bob", org: ORG, namespaces: [NS], roles: ["ADMIN"] });
    localStorage.setItem("loom_token_v1", bob); // another tab signs in as bob
    stored = [];
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await settle();
    expect(screen.queryByRole("button", { name: "Sign in as mallory" })).toBeNull();
    expect(stored).toEqual([]);
  });

  it("a sign-in mark older than ten minutes counts as none", async () => {
    window.history.replaceState(null, "", `/me${markSignIn(Date.now() - 11 * 60 * 1000)}#token=${mallory}`);
    render(<App />);
    await screen.findByRole("button", { name: "Sign in as mallory" });
    expect(stored).toEqual([]);
  });

  it("a signed-out boot of the session-ended card's address starts this tab's own sign-in", async () => {
    window.__LOOM_SSO = true;
    window.__LOOM_OWN_ORIGIN = true;
    vi.mocked(goToSso).mockClear();
    try {
      window.history.replaceState(null, "", "/me?sign_in=sso");
      render(<App />);
      await until(() => vi.mocked(goToSso).mock.calls.length > 0, 10_000);
      const { nonce } = JSON.parse(sessionStorage.getItem("loom_sign_in_pending_v1") ?? "{}");
      expect(goToSso).toHaveBeenCalledWith(nonce);
      expect(goToSso).toHaveBeenCalledTimes(1);
      expect(window.location.search).toBe("");
    } finally {
      delete window.__LOOM_SSO;
      delete window.__LOOM_OWN_ORIGIN;
    }
  });

  it("a signed-out boot of /me?sign_in=sso another site sent starts nothing and shows the button", async () => {
    window.__LOOM_SSO = true;
    window.__LOOM_OWN_ORIGIN = false;
    vi.mocked(goToSso).mockClear();
    try {
      window.history.replaceState(null, "", "/me?sign_in=sso");
      render(<App />);
      await screen.findByRole("link", { name: "Sign in with SSO" }, { timeout: 10_000 });
      await settle();
      expect(goToSso).not.toHaveBeenCalled();
      expect(sessionStorage.getItem("loom_sign_in_pending_v1")).toBeNull();
      expect(window.location.search).toBe("");
    } finally {
      delete window.__LOOM_SSO;
      delete window.__LOOM_OWN_ORIGIN;
    }
  });

  it("a fresh mark trusts no token that does not bring its nonce back", async () => {
    // Another page sends this tab a token while it is away at the identity provider.
    for (const landing of [`/me#token=${mallory}`, `/me?n=someone-elses-nonce-xyz#token=${mallory}`]) {
      cleanup();
      markSignIn();
      window.history.replaceState(null, "", landing);
      render(<App />);
      await screen.findByRole("button", { name: "Sign in as mallory" });
      expect(stored, landing).toEqual([]);
      expect(window.location.hash).toBe("");
      expect(window.location.search).toBe("");
    }
  });

  it("names the account the browser is signed in as beside the one the link brings", async () => {
    localStorage.setItem("loom_token_v1", jwt({ sub: "<b>alice</b>", org: "acme", namespaces: [NS] }));
    stored = [];
    window.history.replaceState(null, "", `/settings#token=${mallory}`);
    render(<App />);
    await screen.findByRole("button", { name: "Sign in as mallory" });
    // Text, not markup.
    expect(screen.getByText("<b>alice</b>")).toBeTruthy();
    expect(screen.getByText(/You are signed in as/).textContent).toBe("You are signed in as <b>alice</b> (acme).");
  });

  /** whoami answers for the bearer each call carries: `alice` is accepted; `link` is refused
   *  (`authenticated: false`) or unanswered (a network error). */
  function serveWhoami(link: string, answer: "accept" | "refuse" | "throw" | "busy", seen: number[]) {
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      if (!String(input).includes("/v1/whoami")) return json(GENERIC);
      const bearer = new Headers(init?.headers).get("Authorization");
      if (bearer === `Bearer ${link}`) {
        seen.push(stored.length); // what was stored when the link's token was asked about
        if (answer === "throw") throw new TypeError("Failed to fetch");
        // A replica whose keys are still loading answers 503 once, then accepts.
        if (answer === "busy" && seen.length === 1)
          return new Response(JSON.stringify({ error: "unavailable" }), {
            status: 503,
            headers: { "Content-Type": "application/json" },
          });
        if (answer === "refuse") return json({ authenticated: false });
      }
      return json({ authenticated: true, subject: "alice", org: ORG, namespaces: [NS], roles: ["ADMIN"] });
    });
  }

  for (const answer of ["refuse", "throw"] as const) {
    it(`a same-account link whoami does not accept (${answer}) leaves the held token stored`, async () => {
      localStorage.setItem("loom_token_v1", alice);
      stored = [];
      const forged = jwt({ sub: "alice", org: ORG, namespaces: [NS], roles: ["ADMIN"], iat: 3 });
      const seen: number[] = [];
      serveWhoami(forged, answer, seen);
      window.history.replaceState(null, "", `/me#token=${forged}`);
      render(<App />);
      await until(() => useStore.getState().whoami !== null);
      expect(seen).toEqual([0]);
      // Nothing written, so no other tab hears of the link's token either.
      expect(stored).toEqual([]);
      expect(localStorage.getItem("loom_token_v1")).toBe(alice);
      expect(useStore.getState().gateReason).toBeNull();
      expect(screen.queryByText(/Sign in from a link/)).toBeNull();
    });
  }

  it("asks again while whoami answers 503 for a same-account link, then stores it", async () => {
    localStorage.setItem("loom_token_v1", alice);
    stored = [];
    const again = jwt({ sub: "alice", org: ORG, namespaces: [NS], roles: ["ADMIN"], iat: 4 });
    const seen: number[] = [];
    serveWhoami(again, "busy", seen);
    window.history.replaceState(null, "", `/me#token=${again}`);
    render(<App />);
    await until(() => useStore.getState().whoami !== null, 10_000);
    expect(seen.length).toBeGreaterThanOrEqual(2);
    expect(seen.slice(0, 2)).toEqual([0, 0]); // both asks before anything was stored
    expect(stored).toEqual([again]);
    expect(localStorage.getItem("loom_token_v1")).toBe(again);
  });

  it("asks about a link's token again after a 503 though another tab clears the stored token meanwhile", async () => {
    localStorage.setItem("loom_token_v1", alice);
    stored = [];
    const link = jwt({ sub: "alice", org: ORG, namespaces: [NS], roles: ["ADMIN"], iat: 6 });
    let asked = 0;
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      if (!String(input).includes("/v1/whoami")) return json(GENERIC);
      const bearer = new Headers(init?.headers).get("Authorization");
      if (bearer === `Bearer ${link}` && ++asked === 1) {
        localStorage.removeItem("loom_token_v1"); // another tab signs out while this one waits
        return new Response(JSON.stringify({ error: "unavailable" }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        });
      }
      return json({ authenticated: true, subject: "alice", org: ORG, namespaces: [NS], roles: ["ADMIN"] });
    });
    window.history.replaceState(null, "", `/me#token=${link}`);
    render(<App />);
    // Nothing is held now, so the account the server named is asked about, not refused.
    await screen.findByRole("button", { name: "Sign in as alice" }, { timeout: 10_000 });
    expect(asked).toBe(2);
    expect(useStore.getState().gateReason).toBeNull();
    expect(stored).toEqual([]);
  });

  it("stores a same-account link's token without asking, once whoami accepts it", async () => {
    localStorage.setItem("loom_token_v1", alice);
    stored = [];
    const again = jwt({ sub: "alice", org: ORG, namespaces: [NS], roles: ["ADMIN"], iat: 2 });
    const seen: number[] = [];
    serveWhoami(again, "accept", seen);
    window.history.replaceState(null, "", `/me#token=${again}`);
    render(<App />);
    await until(() => useStore.getState().whoami !== null);
    // Asked about once, with its own header before anything was stored; boot takes that
    // answer instead of asking again.
    expect(seen).toEqual([0]);
    expect(stored).toEqual([again]);
    expect(localStorage.getItem("loom_token_v1")).toBe(again);
    expect(screen.queryByText(/Sign in from a link/)).toBeNull();
  });
});

// Each case boots the whole App, some twice: slower than a unit test on a loaded runner.
describe("a refused token", { timeout: 30_000 }, () => {
  let refuse = false;
  let refuseListMine = false;
  // whoami alone refuses (any token but `fresh`); counted.
  let refuseWhoamiOnly = false;
  let refusedWhoami = 0;
  let whoamiAnswers: number[] = [];
  let whoamiCalls = 0;
  let listMineRefused: () => void = () => undefined;
  // A dashboard sign-in's token carries its session (`sid`).
  const alice = jwt({ sub: "alice", org: ORG, namespaces: [NS], roles: ["ADMIN"], sid: "s1" });
  const fresh = jwt({ sub: "alice", org: ORG, namespaces: [NS], roles: ["ADMIN"], iat: 2 });
  const bob = jwt({ sub: "bob", org: ORG, namespaces: [NS], roles: ["ADMIN"] });
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    invalidateShared();
    resetNamespaceRoleCache();
    refuse = false;
    refuseListMine = false;
    refuseWhoamiOnly = false;
    refusedWhoami = 0;
    whoamiAnswers = [];
    whoamiCalls = 0;
    useStore.setState({
      authed: true,
      sessionExpired: false,
      gateReason: null,
      whoami: null,
      myNamespaces: null,
      identity: null,
    });
    Element.prototype.scrollTo = vi.fn();
    window.history.replaceState(null, "", "/memories");
    localStorage.setItem("loom_token_v1", alice);
    localStorage.setItem("loom_namespace_v1", NS);
    const listMineDone = new Promise<void>((r) => {
      listMineRefused = r;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        const refused = new Response('{"detail":{"code":"AUTH_INVALID","message":"token expired"}}', { status: 401 });
        if (url.includes("/v1/whoami")) {
          whoamiCalls++;
          // The server answers for the token the request carries.
          const bearer = new Headers(init?.headers).get("Authorization")?.replace("Bearer ", "") ?? "";
          if (refuseListMine) {
            await listMineDone; // its 401 lands first and clears the token
            // Another replica, or a revocation a moment later: whoami still says yes.
            return json({ authenticated: true, subject: "alice", org: ORG, namespaces: [NS], roles: ["ADMIN"] });
          }
          const status = whoamiAnswers.shift() ?? 200;
          if (status !== 200) return new Response("{}", { status });
          if (refuseWhoamiOnly && bearer !== fresh) {
            refusedWhoami++;
            return json({ authenticated: false });
          }
          if (!bearer || (refuse && bearer !== fresh)) {
            await new Promise((r) => setTimeout(r, 30)); // after the early read's 401
            return json({ authenticated: false });
          }
          const sub = bearer === bob ? "bob" : "alice";
          return json({ authenticated: true, subject: sub, org: ORG, namespaces: [NS], roles: ["ADMIN"] });
        }
        if (url.includes("namespace.list_mine")) {
          // The server refuses the same token on every op, and the early read is first back.
          const bearer = new Headers(init?.headers).get("Authorization")?.replace("Bearer ", "") ?? "";
          if (refuse && bearer === alice) return refused;
          if (!refuseListMine) return json({ ...GENERIC, rows: MINE });
          setTimeout(listMineRefused, 0);
          return refused;
        }
        if (refuse && url.includes("/v1/") && !url.includes("/v1/health")) return refused;
        return json(GENERIC);
      }),
    );
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    delete window.__LOOM_SSO;
    useStore.setState({ sessionExpired: false, gateReason: null, whoami: null, myNamespaces: null, identity: null });
    window.history.replaceState(null, "", "/");
  });

  async function expireMidSession() {
    render(<App />);
    await until(() => !!document.getElementById("main") && useStore.getState().whoami !== null);
    refuse = true;
    // Any call now, as a view's refresh or a poll would make it.
    await act(async () => {
      await import("./api").then((api) => api.post("/v1/memory.list", { org: ORG, namespace: NS }).catch(() => null));
    });
    await until(() => useStore.getState().sessionExpired);
  }

  function storeElsewhere(token: string | null) {
    // Another tab's write: this tab hears it as a storage event, after the value changed.
    const oldValue = localStorage.getItem("loom_token_v1");
    if (token === null) localStorage.removeItem("loom_token_v1");
    else localStorage.setItem("loom_token_v1", token);
    window.dispatchEvent(new StorageEvent("storage", { key: "loom_token_v1", oldValue, newValue: token }));
  }

  it("mid-session says so in a card over the page, which stays as it was", async () => {
    await expireMidSession();
    const card = screen.getByTestId("session-expired-bar");
    expect(card).toHaveTextContent("Your session ended");
    // In the toasts' viewport: over every dialog, and a press on it dismisses none.
    expect(card.closest("[data-toast-viewport]")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Sign in in a new tab" })).toBeInTheDocument();
    expect(localStorage.getItem("loom_token_v1")).toBeNull();
    expect(document.getElementById("main")).not.toBeNull();
    expect(useStore.getState().authed).toBe(true);
  });

  it("with SSO on, the card's new tab opens the gate that starts that tab's own marked sign-in", async () => {
    window.__LOOM_SSO = true;
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    try {
      await expireMidSession();
      fireEvent.click(screen.getByRole("button", { name: "Sign in in a new tab" }));
      expect(open).toHaveBeenCalledWith("/me?sign_in=sso", "_blank", "noopener");
    } finally {
      delete window.__LOOM_SSO;
    }
  });

  it("takes the same person's token from another tab once the server confirms it, and carries on", async () => {
    await expireMidSession();
    const before = whoamiCalls;
    refuse = false;
    await act(async () => storeElsewhere(fresh));
    await until(() => !useStore.getState().sessionExpired);
    expect(whoamiCalls).toBe(before + 1);
    expect(reloadPage).not.toHaveBeenCalled();
    expect(screen.queryByTestId("session-expired-bar")).not.toBeInTheDocument();
  });

  it("reloads when someone else signed in in another tab", async () => {
    await expireMidSession();
    refuse = false;
    await act(async () => storeElsewhere(bob));
    await until(() => vi.mocked(reloadPage).mock.calls.length > 0);
    expect(useStore.getState().whoami?.subject).toBe("alice");
  });

  it("keeps the card when a newer drop lands while whoami answers for a stored token", async () => {
    await expireMidSession();
    refuse = false;
    await act(async () => {
      storeElsewhere(fresh);
      storeElsewhere(null);
    });
    await settle(4);
    expect(useStore.getState().sessionExpired).toBe(true);
    expect(reloadPage).not.toHaveBeenCalled();
  });

  it("leaves a healthy page alone when another tab's token cannot be confirmed", async () => {
    render(<App />);
    await until(() => useStore.getState().whoami !== null);
    whoamiAnswers = [503, 503, 503, 503, 503, 503];
    await act(async () => storeElsewhere(fresh));
    await until(() => whoamiAnswers.length === 0, 10_000);
    await settle(2);
    expect(reloadPage).not.toHaveBeenCalled();
    expect(useStore.getState().sessionExpired).toBe(false);
  });

  it("reloads to a plain sign-in when the person signed out in another tab", async () => {
    render(<App />);
    await until(() => useStore.getState().whoami !== null);
    localStorage.setItem("loom_signed_out_v1", "1");
    await act(async () => storeElsewhere(null));
    await until(() => vi.mocked(reloadPage).mock.calls.length > 0);
    expect(useStore.getState().sessionExpired).toBe(false);
    expect(sessionStorage.getItem("loom_session_ended_v1")).toBeNull();
  });

  it("ends this tab's session, keeping the page, when another tab stores a token the server refuses", async () => {
    render(<App />);
    await until(() => useStore.getState().whoami !== null);
    const stale = jwt({ sub: "alice", org: ORG, namespaces: [NS], roles: ["ADMIN"], iat: 1 });
    refusedWhoami = 0;
    refuseWhoamiOnly = true; // the server refuses the stored token; no other call is made to say so
    await act(async () => storeElsewhere(stale));
    await until(() => useStore.getState().sessionExpired);
    expect(refusedWhoami).toBe(1);
    expect(reloadPage).not.toHaveBeenCalled();
  });

  it("reloads at once for another person's token, without waiting on whoami", async () => {
    render(<App />);
    await until(() => useStore.getState().whoami !== null);
    whoamiAnswers = [503, 503, 503, 503, 503, 503];
    await act(async () => storeElsewhere(bob));
    await until(() => vi.mocked(reloadPage).mock.calls.length > 0);
    expect(whoamiAnswers).toHaveLength(6);
  });

  it("a reload after a sign-out elsewhere is still a plain sign-in, with the mark kept", async () => {
    await expireMidSession();
    localStorage.setItem("loom_signed_out_v1", "1"); // another tab signed out; this one had no token to drop
    cleanup();
    useStore.setState({ authed: true, sessionExpired: false, gateReason: null, whoami: null, identity: null });
    render(<App />);
    await until(() => useStore.getState().authed === false);
    expect(useStore.getState().gateReason).toBeNull();
    expect(localStorage.getItem("loom_signed_out_v1")).toBe("1");
  });

  it("a drop and a sign-in elsewhere, heard together, keep the new token", async () => {
    render(<App />);
    await until(() => useStore.getState().whoami !== null);
    await act(async () => {
      // Both land before this tab's handler runs (its chunk resolves after the events).
      storeElsewhere(null);
      storeElsewhere(fresh);
    });
    await settle(3);
    expect(localStorage.getItem("loom_token_v1")).toBe(fresh);
  });

  it("ends this tab's session too when another tab clears the whole storage", async () => {
    render(<App />);
    await until(() => useStore.getState().whoami !== null);
    await act(async () => {
      localStorage.clear();
      window.dispatchEvent(new StorageEvent("storage", { key: null }));
    });
    await until(() => useStore.getState().sessionExpired);
    expect(screen.getByTestId("session-expired-bar")).toBeInTheDocument();
  });

  it("ends this tab's session too when another tab drops the token", async () => {
    render(<App />);
    await until(() => useStore.getState().whoami !== null);
    await act(async () => storeElsewhere(null));
    await until(() => useStore.getState().sessionExpired);
    expect(screen.getByTestId("session-expired-bar")).toBeInTheDocument();
    // The dropped token was a sign-in session's: a reload resumes it, as the tab that lost
    // it first would.
    expect(sessionStorage.getItem("loom_session_ended_v1")).toBe("expired");
  });

  it("a reload after the session ended starts SSO by itself", async () => {
    window.__LOOM_SSO = true;
    await expireMidSession();
    cleanup();
    useStore.setState({ authed: true, sessionExpired: false, gateReason: null, whoami: null, identity: null });
    render(<App />);
    await screen.findByRole("link", { name: "Sign in with SSO" }, { timeout: 8000 });
    await until(() => vi.mocked(goToSso).mock.calls.length > 0);
    expect(goToSso).toHaveBeenCalledTimes(1);
  });

  it("a reload after a minted token ended shows the gate and starts no sign-in by itself", async () => {
    window.__LOOM_SSO = true;
    localStorage.setItem("loom_token_v1", jwt({ sub: "alice", org: ORG, namespaces: [NS], roles: ["ADMIN"] }));
    await expireMidSession();
    cleanup();
    useStore.setState({ authed: true, sessionExpired: false, gateReason: null, whoami: null, identity: null });
    render(<App />);
    await screen.findByRole("link", { name: "Sign in with SSO" }, { timeout: 8000 });
    expect(useStore.getState().gateReason).toBe("ended");
    expect(screen.getByRole("status")).toHaveTextContent("Your session ended");
    await settle();
    expect(goToSso).not.toHaveBeenCalled();
  });

  it("an ended tab that cannot confirm another tab's token asks again later, and then carries on", async () => {
    await expireMidSession();
    refuse = false;
    whoamiAnswers = [503, 503, 503, 503, 503, 503];
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      await act(async () => storeElsewhere(fresh));
      await until(() => whoamiAnswers.length === 0, 10_000);
      expect(useStore.getState().sessionExpired).toBe(true);
      await act(async () => {
        vi.advanceTimersByTime(30_000);
      });
      await until(() => !useStore.getState().sessionExpired, 10_000);
    } finally {
      vi.useRealTimers();
    }
  });

  it("a sign-out elsewhere reloads a tab whose session had already ended", async () => {
    await expireMidSession();
    await act(async () => {
      localStorage.setItem("loom_signed_out_v1", "1");
      window.dispatchEvent(new StorageEvent("storage", { key: "loom_signed_out_v1", oldValue: null, newValue: "1" }));
    });
    await until(() => vi.mocked(reloadPage).mock.calls.length > 0);
  });

  it("a Reload while the server is away starts no sign-in by itself", async () => {
    window.__LOOM_SSO = true;
    await expireMidSession();
    cleanup();
    useStore.setState({ authed: true, sessionExpired: false, gateReason: null, whoami: null, identity: null });
    vi.mocked(fetch).mockImplementation(async () => {
      throw new TypeError("Failed to fetch");
    });
    render(<App />);
    await until(() => useStore.getState().authed === false);
    await screen.findByRole("link", { name: "Sign in with SSO" }, { timeout: 8000 });
    expect(useStore.getState().gateReason).toBeNull();
    expect(goToSso).not.toHaveBeenCalled();
  });

  it("a reload after the session ended shows the gate saying so, though the token is gone", async () => {
    await expireMidSession();
    cleanup();
    useStore.setState({ authed: true, sessionExpired: false, gateReason: null, whoami: null, identity: null });
    render(<App />);
    await until(() => useStore.getState().authed === false);
    expect(useStore.getState().gateReason).toBe("expired");
  });

  it("at boot shows the gate saying the session expired when list_mine is refused while whoami answers yes", async () => {
    refuseListMine = true;
    render(<App />);
    await until(() => useStore.getState().authed === false);
    await settle();
    expect(useStore.getState()).toMatchObject({ authed: false, gateReason: "expired", sessionExpired: false });
    // whoami's yes did not bring the app up without a token.
    expect(useStore.getState().whoami).toBeNull();
    expect(document.getElementById("main")).toBeNull();
    expect(localStorage.getItem("loom_token_v1")).toBeNull();
  });

  it("signing out after the session ended reloads to a plain sign-in, which starts no SSO", async () => {
    window.__LOOM_SSO = true;
    await expireMidSession();
    // Header's Sign out: the mark, then the reload, which boots with no token.
    const api = await import("./api");
    api.signOut();
    cleanup();
    useStore.setState({ authed: true, sessionExpired: false, gateReason: null, whoami: null, identity: null });
    render(<App />);
    await until(() => useStore.getState().authed === false);
    expect(useStore.getState().gateReason).toBeNull();
    await screen.findByRole("link", { name: "Sign in with SSO" }, { timeout: 8000 });
    expect(goToSso).not.toHaveBeenCalled();
  });

  it("a refused sign-in link says so and starts no SSO: it may be the way in while the provider is down", async () => {
    window.__LOOM_SSO = true;
    localStorage.clear();
    refuse = true; // whoami refuses the link's token
    // The SSO sign-in this tab started comes back with a token the server refuses.
    window.history.replaceState(null, "", `/me${markSignIn()}#token=${alice}`);
    render(<App />);
    await until(() => useStore.getState().authed === false);
    await screen.findByRole("link", { name: "Sign in with SSO" }, { timeout: 8000 });
    expect(useStore.getState().gateReason).toBe("refused");
    expect(goToSso).not.toHaveBeenCalled();
    expect(localStorage.getItem("loom_token_v1")).toBeNull();
    delete window.__LOOM_SSO;
  });

  it("boots afresh when another tab stores a token while whoami cannot answer", async () => {
    whoamiAnswers = [503, 503, 503, 503, 503, 503];
    render(<App />);
    await until(() => whoamiCalls >= 1);
    localStorage.setItem("loom_token_v1", fresh);
    await until(() => vi.mocked(reloadPage).mock.calls.length > 0, 10_000);
    expect(useStore.getState().authed).toBe(true);
  });

  it("a sign-in link that whoami alone refuses says so and starts no SSO", async () => {
    window.__LOOM_SSO = true;
    localStorage.clear();
    refuseWhoamiOnly = true; // list_mine answers; only whoami refuses the link's token
    // The SSO sign-in this tab started comes back with a token the server refuses.
    window.history.replaceState(null, "", `/me${markSignIn()}#token=${alice}`);
    render(<App />);
    await until(() => useStore.getState().authed === false);
    await screen.findByRole("link", { name: "Sign in with SSO" }, { timeout: 8000 });
    expect(useStore.getState().gateReason).toBe("refused");
    expect(refusedWhoami).toBeGreaterThan(0);
    expect(goToSso).not.toHaveBeenCalled();
  });

  it("boots afresh when another tab stores a token while whoami answers for this one", async () => {
    let answer: () => void = () => undefined;
    const held = new Promise<void>((r) => {
      answer = r;
    });
    const fetchMock = vi.mocked(fetch);
    const serve = fetchMock.getMockImplementation();
    fetchMock.mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/v1/whoami")) await held;
      return serve ? serve(input, init) : json(GENERIC);
    });
    render(<App />);
    await until(() => whoamiCalls === 0 && fetchMock.mock.calls.some(([u]) => String(u).includes("/v1/whoami")));
    localStorage.setItem("loom_token_v1", bob);
    answer();
    await until(() => vi.mocked(reloadPage).mock.calls.length > 0);
  });

  it("asks whoami again while the replica answers 503, and keeps the token", async () => {
    whoamiAnswers = [503];
    render(<App />);
    await until(() => useStore.getState().whoami !== null, 8000);
    expect(whoamiCalls).toBe(2);
    expect(localStorage.getItem("loom_token_v1")).not.toBeNull();
    expect(useStore.getState().authed).toBe(true);
  });
});
