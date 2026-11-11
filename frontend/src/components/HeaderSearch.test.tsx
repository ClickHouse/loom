import { fireEvent, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { HeaderSearch } from "./HeaderSearch";
import { sharedMine } from "../lib/sharedReads";

// The header search is a WAI-ARIA combobox: focus stays in the input while ↑↓ move the
// highlighted option, which the input names through aria-activedescendant.

const inspect = vi.fn();
const inspectProfile = vi.fn();
// What the empty box lists: the newest memories and the profile sections.
const catalog = vi.hoisted(() => ({
  recent: [] as Array<{ memory_key: string; agent: string }>,
  profile: [] as Array<{ label: string; value: string }>,
  docs: [] as Array<{ source_uri: string; title: string; tags: string[]; chunk_count: number }>,
}));
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listMemories: vi.fn(async () => ({ entries: catalog.recent, truncated: false, total: catalog.recent.length })),
    listKnowledge: vi.fn(async () => ({ rows: catalog.docs })),
    listProfile: vi.fn(async () => ({ entries: catalog.profile })),
    browseNamespaces: vi.fn(async () => []),
    searchMemory: vi.fn(async () => ({
      results: [
        { memory_key: "coffee/order", agent: "a", memory_type: "semantic", content_excerpt: "flat white" },
        { memory_key: "coffee/shop", agent: "a", memory_type: "semantic", content_excerpt: "corner cafe" },
      ],
    })),
  };
});
vi.mock("../lib/sharedReads", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/sharedReads")>();
  return { ...actual, sharedMine: vi.fn(async () => []) };
});

beforeEach(() => {
  inspect.mockReset();
  inspectProfile.mockReset();
  catalog.recent = [];
  catalog.profile = [];
  catalog.docs = [];
  useStore.setState({
    identity: { org: "dev", namespace: "team", agent: "dashboard" },
    view: "memories",
    inspect,
    inspectProfile,
  });
});

