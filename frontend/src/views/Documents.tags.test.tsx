// A Library row's tag adds that tag to the filter, a second narrows it (every tag must
// match), and pressing an applied tag takes it out. The filters in force show beside the
// search, and a tag filter is asked of the server, not only of the pages already loaded.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
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

  it("stacks tags in the search field, and takes one out again", async () => {
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
    // The field says the tags; no chip beside it repeats them.
    expect(screen.getByLabelText("Search library")).toHaveValue("tag:Specs tag:onboarding");
    expect(within(bar).queryByText("tag:Specs")).toBeNull();
    await waitFor(() =>
      expect(api.listKnowledge).toHaveBeenLastCalledWith(
        { org: "acme", namespace: "team" },
        expect.objectContaining({ tags: expect.arrayContaining(["Specs", "onboarding"]) }),
      ),
    );

    await userEvent.click(within(row("Both")).getByRole("button", { name: "Filter by tag Specs" }));
    await settle();
    expect(shown()).toEqual(["Both", "Onboarding only"]);
    await userEvent.click(within(bar).getAllByRole("button", { name: "Clear filters" })[0]);
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

  // The Library reads `tag:` with the parser Memories uses, the prefix in any case.
  it("reads the tag: prefix in any case", async () => {
    render(<Documents />);
    await settle();
    await userEvent.type(screen.getByLabelText("Search library"), "TAG:onboarding");
    await settle();
    expect(shown()).toEqual(["Both", "Onboarding only"]);
    await waitFor(() =>
      expect(api.listKnowledge).toHaveBeenLastCalledWith(
        { org: "acme", namespace: "team" },
        expect.objectContaining({ tags: ["onboarding"] }),
      ),
    );
  });

  // A tag the server matches nothing for says nothing matches and keeps the search; a failed
  // read is the error state, not "Your library is empty".
  it("a tag that matches nothing keeps the search and offers to clear it", async () => {
    api.listKnowledge.mockImplementation(async (_id: unknown, opts: { tags?: string[] }) => ({
      rows: opts?.tags?.length ? [] : ROWS,
      next_cursor: null,
    }));
    render(<Documents />);
    await settle();
    await userEvent.type(screen.getByLabelText("Search library"), "tag:nomatch");
    expect(
      await screen.findByText((_, el) => el?.textContent === "No documents match tag:nomatch"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Your library is empty")).toBeNull();
    expect(screen.getByLabelText("Search library")).toHaveValue("tag:nomatch");
  });

  it("a failed read says so and offers to retry, never the empty library", async () => {
    api.listKnowledge.mockRejectedValueOnce(new Error("boom")).mockResolvedValueOnce({ rows: ROWS, next_cursor: null });
    render(<Documents />);
    expect(await screen.findByText("Couldn't load documents")).toBeInTheDocument();
    expect(screen.queryByText("Your library is empty")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Both")).toBeInTheDocument();
  });

  // A typed tag waits 250 ms before it is read; until that read lands the page
  // does not say nothing matches it.
  it("does not say a typed tag matches nothing before it is read", async () => {
    render(<Documents />);
    await settle();
    fireEvent.change(screen.getByLabelText("Search library"), { target: { value: "tag:onb" } });
    expect(screen.queryByText(/No documents match/)).toBeNull();
    await settle();
  });
});
