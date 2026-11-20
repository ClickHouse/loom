import { beforeEach, describe, expect, it, vi } from "vitest";

// The address names the namespace a page shows (`?namespace=`, first): a navigation, a
// switch and boot write it, and a link opens on it. A link to a namespace the account
// cannot read keeps naming it while the page says so, and opens nothing it names. Back and
// Forward never switch namespace, and an entry made in another one opens nothing it names.
// On the bare root `?view=` names the page by id or by path, and `?memory=` opens Memories.

const { authOff } = vi.hoisted(() => ({ authOff: { value: false } }));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  isAuthDisabled: () => authOff.value,
}));

import { readPlace } from "./place";
import { addressPageNamespace, linkedNamespace, navigationHref, pathnameView, useStore } from "./state";

const team = { org: "dev", namespace: "team", agent: "loom-ui" };

function at(url: string, state: unknown = null): void {
  history.replaceState(state, "", url);
}
const address = () => location.pathname + location.search;

/** Back one entry, then what App's popstate handler does for a view change. */
async function back(view: Parameters<ReturnType<typeof useStore.getState>["arriveFromHistory"]>[0]) {
  await new Promise((resolve) => {
    window.addEventListener("popstate", resolve, { once: true });
    history.back();
  });
  useStore.getState().arriveFromHistory(view, readPlace(history.state));
}

