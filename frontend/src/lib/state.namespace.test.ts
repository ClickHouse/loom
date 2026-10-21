import { beforeEach, describe, expect, it, vi } from "vitest";

// The address names the namespace a page shows (`?namespace=`, first): a navigation, a
// switch and boot write it, and a link opens on it. A link to a namespace the account
// cannot read keeps naming it while the page says so, and opens nothing it names. On the
// bare root `?view=` names the page by id or by path, and `?memory=` opens Memories.

const { authOff } = vi.hoisted(() => ({ authOff: { value: false } }));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  isAuthDisabled: () => authOff.value,
}));

import { linkedNamespace, navigationHref, pathnameView, useStore } from "./state";

const team = { org: "dev", namespace: "team", agent: "loom-ui" };

function at(url: string, state: unknown = null): void {
  history.replaceState(state, "", url);
}
const address = () => location.pathname + location.search;

describe("reading a link", () => {
  it("takes ?namespace= as the link's namespace; none when it is absent, empty or `*`", () => {
    at("/memories?namespace=t-1-shared");
    expect(linkedNamespace()).toBe("t-1-shared");
    for (const url of ["/memories", "/memories?namespace=", "/memories?namespace=*"]) {
      at(url);
      expect(linkedNamespace(), url).toBeNull();
    }
  });

  it("names the page on the bare root by view id or by path; a path wins", () => {
    for (const [url, view] of [
      ["/?view=memories", "memories"],
      ["/?view=admin:health", "admin:health"],
      ["/?view=admin/health", "admin:health"],
      ["/?view=library", "knowledge"],
      ["/?view=knowledge", "knowledge"],
      ["/?memory=api%2Frate-limit", "memories"],
      ["/?view=nope", null],
      ["/?namespace=team", null],
      ["/library?view=memories", "knowledge"],
    ] as const) {
      at(url);
      expect(pathnameView(), url).toBe(view);
    }
  });
});

describe("writing the address", () => {
  beforeEach(() => {
    authOff.value = false;
    useStore.setState({
      identity: team,
      deniedNamespace: null,
      view: "memories",
      inspectedMemoryKey: null,
      inspectedAgent: "",
    });
  });

  it("names the page's namespace first in every navigation", () => {
    at("/memories?memory=k");
    expect(navigationHref("memories")).toBe("/memories?namespace=team&memory=k");
    expect(navigationHref("graph", { seed: "x" })).toBe("/graph?namespace=team&seed=x");
    const before = history.length;
    useStore.getState().navigate("recall");
    expect(address()).toBe("/recall?namespace=team");
    expect(history.length).toBe(before + 1);
  });

  it("names none without a namespace, or in auth-off mode", () => {
    useStore.setState({ identity: { ...team, namespace: "" } });
    expect(navigationHref("recall")).toBe("/recall");
    useStore.setState({ identity: team });
    authOff.value = true;
    at("/memories?namespace=other");
    expect(navigationHref("recall")).toBe("/recall");
  });

  it("boot rewrites the address it arrived at in place, keeping the entry's state", () => {
    at("/?view=memories&memory=k", { loomInspect: { key: "k", agent: "bot" } });
    const before = history.length;
    useStore.getState().bootView(pathnameView() ?? "setup");
    expect(address()).toBe("/memories?namespace=team&memory=k");
    expect(history.length).toBe(before);
    expect(useStore.getState()).toMatchObject({ view: "memories", inspectedMemoryKey: "k", inspectedAgent: "bot" });
  });

  it("a switch names the new namespace in place and drops what belonged to the old one", () => {
    at("/memories?namespace=team&memory=k&section=insights");
    const before = history.length;
    useStore.getState().setIdentity({ ...team, namespace: "other" });
    expect(address()).toBe("/memories?namespace=other&section=insights");
    expect(history.length).toBe(before);
  });

  it("Back to an entry made in another namespace names the page's, and nothing of the other", () => {
    at("/conversations?namespace=old&thread=t-1", { loomNamespace: "old" });
    useStore.getState().arriveFromHistory("sessions", null);
    expect(address()).toBe("/conversations?namespace=team");
    // An entry of this namespace keeps what it names.
    at("/conversations?namespace=team&thread=t-2", { loomNamespace: "team" });
    useStore.getState().arriveFromHistory("sessions", null);
    expect(address()).toBe("/conversations?namespace=team&thread=t-2");
    expect(useStore.getState().pendingConversationThread).toEqual({ threadId: "t-2" });
  });
});

describe("a link to a namespace the account cannot read", () => {
  beforeEach(() => {
    authOff.value = false;
    useStore.setState({ identity: null, deniedNamespace: null, view: "setup", inspectedMemoryKey: null });
    at("/memories?namespace=t-x-shared&memory=k");
    useStore.getState().bootIdentity(team, "t-x-shared");
    useStore.getState().bootView("memories");
  });

  it("keeps naming that namespace, and opens nothing the link names on the one under it", () => {
    expect(address()).toBe("/memories?namespace=t-x-shared&memory=k");
    expect(useStore.getState()).toMatchObject({
      deniedNamespace: "t-x-shared",
      identity: team,
      inspectedMemoryKey: null,
    });
  });

  it("a navigation leaves it, in the namespace the picker shows", () => {
    useStore.getState().navigate("recall");
    expect(useStore.getState().deniedNamespace).toBeNull();
    expect(address()).toBe("/recall?namespace=team");
  });

  it("any pick leaves it in place, the namespace the page already has included", () => {
    const before = history.length;
    useStore.getState().setIdentity(team);
    expect(useStore.getState().deniedNamespace).toBeNull();
    expect(address()).toBe("/memories?namespace=team");
    expect(history.length).toBe(before);
  });
});
