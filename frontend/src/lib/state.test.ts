import { renderHook } from "../test/rtl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { readPlace } from "./place";
import {
  navigationHref,
  pathnameView,
  pathToView,
  useIdentity,
  useStore,
  viewHref,
  viewToPath,
  type View,
} from "./state";

function setLocation(pathAndQuery: string): void {
  window.history.replaceState(null, "", pathAndQuery);
}

describe("navigationHref keeps only the query keys the target view owns", () => {
  beforeEach(() => {
    setLocation("/graph?seed=alpha&repo=loom&stray=1");
  });

  it("Graph keeps ?seed=, Repositories keeps ?repo=", () => {
    expect(navigationHref("graph")).toBe("/graph?seed=alpha");
    expect(navigationHref("code")).toBe("/repositories?repo=loom");
  });

  it("a view that owns no key gets a bare URL", () => {
    expect(navigationHref("memories")).toBe("/memories");
    expect(navigationHref("admin:health")).toBe("/admin/health");
    expect(navigationHref("browse")).toBe("/spaces");
  });

  it("carries nothing when the owned key is absent", () => {
    setLocation("/memories");
    expect(navigationHref("graph")).toBe("/graph");
    expect(navigationHref("code")).toBe("/repositories");
  });

  it("Graph keeps a ?path= deep link, other views drop it", () => {
    setLocation("/graph?path=a-%3Eb");
    expect(navigationHref("graph")).toBe("/graph?path=a-%3Eb");
    expect(navigationHref("memories")).toBe("/memories");
  });

  it("re-encodes the value it keeps", () => {
    setLocation("/graph?seed=a%20b");
    expect(navigationHref("graph")).toBe("/graph?seed=a+b");
  });
});

describe("view ↔ path", () => {
  it("round-trips every view", () => {
    const views: View[] = ["memories", "graph", "code", "browse", "setup", "admin:review_queue", "admin:users"];
    for (const v of views) expect(pathToView(viewToPath(v))).toBe(v);
  });

  it("a page's path reads like its label, and only that path resolves", () => {
    expect(viewHref("knowledge")).toBe("/library");
    expect(viewHref("sessions")).toBe("/conversations");
    expect(viewHref("sharing")).toBe("/flow");
    expect(viewHref("setup")).toBe("/settings");
    // Settings owns `?tab=`; a navigation to it keeps the open tab and nothing else.
    window.history.replaceState(null, "", "/settings?tab=memory&repo=x");
    expect(navigationHref("setup")).toBe("/settings?tab=memory");
    window.history.replaceState(null, "", "/settings");
    for (const old of ["knowledge", "sessions", "sharing", "setup", "discover", "browse", "code"]) {
      expect(pathToView(old), `old path /${old}`).toBeNull();
    }
  });

  it("maps browse to /spaces and admin views to a slash", () => {
    expect(viewHref("browse")).toBe("/spaces");
    expect(viewHref("admin:health")).toBe("/admin/health");
  });

  it("Repositories lives at /repositories", () => {
    expect(viewHref("code")).toBe("/repositories");
    expect(pathToView("repositories")).toBe("code");
  });

  it("rejects unknown and empty paths", () => {
    expect(pathToView("")).toBeNull();
    expect(pathToView("nope")).toBeNull();
  });
});

describe("the dashboard lives at the server root", () => {
  it("reads the view from the root path, and the bare root has none", () => {
    for (const [path, view] of [
      ["/", null],
      ["/memories", "memories"],
      ["/memories/", "memories"],
      ["/admin/review_queue", "admin:review_queue"],
      ["/repositories", "code"],
      ["/nope", null],
    ] as const) {
      setLocation(path);
      expect(pathnameView(), path).toBe(view);
    }
  });

  it("links every view at the root", () => {
    for (const v of ["memories", "graph", "admin:health"] as View[]) {
      expect(viewHref(v)).toBe(`/${viewToPath(v)}`);
    }
  });
});

