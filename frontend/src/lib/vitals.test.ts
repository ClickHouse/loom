import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { _resetVitals, flushView, installVitals, startView } from "./vitals";
import { noteCallEnd, noteCallStart } from "./vitalsCounter";
import { setPageOwner, setToken } from "../api";

// One ui.vitals row per page view, posted when the view ends, carrying the route, the
// view's call count and span, the DOM size and the build; LCP only on the first view of
// the document.
describe("ui.vitals", () => {
  beforeEach(() => {
    _resetVitals();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts one row per view with the route, calls and build; LCP only once", async () => {
    const bodies: Array<Record<string, unknown>> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) => {
        bodies.push(JSON.parse(String(init?.body)));
        return new Response("{}", { status: 200 });
      }),
    );
    installVitals(); // jsdom has no PerformanceObserver — the guards hold
    startView("memories");
    noteCallStart();
    noteCallEnd();
    noteCallStart();
    noteCallEnd();
    flushView({ org: "o", namespace: "n" }, "31bc74135");
    startView("knowledge");
    noteCallStart();
    noteCallEnd();
    flushView({ org: "o", namespace: "n" }, "31bc74135");
    expect(bodies).toHaveLength(2);
    expect(bodies[0]).toMatchObject({
      route: "memories",
      api_calls: 2,
      build: "31bc74135",
      cold: true,
      agent: "loom-ui",
    });
    expect(typeof bodies[0].dom_nodes).toBe("number");
    expect(bodies[1]).toMatchObject({ route: "knowledge", api_calls: 1, cold: false, lcp_ms: null });
    // The interaction vital rides every row; jsdom has no event timing → null
    expect(bodies[0]).toHaveProperty("inp_ms", null);
    expect(vi.mocked(fetch).mock.calls[0][0]).toBe("/v1/ui.vitals");
    expect(vi.mocked(fetch).mock.calls[0][1]?.keepalive).toBe(true);
  });

  it("posts nothing without a namespace or without a view", () => {
    const f = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", f);
    flushView({ org: "o", namespace: "" }, "");
    startView("runs");
    flushView(null, "");
    expect(f).not.toHaveBeenCalled();
  });

  it("sends no row under a token that names someone other than the page's person", () => {
    const fetchMock = vi.fn(async () => new Response("{}"));
    vi.stubGlobal("fetch", fetchMock);
    const part = (o: object) => btoa(JSON.stringify(o)).replace(/=+$/, "");
    setPageOwner({ subject: "alice", org: "o" });
    try {
      setToken(`${part({ alg: "HS256" })}.${part({ sub: "bob", org: "o" })}.sig`);
      startView("memories");
      flushView({ org: "o", namespace: "n" }, "b1");
      expect(fetchMock).not.toHaveBeenCalled();
    } finally {
      setPageOwner(null);
      localStorage.clear();
    }
  });
});