describe("HeaderSearch", () => {
  // The group heading names the type, so a row leads with the type's icon
  // instead of a coloured "memory" badge; line 1 is the key with the memory type at its
  // end, line 2 the excerpt.
  it("leads each memory row with its type icon, not a badge, and puts the type at the end", async () => {
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(screen.getByRole("combobox", { name: "Search Loom" }), "coffee");
    await screen.findByRole("listbox", { name: "Search results" });
    const [first] = screen.getAllByRole("option");
    expect(first).not.toHaveTextContent(/^memory/);
    expect(first.querySelector('[class*="badge"]')).toBeNull();
    expect(first.querySelector("svg")).not.toBeNull();
    expect(first).toHaveTextContent("coffee/order");
    expect(first).toHaveTextContent("semantic");
    expect(first).toHaveTextContent("flat white");
    expect(first.textContent?.indexOf("coffee/order")).toBeLessThan(first.textContent?.indexOf("semantic") ?? 0);
  });

  // A Library result draws its tags as the outlined chips a row draws, and "Best matches"
  // has an icon of its own, not the clock of "Recent memories".
  it("draws a document's tags as the rows do, and tells Best matches from Recent by icon", async () => {
    catalog.recent = [{ memory_key: "coffee/old", agent: "a" }];
    catalog.docs = [{ source_uri: "s3://coffee.md", title: "Coffee guide", tags: ["beans", "brew"], chunk_count: 2 }];
    const user = userEvent.setup();
    render(<HeaderSearch />);
    const input = screen.getByRole("combobox", { name: "Search Loom" });
    await user.click(input);
    const recent = await screen.findByRole("group", { name: "Recent memories" });
    const recentIcon = recent.querySelector("svg")?.outerHTML;
    await user.type(input, "coffee");
    const best = await screen.findByRole("group", { name: "Best matches" });
    expect(best.querySelector("svg")?.outerHTML).not.toBe(recentIcon);
    const library = await screen.findByRole("group", { name: "Library" });
    expect(library).not.toHaveTextContent("#beans");
    // The outlined chip a row's TagList draws.
    expect(screen.getByText("beans").closest(".chip")).toHaveClass("chip-outline");
  });

  it("draws a Jump to namespace as a button: the switch keeps the page, so it has no href", async () => {
    vi.mocked(sharedMine).mockResolvedValueOnce([
      { namespace: "coffee-team", visibility: "private", description: "", role: "writer", status: "active" },
    ] as never);
    const user = userEvent.setup();
    render(<HeaderSearch />);
    const input = screen.getByRole("combobox", { name: "Search Loom" });
    await user.click(input);
    await user.type(input, "coffee");
    const jump = await screen.findByRole("group", { name: "Jump to" });
    const option = within(jump).getByText("coffee-team").closest("button, a");
    expect(option?.tagName).toBe("BUTTON");
    expect(option).not.toHaveAttribute("href");
  });

  it("offers no collection as a namespace to jump to", async () => {
    vi.mocked(sharedMine).mockResolvedValueOnce([
      { namespace: "coffee-team", visibility: "private", description: "", role: "writer", status: "active" },
      {
        namespace: "col-coffee",
        kind: "collection",
        title: "Coffee",
        visibility: "private",
        description: "",
        role: "admin",
        status: "active",
      },
    ] as never);
    const user = userEvent.setup();
    render(<HeaderSearch />);
    const input = screen.getByRole("combobox", { name: "Search Loom" });
    await user.click(input);
    await user.type(input, "coffee");
    const jump = await screen.findByRole("group", { name: "Jump to" });
    expect(within(jump).getByText("coffee-team")).toBeInTheDocument();
    expect(within(jump).queryByText("col-coffee")).toBeNull();
  });

  it("rings the option the keys move to, and only tints the one the pointer is on", async () => {
    const user = userEvent.setup();
    render(<HeaderSearch />);
    const input = screen.getByRole("combobox", { name: "Search Loom" });
    await user.click(input);
    await user.type(input, "coffee");
    const best = await screen.findByRole("group", { name: "Best matches" });
    const [first, second] = within(best).getAllByRole("option");
    await user.hover(second);
    expect(second).toHaveAttribute("aria-selected", "true");
    expect(second).not.toHaveAttribute("data-by-keys");
    await user.keyboard("{ArrowUp}");
    const active = screen.getAllByRole("option").find((o) => o.getAttribute("aria-selected") === "true");
    expect(active).toHaveAttribute("data-by-keys");
    expect(first).toBeInTheDocument();
  });

  it("names each best match's agent, so two agents' rows of one key tell apart", async () => {
    const user = userEvent.setup();
    render(<HeaderSearch />);
    const input = screen.getByRole("combobox", { name: "Search Loom" });
    await user.click(input);
    await user.type(input, "coffee");
    const best = await screen.findByRole("group", { name: "Best matches" });
    expect(within(best).getAllByText(/^a · /).length).toBeGreaterThan(0);
  });

  it("highlights options with the arrow keys and opens the highlighted one on Enter", async () => {
    const user = userEvent.setup();
    render(<HeaderSearch />);
    const input = screen.getByRole("combobox", { name: "Search Loom" });
    await user.type(input, "coffee");
    const listbox = await screen.findByRole("listbox", { name: "Search results" });
    expect(input).toHaveAttribute("aria-expanded", "true");
    expect(input).toHaveAttribute("aria-controls", listbox.id);
    expect(screen.getByRole("group", { name: "Best matches" })).toBeInTheDocument();

    const options = screen.getAllByRole("option");
    expect(options).toHaveLength(2);
    await waitFor(() => expect(input).toHaveAttribute("aria-activedescendant", options[0].id));
    expect(options[0]).toHaveAttribute("aria-selected", "true");

    await user.keyboard("{ArrowDown}");
    expect(input).toHaveAttribute("aria-activedescendant", options[1].id);
    expect(options[1]).toHaveAttribute("aria-selected", "true");
    expect(input).toHaveFocus();

    await user.keyboard("{Enter}");
    expect(inspect).toHaveBeenCalledWith("coffee/shop", { agent: "a" });
  });

  it("opens the panel on focus and keeps it open", async () => {
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.tab();
    expect(screen.getByRole("combobox", { name: "Search Loom" })).toHaveFocus();
    expect(await screen.findByText("Syntax")).toBeInTheDocument();
    // In a browser the focus that opened the panel reaches Radix's outside-focus listener
    // once the panel has mounted; the field is the panel's anchor, so the panel stays.
    fireEvent.focusIn(screen.getByRole("combobox", { name: "Search Loom" }));
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.getByText("Syntax")).toBeInTheDocument();
  });

  it("closes the panel when focus leaves the field for another control", async () => {
    const user = userEvent.setup();
    render(
      <>
        <HeaderSearch />
        <button type="button">New</button>
      </>,
    );
    await user.tab();
    expect(await screen.findByText("Syntax")).toBeInTheDocument();
    // A combobox's popup closes when focus leaves the combobox (WAI-ARIA), however fast.
    await user.tab();
    expect(screen.getByRole("button", { name: "New" })).toHaveFocus();
    await waitFor(() => expect(screen.queryByText("Syntax")).toBeNull());
  });

  it("moves through the empty box's recent memories and profile with the arrow keys", async () => {
    catalog.recent = [
      { memory_key: "notes/one", agent: "a" },
      { memory_key: "notes/two", agent: "b" },
    ];
    catalog.profile = [{ label: "timezone", value: "UTC" }];
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.tab();
    const input = screen.getByRole("combobox", { name: "Search Loom" });
    await screen.findByRole("group", { name: "Recent memories" });
    await screen.findByRole("group", { name: "Your profile" });
    const options = screen.getAllByRole("option");
    expect(options).toHaveLength(3);
    // Nothing is highlighted until ↓, so Enter in an empty box opens nothing.
    expect(input).not.toHaveAttribute("aria-activedescendant");

    await user.keyboard("{ArrowDown}{ArrowDown}");
    expect(input).toHaveAttribute("aria-activedescendant", options[1].id);
    await user.keyboard("{Enter}");
    expect(inspect).toHaveBeenCalledWith("notes/two", { agent: "b" });

    await user.click(input);
    await user.keyboard("{ArrowDown}{ArrowDown}{ArrowDown}{Enter}");
    expect(inspectProfile).toHaveBeenCalledWith("timezone");
  });
});