describe("navigating actions mirror the view into the URL", () => {
  beforeEach(() => {
    setLocation("/recall");
  });

  it("a tag search lands on /memories as one new history entry", () => {
    const before = window.history.length;
    useStore.getState().searchByTag("infra");
    expect(useStore.getState().view).toBe("memories");
    expect(window.location.pathname).toBe("/memories");
    expect(window.history.length).toBe(before + 1);
  });

  it("opening a conversation thread lands on /conversations", () => {
    useStore.getState().openConversationThread("t-1");
    expect(useStore.getState().view).toBe("sessions");
    expect(useStore.getState().pendingConversationThread).toEqual({ threadId: "t-1" });
    expect(window.location.pathname).toBe("/conversations");
  });

  it("editing a profile section lands on /profile, and clearing the request pushes nothing", () => {
    useStore.getState().requestEditProfile("role");
    expect(window.location.pathname).toBe("/profile");
    const after = window.history.length;
    useStore.getState().requestEditProfile(null);
    expect(window.location.pathname).toBe("/profile");
    expect(window.history.length).toBe(after);
  });

  it("drops query keys the target view does not own", () => {
    setLocation("/graph?seed=alpha");
    useStore.getState().searchByTag("infra");
    expect(window.location.pathname + window.location.search).toBe("/memories");
  });
});

describe("useIdentity", () => {
  it("returns the identity App's gate has set", () => {
    const identity = { org: "acme", namespace: "personal-me", agent: "ui" };
    useStore.setState({ identity });
    const { result, unmount } = renderHook(() => useIdentity());
    expect(result.current).toBe(identity);
    unmount();
    useStore.setState({ identity: null });
  });

  it("throws a named error when rendered without an identity", () => {
    useStore.setState({ identity: null });
    // React reports the render error on console.error before rethrowing it.
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => renderHook(() => useIdentity())).toThrow(/useIdentity: no identity/);
    consoleError.mockRestore();
  });
});

describe("the object a page shows is in its URL", () => {
  const identity = { org: "acme", namespace: "team", agent: "loom-ui" };
  beforeEach(() => {
    useStore.setState({ identity, view: "memories", inspectedMemoryKey: null, inspectedAgent: "" });
  });

  it("Memories, Library and Conversations own ?memory=, ?doc= and ?thread=", () => {
    setLocation("/memories?memory=a%2Fb&doc=x&thread=t");
    expect(navigationHref("memories")).toBe("/memories?namespace=team&memory=a%2Fb");
    expect(navigationHref("knowledge")).toBe("/library?namespace=team&doc=x");
    expect(navigationHref("sessions")).toBe("/conversations?namespace=team&thread=t");
    expect(navigationHref("graph")).toBe("/graph?namespace=team");
  });

  it("inspecting on Memories writes ?memory= into the entry, without a new one; closing drops it", () => {
    setLocation("/memories");
    const before = window.history.length;
    useStore.getState().inspect("api/rate-limit", { agent: "claude-code" });
    expect(window.location.search).toBe("?memory=api%2Frate-limit");
    expect(window.history.length).toBe(before);
    useStore.getState().inspect(null);
    expect(window.location.search).toBe("");
  });

  it("a row opened with its user and session keeps them; the next open by key or agent clears them", () => {
    useStore.getState().inspect("billing/invoices", { agent: "claude-code", userId: "ada", sessionId: "-" });
    expect(useStore.getState().inspectedOwner).toEqual({ user_id: "ada", session_id: "-" });
    useStore.getState().inspect("api/rate-limit", { agent: "claude-code" });
    expect(useStore.getState().inspectedOwner).toBeNull();
    useStore.getState().inspect("billing/invoices", { agent: "claude-code", userId: "ada" });
    useStore.getState().inspect("api/rate-limit");
    expect(useStore.getState().inspectedOwner).toBeNull();
  });

  it("a deep link opens what it names on boot: the memory, the document, the thread", () => {
    setLocation("/memories?memory=api%2Frate-limit");
    useStore.getState().bootView("memories");
    // No remembered writer: every agent's row, as Runs opens a key it knows by name.
    expect(useStore.getState()).toMatchObject({ inspectedMemoryKey: "api/rate-limit", inspectedAgent: "*" });
    expect(window.location.search).toBe("?namespace=team&memory=api%2Frate-limit");
    setLocation("/library?doc=docs%2Fa.md");
    useStore.getState().bootView("knowledge");
    expect(useStore.getState().pendingDocSourceUri).toBe("docs/a.md");
    setLocation("/conversations?thread=t-1");
    useStore.getState().bootView("sessions");
    expect(useStore.getState().pendingConversationThread).toEqual({ threadId: "t-1" });
  });

  it("a reload keeps the writer agent the memory was opened as", () => {
    setLocation("/memories");
    useStore.getState().inspect("api/rate-limit", { agent: "claude-code" });
    useStore.setState({ inspectedMemoryKey: null, inspectedAgent: "" });
    useStore.getState().bootView("memories");
    expect(useStore.getState().inspectedAgent).toBe("claude-code");
  });

  // A tag clicked in the drawer, or the sidebar's Memories link, closes the drawer and drops
  // ?memory=, so a reload does not reopen what the reader closed.
  it("navigating to Memories closes the open memory in the URL too", () => {
    setLocation("/memories");
    useStore.getState().inspect("api/rate-limit", { agent: "claude-code" });
    useStore.getState().navigate("memories");
    expect(useStore.getState().inspectedMemoryKey).toBeNull();
    expect(window.location.search).toBe("?namespace=team");
    useStore.getState().inspect("api/rate-limit", { agent: "claude-code" });
    useStore.getState().searchByTag("api");
    expect(window.location.search).toBe("?namespace=team");
  });

  // A memory belongs to its namespace: switching namespace, from any picker, closes it.
  it("switching namespace closes the open memory", () => {
    useStore.setState({ inspectedMemoryKey: "api/rate-limit" });
    useStore.getState().setIdentity({ ...identity, namespace: "other" });
    expect(useStore.getState().inspectedMemoryKey).toBeNull();
    useStore.setState({ inspectedMemoryKey: "api/rate-limit" });
    useStore.getState().setIdentity({ ...identity, namespace: "other", agent: "x" });
    expect(useStore.getState().inspectedMemoryKey).toBe("api/rate-limit");
  });

  // The sidebar's Memories link lands on the list, not on the Insights tab it was left on.
  it("navigating to Memories drops its tab from the URL", () => {
    setLocation("/memories?section=insights");
    useStore.getState().navigate("memories");
    expect(window.location.search).toBe("?namespace=team");
  });

  it("opening a document or a thread from elsewhere pushes its URL", () => {
    setLocation("/memories");
    useStore.getState().openDocument("docs/a.md");
    expect(window.location.pathname + window.location.search).toBe("/library?namespace=team&doc=docs%2Fa.md");
    useStore.getState().openConversationThread("t-1");
    expect(window.location.pathname + window.location.search).toBe("/conversations?namespace=team&thread=t-1");
  });
});

