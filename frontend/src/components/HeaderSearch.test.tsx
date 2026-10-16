import { render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { HeaderSearch } from "./HeaderSearch";

// The header search is a WAI-ARIA combobox: focus stays in the input while ↑↓ move the
// highlighted option, which the input names through aria-activedescendant.

const inspect = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listMemories: vi.fn(async () => ({ entries: [], truncated: false, total: 0 })),
    listKnowledge: vi.fn(async () => ({ rows: [] })),
    listProfile: vi.fn(async () => ({ entries: [] })),
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
  useStore.setState({ identity: { org: "dev", namespace: "team", agent: "dashboard" }, view: "memories", inspect });
});

describe("HeaderSearch", () => {
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
});
