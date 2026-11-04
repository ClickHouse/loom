import { render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { HeaderSearch } from "./HeaderSearch";

// Picking a namespace in the header search switches the namespace and keeps the page; a
// toast says what changed. The field's only hint is the platform's palette shortcut.

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listMemories: vi.fn(async () => ({ entries: [], truncated: false, total: 0 })),
    listKnowledge: vi.fn(async () => ({ rows: [] })),
    listProfile: vi.fn(async () => ({ entries: [] })),
    browseNamespaces: vi.fn(async () => []),
    searchMemory: vi.fn(async () => ({ results: [] })),
    setSavedNamespace: vi.fn(),
  };
});
vi.mock("../lib/sharedReads", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/sharedReads")>();
  return {
    ...actual,
    sharedMine: vi.fn(async () => [
      {
        namespace: "team-alerts",
        role: "writer",
        status: "active",
        visibility: "private",
        description: "",
        joined_at: null,
        is_new: false,
        kind: "memory",
      },
    ]),
  };
});
const { toast } = vi.hoisted(() => ({ toast: vi.fn() }));
vi.mock("./Toast", async (importOriginal) => ({ ...(await importOriginal<typeof import("./Toast")>()), toast }));

beforeEach(() => {
  toast.mockReset();
  history.replaceState(null, "", "/library");
  useStore.setState({ identity: { org: "dev", namespace: "team", agent: "dashboard" }, view: "knowledge" });
});

describe("HeaderSearch namespace switch", () => {
  it("keeps the current view and toasts the switch", async () => {
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(screen.getByRole("combobox", { name: "Search Loom" }), "alerts");
    await user.click(await screen.findByRole("option", { name: /team-alerts/ }));
    await waitFor(() => expect(useStore.getState().identity?.namespace).toBe("team-alerts"));
    expect(useStore.getState().view).toBe("knowledge");
    expect(location.pathname).toBe("/library");
    expect(toast).toHaveBeenCalledWith("Switched to team-alerts", "info");
  });

  it("drops the URL keys that name an object of the namespace it left, and keeps the entry's state", async () => {
    history.replaceState({ loomPlace: { scrollTop: 40 } }, "", "/library?doc=s3%3A%2F%2Fa&view=grid");
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(screen.getByRole("combobox", { name: "Search Loom" }), "alerts");
    await user.click(await screen.findByRole("option", { name: /team-alerts/ }));
    await waitFor(() => expect(useStore.getState().identity?.namespace).toBe("team-alerts"));
    expect(location.pathname + location.search).toBe("/library?namespace=team-alerts&view=grid");
    expect(history.state).toMatchObject({ loomPlace: { scrollTop: 40 } });
  });

  it("drops ?task= on Tasks, ?thread= on Conversations and ?memory= on Memories", async () => {
    const user = userEvent.setup();
    for (const [url, want] of [
      ["/tasks?inbox=bot&task=t-1", "/tasks?namespace=team-alerts&inbox=bot"],
      ["/conversations?thread=th-1", "/conversations?namespace=team-alerts"],
      ["/memories?memory=a%2Fb", "/memories?namespace=team-alerts"],
    ]) {
      history.replaceState(null, "", url);
      useStore.setState({ identity: { org: "dev", namespace: "team", agent: "dashboard" } });
      const { unmount } = render(<HeaderSearch />);
      await user.type(screen.getByRole("combobox", { name: "Search Loom" }), "alerts");
      await user.click(await screen.findByRole("option", { name: /team-alerts/ }));
      await waitFor(() => expect(useStore.getState().identity?.namespace).toBe("team-alerts"));
      expect(location.pathname + location.search).toBe(want);
      unmount();
    }
  });

  it("shows no navigate/open hint strip, and the shortcut as the platform prints it", async () => {
    const user = userEvent.setup();
    render(<HeaderSearch />);
    // jsdom is not an Apple platform.
    expect(screen.getByText("Ctrl K")).toBeInTheDocument();
    await user.type(screen.getByRole("combobox", { name: "Search Loom" }), "alerts");
    await screen.findByRole("option", { name: /team-alerts/ });
    expect(screen.queryByText(/navigate · /)).toBeNull();
  });
});

describe("HeaderSearch's more documents", () => {
  it("opens the header namespace's Library, never the collection the Library shows", async () => {
    const api = await import("../api");
    vi.mocked(api.listKnowledge).mockResolvedValue({
      rows: Array.from({ length: 12 }, (_, i) => ({
        source_uri: `d${i}`,
        title: `policy ${i}`,
        chunk_count: 1,
        tags: [],
      })),
    } as never);
    history.replaceState(null, "", "/library?namespace=team&collection=col-a");
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(screen.getByRole("combobox", { name: "Search Loom" }), "policy");
    await user.click(await screen.findByRole("button", { name: /more documents/ }));
    expect(new URLSearchParams(location.search).get("collection")).toBeNull();
    expect(location.pathname).toBe("/library");
  });
});