describe("a navigation leaves a place for Back to restore", () => {
  const identity = { org: "acme", namespace: "team", agent: "loom-ui" };
  beforeEach(() => {
    document.body.innerHTML = '<main id="main"><button data-focus-key="memory:k">k</button></main>';
    setLocation("/memories?memory=k");
    useStore.setState({ identity, view: "memories", inspectedMemoryKey: "k", inspectedAgent: "bot" });
  });

  it("the entry being left remembers the open memory and the focused key; the new one counts", () => {
    const main = document.getElementById("main")!;
    Object.defineProperty(main, "scrollTop", { value: 420, configurable: true });
    document.querySelector<HTMLElement>("[data-focus-key]")!.focus();
    let left: unknown = null;
    const push = history.pushState.bind(history);
    const spy = vi.spyOn(history, "pushState").mockImplementation((d, u, url) => {
      left = history.state;
      push(d, u, url);
    });
    const seq = useStore.getState().navSeq;
    useStore.getState().navigate("graph", { query: { seed: "x" }, back: "memory k", note: "Graph, walked from x" });
    spy.mockRestore();
    expect(left).toMatchObject({
      loomPlace: { scrollTop: 420, focusKey: "memory:k", inspected: "k", inspectedAgent: "bot", inspectedOwner: null },
    });
    expect(history.state).toMatchObject({ loomBack: "memory k" });
    expect(window.location.pathname + window.location.search).toBe("/graph?namespace=team&seed=x");
    expect(useStore.getState()).toMatchObject({
      view: "graph",
      inspectedMemoryKey: null,
      navSeq: seq + 1,
      navNote: "Graph, walked from x",
    });
  });

  it("Back reopens the memory the entry remembers, and is not a navigation", () => {
    useStore.setState({
      view: "graph",
      inspectedMemoryKey: null,
      identity: { org: "dev", namespace: "team", agent: "loom-ui" },
    });
    history.replaceState({ loomNamespace: "team" }, "", "/memories");
    const seq = useStore.getState().navSeq;
    useStore.getState().arriveFromHistory("memories", {
      scrollTop: 420,
      focusKey: null,
      inspected: "k",
      inspectedAgent: "bot",
      inspectedOwner: null,
    });
    expect(useStore.getState()).toMatchObject({
      view: "memories",
      inspectedMemoryKey: "k",
      inspectedAgent: "bot",
      navSeq: seq,
    });
  });

  it("Back to an entry made in another namespace reopens nothing it names", () => {
    useStore.setState({
      view: "graph",
      inspectedMemoryKey: null,
      pendingConversationThread: null,
      identity: { org: "dev", namespace: "other", agent: "loom-ui" },
    });
    history.replaceState({ loomNamespace: "team" }, "", "/memories?memory=k");
    useStore.getState().arriveFromHistory("memories", {
      scrollTop: 0,
      focusKey: null,
      inspected: "k",
      inspectedAgent: "bot",
      inspectedOwner: null,
    });
    expect(useStore.getState().inspectedMemoryKey).toBeNull();
    // An address boot rewrites (the old /sessions path) keeps the entry's namespace.
    history.replaceState({ loomNamespace: "team" }, "", "/sessions?thread=t");
    useStore.getState().arriveFromHistory("sessions", null);
    expect(useStore.getState().pendingConversationThread).toBeNull();
    // A reload of such an entry opens nothing either.
    useStore.getState().bootView("sessions");
    expect(useStore.getState().pendingConversationThread).toBeNull();
  });

  it("a namespace switch takes the open memory, thread and document out of the URL", () => {
    useStore.setState({ view: "memories", identity: { org: "dev", namespace: "team", agent: "loom-ui" } });
    history.replaceState(null, "", "/memories?memory=k");
    useStore.getState().inspect("k", { agent: "bot" });
    useStore.getState().setIdentity({ org: "dev", namespace: "other", agent: "loom-ui" });
    expect(window.location.search).toBe("?namespace=other");
    expect(useStore.getState().inspectedMemoryKey).toBeNull();
    useStore.getState().bootView("memories");
    expect(useStore.getState().inspectedMemoryKey).toBeNull();
  });

  it("a switch on an entry Back reached forgets the memory that entry remembered", () => {
    useStore.setState({ view: "memories", identity: { org: "dev", namespace: "team", agent: "loom-ui" } });
    history.replaceState({ loomNamespace: "team" }, "", "/memories");
    useStore.getState().inspect("k", { agent: "bot" });
    useStore.getState().navigate("graph");
    // Back to the Memories entry: it remembers "k".
    history.replaceState(
      {
        loomNamespace: "team",
        loomPlace: { scrollTop: 0, focusKey: null, inspected: "k", inspectedAgent: "bot", inspectedOwner: null },
      },
      "",
      "/memories?memory=k",
    );
    useStore.getState().arriveFromHistory("memories", readPlace(history.state));
    expect(useStore.getState().inspectedMemoryKey).toBe("k");
    useStore.getState().setIdentity({ org: "dev", namespace: "other", agent: "loom-ui" });
    // Forward and Back again: the entry reopens nothing of "team" under "other".
    useStore.getState().arriveFromHistory("memories", readPlace(history.state));
    expect(useStore.getState().inspectedMemoryKey).toBeNull();
  });

  it("a switch takes a task out of the URL, from any picker", () => {
    useStore.setState({ view: "tasks", identity: { org: "dev", namespace: "team", agent: "loom-ui" } });
    history.replaceState(null, "", "/tasks?task=t-1");
    useStore.getState().setIdentity({ org: "dev", namespace: "other", agent: "loom-ui" });
    expect(window.location.search).toBe("?namespace=other");
  });

  it("boot's namespace correction keeps a deep link's memory open", () => {
    // A link opened from outside: no namespace on the entry.
    history.replaceState(null, "", "/memories?memory=k");
    useStore.setState({ identity: null, inspectedMemoryKey: null });
    useStore.getState().bootIdentity({ org: "dev", namespace: "first-claim", agent: "loom-ui" });
    useStore.getState().bootView("memories");
    useStore.getState().bootIdentity({ org: "dev", namespace: "joined", agent: "loom-ui" });
    useStore.getState().bootView("memories");
    expect(window.location.search).toBe("?namespace=joined&memory=k");
    expect(useStore.getState().inspectedMemoryKey).toBe("k");
  });

  it("a reload with the drawer open reopens it once boot settles on the entry's namespace", () => {
    history.replaceState(
      { loomNamespace: "joined", loomInspect: { key: "k", agent: "bot" } },
      "",
      "/memories?memory=k",
    );
    useStore.setState({ identity: null, inspectedMemoryKey: null });
    useStore.getState().bootIdentity({ org: "dev", namespace: "first-claim", agent: "loom-ui" });
    useStore.getState().bootView("memories");
    expect(useStore.getState().inspectedMemoryKey).toBeNull();
    useStore.getState().bootIdentity({ org: "dev", namespace: "joined", agent: "loom-ui" });
    useStore.getState().bootView("memories");
    expect(useStore.getState()).toMatchObject({ inspectedMemoryKey: "k", inspectedAgent: "bot" });
  });
});
