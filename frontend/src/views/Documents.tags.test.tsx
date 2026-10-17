// A Library row's tag adds that tag to the filter, a second narrows it (every tag must
// match), and pressing an applied tag takes it out. The filters in force show beside the
// search, and a tag filter is asked of the server, not only of the pages already loaded.
import { act, cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { Documents } from "./Documents";

const api = vi.hoisted(() => ({ listKnowledge: vi.fn() }));
vi.mock("../api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api")>()), ...api }));

const doc = (title: string, tags: string[]) => ({
  source_uri: `docs/${title}`,
  title,
  original_filename: "",
  description: "",
  tags,
  has_blob: false,
});
const ROWS = [
  doc("Both", ["Specs", "onboarding"]),
  doc("Specs only", ["Specs"]),
  doc("Onboarding only", ["onboarding"]),
];

async function settle() {
  for (let i = 0; i < 3; i++)
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30));
    });
}
const shown = () => ROWS.map((r) => r.title).filter((t) => screen.queryByText(t));
const row = (title: string) => screen.getByText(title).closest("[data-doc-row]") as HTMLElement;

describe("Library: filtering by tags", () => {
  beforeEach(() => {
    api.listKnowledge.mockImplementation(async (_id: unknown, opts: { tags?: string[] }) => ({
      rows: opts?.tags?.length ? ROWS.filter((r) => r.tags.some((t) => opts.tags!.includes(t))) : ROWS,
      total: ROWS.length,
      next_cursor: null,
    }));
    useStore.setState({ identity: { org: "acme", namespace: "team", agent: "loom-ui" }, view: "knowledge" });
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    useStore.setState({ identity: null });
  });

  it("stacks tags, shows them by the search, and takes one out again", async () => {
    render(<Documents />);
    await settle();
    await userEvent.click(within(row("Specs only")).getByRole("button", { name: "Filter by tag Specs" }));
    await settle();
    expect(shown()).toEqual(["Both", "Specs only"]);
    await userEvent.click(within(row("Both")).getByRole("button", { name: "Filter by tag onboarding" }));
    await settle();
    expect(shown()).toEqual(["Both"]);
    expect(within(row("Both")).getByRole("button", { name: "Filter by tag onboarding" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    const bar = screen.getByLabelText("Search and filters");
    expect(within(bar).getByText("tag:Specs")).toBeInTheDocument();
    await waitFor(() =>
      expect(api.listKnowledge).toHaveBeenLastCalledWith(
        { org: "acme", namespace: "team" },
        expect.objectContaining({ tags: expect.arrayContaining(["Specs", "onboarding"]) }),
      ),
    );

    await userEvent.click(within(bar).getByRole("button", { name: "Remove tag:Specs" }));
    await settle();
    expect(shown()).toEqual(["Both", "Onboarding only"]);
    await userEvent.click(within(bar).getByRole("button", { name: "Clear filters" }));
    await settle();
    expect(shown()).toEqual(["Both", "Specs only", "Onboarding only"]);
  });

  it("matches a tag whole, in any case", async () => {
    render(<Documents />);
    await settle();
    await userEvent.type(screen.getByLabelText("Search library"), "tag:spec");
    await settle();
    expect(shown()).toEqual([]);
    await userEvent.type(screen.getByLabelText("Search library"), "s");
    await settle();
    expect(shown()).toEqual(["Both", "Specs only"]);
  });
});
