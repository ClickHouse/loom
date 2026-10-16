import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
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
    expect(useStore.getState().pendingConversationThread).toBe("t-1");
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
