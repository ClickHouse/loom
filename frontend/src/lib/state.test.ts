import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { navigationHref, pathToView, useIdentity, useStore, viewHref, viewToPath, type View } from "./state";

function setLocation(pathAndQuery: string): void {
  window.history.replaceState(null, "", pathAndQuery);
}

describe("navigationHref keeps only the query keys the target view owns", () => {
  beforeEach(() => {
    setLocation("/me/graph?seed=alpha&repo=loom&stray=1");
  });

  it("Graph keeps ?seed=, Repositories keeps ?repo=", () => {
    expect(navigationHref("graph")).toBe("/me/graph?seed=alpha");
    expect(navigationHref("code")).toBe("/me/repositories?repo=loom");
  });

  it("a view that owns no key gets a bare URL", () => {
    expect(navigationHref("memories")).toBe("/me/memories");
    expect(navigationHref("admin:health")).toBe("/me/admin/health");
    expect(navigationHref("browse")).toBe("/me/spaces");
  });

  it("carries nothing when the owned key is absent", () => {
    setLocation("/me/memories");
    expect(navigationHref("graph")).toBe("/me/graph");
    expect(navigationHref("code")).toBe("/me/repositories");
  });

  it("Graph keeps a ?path= deep link, other views drop it", () => {
    setLocation("/me/graph?path=a-%3Eb");
    expect(navigationHref("graph")).toBe("/me/graph?path=a-%3Eb");
    expect(navigationHref("memories")).toBe("/me/memories");
  });

  it("re-encodes the value it keeps", () => {
    setLocation("/me/graph?seed=a%20b");
    expect(navigationHref("graph")).toBe("/me/graph?seed=a+b");
  });
});

describe("view ↔ path", () => {
  it("round-trips every view", () => {
    const views: View[] = ["memories", "graph", "code", "browse", "setup", "admin:review_queue", "admin:users"];
    for (const v of views) expect(pathToView(viewToPath(v))).toBe(v);
  });

  it("a page's path reads like its label, and only that path resolves", () => {
    expect(viewHref("knowledge")).toBe("/me/library");
    expect(viewHref("sessions")).toBe("/me/conversations");
    expect(viewHref("sharing")).toBe("/me/flow");
    expect(viewHref("setup")).toBe("/me/settings");
    // Settings owns `?tab=`; a navigation to it keeps the open tab and nothing else.
    window.history.replaceState(null, "", "/me/settings?tab=memory&repo=x");
    expect(navigationHref("setup")).toBe("/me/settings?tab=memory");
    window.history.replaceState(null, "", "/me/settings");
    for (const old of ["knowledge", "sessions", "sharing", "setup", "discover", "browse", "code"]) {
      expect(pathToView(old), `old path /me/${old}`).toBeNull();
    }
  });

  it("maps browse to /me/spaces and admin views to a slash", () => {
    expect(viewHref("browse")).toBe("/me/spaces");
    expect(viewHref("admin:health")).toBe("/me/admin/health");
  });

  it("Repositories lives at /me/repositories", () => {
    expect(viewHref("code")).toBe("/me/repositories");
    expect(pathToView("repositories")).toBe("code");
  });

  it("rejects unknown and empty paths", () => {
    expect(pathToView("")).toBeNull();
    expect(pathToView("nope")).toBeNull();
  });
});

describe("navigating actions mirror the view into the URL", () => {
  beforeEach(() => {
    setLocation("/me/recall");
  });

  it("a tag search lands on /me/memories as one new history entry", () => {
    const before = window.history.length;
    useStore.getState().searchByTag("infra");
    expect(useStore.getState().view).toBe("memories");
    expect(window.location.pathname).toBe("/me/memories");
    expect(window.history.length).toBe(before + 1);
  });

  it("opening a conversation thread lands on /me/conversations", () => {
    useStore.getState().openConversationThread("t-1");
    expect(useStore.getState().view).toBe("sessions");
    expect(useStore.getState().pendingConversationThread).toBe("t-1");
    expect(window.location.pathname).toBe("/me/conversations");
  });

  it("editing a profile section lands on /me/profile, and clearing the request pushes nothing", () => {
    useStore.getState().requestEditProfile("role");
    expect(window.location.pathname).toBe("/me/profile");
    const after = window.history.length;
    useStore.getState().requestEditProfile(null);
    expect(window.location.pathname).toBe("/me/profile");
    expect(window.history.length).toBe(after);
  });

  it("drops query keys the target view does not own", () => {
    setLocation("/me/graph?seed=alpha");
    useStore.getState().searchByTag("infra");
    expect(window.location.pathname + window.location.search).toBe("/me/memories");
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
