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
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { preloadView } = vi.hoisted(() => ({ preloadView: vi.fn() }));
vi.mock("./views/registry", () => ({ preloadView, VIEW_LOADERS: {} }));

import App from "./App";
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
    window.history.replaceState(null, "", "/me/memories");
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
