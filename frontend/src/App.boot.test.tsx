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
      localStorage.removeItem("loom_token_v1");
      useStore.setState({ authed: false });
      const token = jwt({ sub: "alice", org: ORG, namespaces: [NS], roles: ["ADMIN"] });
      window.history.replaceState(null, "", `/#token=${token}`);
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
      localStorage.removeItem("loom_token_v1");
      useStore.setState({ authed: false });
      window.history.replaceState(
        null,
        "",
        `${root}#token=${jwt({ sub: "alice", org: ORG, namespaces: [NS], roles: ["ADMIN"] })}`,
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
    useStore.setState({ authed: false, whoami: null, myNamespaces: null, identity: null, view: "setup" });
    Element.prototype.scrollTo = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json(GENERIC)),
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

  it("stores a #token= fragment and scrubs it", async () => {
    window.history.replaceState(null, "", "/memories#token=frag.jwt.value");
    render(<App />);
    await settle();
    expect(stored).toEqual(["frag.jwt.value"]);
    expect(window.location.href).not.toContain("frag.jwt.value");
  });
});

// Each case boots the whole App, some twice: slower than a unit test on a loaded runner.
describe("a refused token", { timeout: 30_000 }, () => {
  let refuse = false;
  let refuseListMine = false;
  let whoamiAnswers: number[] = [];
  let whoamiCalls = 0;
  let listMineRefused: () => void = () => undefined;
  const alice = jwt({ sub: "alice", org: ORG, namespaces: [NS], roles: ["ADMIN"] });
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
          if (!bearer || (refuse && bearer === alice)) return json({ authenticated: false });
          const sub = bearer === bob ? "bob" : "alice";
          return json({ authenticated: true, subject: sub, org: ORG, namespaces: [NS], roles: ["ADMIN"] });
        }
        if (url.includes("namespace.list_mine")) {
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
    whoamiAnswers = [503, 503, 503, 503];
    await act(async () => storeElsewhere(fresh));
    await until(() => whoamiAnswers.length === 0, 10_000);
    await settle(2);
    expect(reloadPage).not.toHaveBeenCalled();
    expect(useStore.getState().sessionExpired).toBe(false);
  });

  it("reloads to a plain sign-in when the person signed out in another tab", async () => {
    render(<App />);
    await until(() => useStore.getState().whoami !== null);
    localStorage.setItem("loom_signed_out_v1", String(Date.now()));
    await act(async () => storeElsewhere(null));
    await until(() => vi.mocked(reloadPage).mock.calls.length > 0);
    expect(useStore.getState().sessionExpired).toBe(false);
    expect(sessionStorage.getItem("loom_session_ended_v1")).toBeNull();
  });

  it("ends this tab's session too when another tab drops the token", async () => {
    render(<App />);
    await until(() => useStore.getState().whoami !== null);
    await act(async () => storeElsewhere(null));
    await until(() => useStore.getState().sessionExpired);
    expect(screen.getByTestId("session-expired-bar")).toBeInTheDocument();
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
    await settle(4); // the gate's chunk and its effect
    expect(goToSso).not.toHaveBeenCalled();
    delete window.__LOOM_SSO;
  });

  it("a refused sign-in link says so and starts no SSO: it may be the way in while the provider is down", async () => {
    window.__LOOM_SSO = true;
    localStorage.clear();
    refuse = true; // whoami refuses the link's token
    window.history.replaceState(null, "", `/me#token=${alice}`);
    render(<App />);
    await until(() => useStore.getState().authed === false);
    await settle(4);
    expect(useStore.getState().gateReason).toBe("refused");
    expect(goToSso).not.toHaveBeenCalled();
    delete window.__LOOM_SSO;
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