describe("reading a link", () => {
  it("takes ?namespace= as the link's namespace; none when it is absent or empty", () => {
    at("/memories?namespace=group-a-shared");
    expect(linkedNamespace()).toBe("group-a-shared");
    for (const url of ["/memories", "/memories?namespace="]) {
      at(url);
      expect(linkedNamespace(), url).toBeNull();
    }
  });

  it("takes no value that is not a namespace name: `*`, a sentence, capitals, one too long", () => {
    for (const value of ["*", "Your account is locked, call support", "Team", "-team", "team/notes", "a".repeat(129)]) {
      at(`/memories?namespace=${encodeURIComponent(value)}`);
      expect(linkedNamespace(), value).toBeNull();
    }
    at(`/memories?namespace=${"a".repeat(128)}`);
    expect(linkedNamespace()).toBe("a".repeat(128));
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
    at("/memories?namespace=team&memory=k");
    const before = history.length;
    useStore.getState().setIdentity({ ...team, namespace: "other" });
    expect(address()).toBe("/memories?namespace=other");
    expect(history.length).toBe(before);
  });

  it.each([
    ["tasks", "/tasks", "task"],
    ["knowledge", "/library", "doc"],
    ["sessions", "/conversations", "thread"],
  ] as const)("navigation to %s drops a denied namespace's object", (view, path, key) => {
    at(`${path}?namespace=unreadable&${key}=foreign`);
    useStore.setState({
      deniedNamespace: "unreadable",
      pendingDocSourceUri: "foreign",
      pendingConversationThread: { threadId: "foreign" },
      recallRunFilter: "foreign",
    });
    useStore.getState().navigate(view);
    expect(address()).toBe(`${path}?namespace=team`);
    expect(useStore.getState()).toMatchObject({
      deniedNamespace: null,
      pendingDocSourceUri: null,
      pendingConversationThread: null,
      recallRunFilter: null,
    });
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

  it("Back to a collection's document made in another namespace keeps and opens it; a namespace's does not", () => {
    useStore.setState({ pendingDocSourceUri: null });
    at("/library?namespace=old&collection=col-c&doc=x.md", { loomNamespace: "old" });
    useStore.getState().arriveFromHistory("knowledge", null);
    expect(address()).toBe("/library?namespace=team&collection=col-c&doc=x.md");
    expect(useStore.getState().pendingDocSourceUri).toBe("x.md");
    useStore.setState({ pendingDocSourceUri: null });
    at("/library?namespace=old&doc=y.md", { loomNamespace: "old" });
    useStore.getState().arriveFromHistory("knowledge", null);
    expect(address()).toBe("/library?namespace=team");
    expect(useStore.getState().pendingDocSourceUri).toBeNull();
  });

  it("a move inside one view made before a switch names the page's namespace, keeping the view's keys", () => {
    // A Graph walk's entry carries no stamp of the app's; its address names the old namespace.
    at("/graph?namespace=old&seed=alpha", { graphSeed: "alpha" });
    addressPageNamespace();
    expect(address()).toBe("/graph?namespace=team&seed=alpha");
    expect(history.state).toMatchObject({ graphSeed: "alpha" });
  });

  it("Back onto a link's entry after a switch opens nothing of the link's namespace", async () => {
    useStore.setState({ identity: null });
    at("/memories?namespace=group-a-shared&memory=k");
    useStore.getState().bootIdentity({ ...team, namespace: "group-a-shared" });
    useStore.getState().bootView("memories");
    expect(useStore.getState().inspectedMemoryKey).toBe("k");
    useStore.getState().navigate("recall");
    useStore.getState().setIdentity(team);
    await back("memories");
    expect(useStore.getState().inspectedMemoryKey).toBeNull();
    expect(address()).toBe("/memories?namespace=team");
  });
});

describe("an entry Back reaches from another namespace", () => {
  const joined = { ...team, namespace: "joined" };
  beforeEach(() => {
    authOff.value = false;
    useStore.setState({ identity: joined, deniedNamespace: null, view: "memories", inspectedMemoryKey: null });
    at("/memories?namespace=joined", { loomNamespace: "joined" });
  });

  it("becomes this namespace's: a memory opened on it reopens after a reload", async () => {
    useStore.getState().navigate("recall");
    useStore.getState().setIdentity(team);
    await back("memories");
    expect(history.state).toMatchObject({ loomNamespace: "team", loomInspect: null });
    useStore.getState().inspect("team-key", { agent: "bot" });
    expect(address()).toBe("/memories?namespace=team&memory=team-key");
    // A reload: boot reads the address and settles on `team`.
    useStore.setState({ identity: null, inspectedMemoryKey: null, inspectedAgent: "" });
    useStore.getState().bootIdentity(team);
    useStore.getState().bootView("memories");
    expect(useStore.getState()).toMatchObject({ inspectedMemoryKey: "team-key", inspectedAgent: "bot" });
  });

  it("after the account loses that namespace, a reload opens nothing under the no-access page", async () => {
    useStore.getState().navigate("recall");
    useStore.getState().setIdentity(team);
    await back("memories");
    useStore.getState().inspect("team-key", { agent: "bot" });
    // A reload once `team` is unreadable: the page on `joined`, the address still on `team`.
    useStore.setState({ identity: null, inspectedMemoryKey: null, inspectedAgent: "" });
    useStore.getState().bootIdentity(joined, "team");
    useStore.getState().bootView("memories");
    expect(useStore.getState()).toMatchObject({ deniedNamespace: "team", inspectedMemoryKey: null });
  });
});

describe("an entry Back reaches whose address names a collection", () => {
  beforeEach(() => {
    useStore.setState({ identity: team, deniedNamespace: null, view: "memories" });
    at("/memories?namespace=team");
  });

  it("never makes the collection the header's namespace: the address names the page's", async () => {
    // An entry whose namespace is a collection.
    history.pushState({ loomNamespace: "col-research" }, "", "/memories?namespace=col-research&memory=k");
    useStore.getState().navigate("recall");
    await back("memories");
    expect(useStore.getState().identity).toEqual(team);
    expect(address()).toBe("/memories?namespace=team");
    // The same when only the address moves, as a move inside one view does.
    at("/memories?namespace=col-research");
    addressPageNamespace();
    expect(useStore.getState().identity).toEqual(team);
    expect(address()).toBe("/memories?namespace=team");
  });
});

describe("what boot records of a link", () => {
  beforeEach(() => useStore.setState({ identity: null, deniedNamespace: null, linkOpened: null }));

  it("keeps an equal identity's object, so views do not read again", () => {
    useStore.getState().bootIdentity(team);
    const first = useStore.getState().identity;
    useStore.getState().bootIdentity({ ...team });
    expect(useStore.getState().identity).toBe(first);
  });

  it("names the namespace a link opened until a switch", () => {
    useStore.getState().bootIdentity({ ...team, namespace: "group-a-shared" }, null, "group-a-shared");
    expect(useStore.getState().linkOpened).toBe("group-a-shared");
    at("/memories?namespace=group-a-shared");
    useStore.getState().setIdentity(team);
    expect(useStore.getState().linkOpened).toBeNull();
  });
});

describe("a link to a namespace the account cannot read", () => {
  beforeEach(() => {
    authOff.value = false;
    useStore.setState({ identity: null, deniedNamespace: null, view: "setup", inspectedMemoryKey: null });
    at("/memories?namespace=group-b-shared&memory=k");
    useStore.getState().bootIdentity(team, "group-b-shared");
    useStore.getState().bootView("memories");
  });

  it("keeps naming that namespace, and opens nothing the link names on the one under it", () => {
    expect(address()).toBe("/memories?namespace=group-b-shared&memory=k");
    expect(useStore.getState()).toMatchObject({
      deniedNamespace: "group-b-shared",
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

  it("Back onto its page after leaving opens nothing the link named, in the namespace the page reads", async () => {
    useStore.getState().navigate("recall");
    await back("memories");
    expect(useStore.getState()).toMatchObject({ deniedNamespace: null, inspectedMemoryKey: null });
    expect(address()).toBe("/memories?namespace=team");
  });

  it("a memory opened from the palette leaves it first: the address and the read name one namespace", () => {
    useStore.getState().inspect("team-key", { agent: "bot" });
    expect(useStore.getState()).toMatchObject({ deniedNamespace: null, inspectedMemoryKey: "team-key" });
    expect(address()).toBe("/memories?namespace=team&memory=team-key");
  });
});
