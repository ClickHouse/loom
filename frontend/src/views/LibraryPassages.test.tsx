/**
 * Finding a passage in the Library: the panel opens, a query runs `knowledge.search` in
 * the namespace shown, each hit is cited by revision and place, and a hit opens its
 * document; no match and a failure each say so.
 */
import { cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  searchKnowledge: vi.fn(),
}));
import { type KnowledgeHit, searchKnowledge } from "../api";
import { axeViolations } from "../test/axe";
import { LibraryPassages } from "./LibraryPassages";

const identity = { org: "o", namespace: "docs" };

const HIT: KnowledgeHit = {
  source_uri: "repo://handbook/retention.pdf",
  chunk_idx: 4,
  content: "Raw events are kept for 30 days and then dropped by the TTL.",
  section: "Retention > Events",
  score: 0.8,
  revision: 3,
  format: "pdf",
  block_type: "paragraph",
  locator: { page_start: 7, page_end: 7 },
  keys_matched: [],
};

async function openAndFind(query: string, onOpen = vi.fn(), onHitsShown = vi.fn()) {
  // Inside the page's main landmark, as the Library renders it.
  render(
    <main>
      <LibraryPassages identity={identity} navEnabled onHitsShown={onHitsShown} onOpen={onOpen} />
    </main>,
  );
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Passage to find"), query);
  await user.click(screen.getByRole("button", { name: "Find" }));
  return { user, onOpen, onHitsShown };
}

describe("LibraryPassages", () => {
  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
  });

  it("says what it searches, tied to its field", () => {
    render(<LibraryPassages identity={identity} navEnabled onHitsShown={vi.fn()} onOpen={vi.fn()} />);
    expect(screen.getByLabelText("Passage to find")).toHaveAccessibleDescription(/whatever the tag filter/);
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("cites each hit by revision and place, and opens its document", async () => {
    vi.mocked(searchKnowledge).mockResolvedValue([
      HIT,
      {
        ...HIT,
        source_uri: "repo://conf/limits.toml",
        chunk_idx: 0,
        format: "toml",
        revision: 1,
        section: "",
        locator: { line_start: 3, line_end: 4, key_path: "retention.days" },
        keys_matched: ["retention.days"],
      },
    ]);
    const { user, onOpen, onHitsShown } = await openAndFind("how long are events kept");
    const list = await screen.findByRole("list", { name: "Passages found" });
    expect(vi.mocked(searchKnowledge).mock.calls[0].slice(0, 2)).toEqual([identity, "how long are events kept"]);
    expect(screen.getByRole("status")).toHaveTextContent("2 passages");
    // The list below hands its row keys over while hits show.
    expect(onHitsShown).toHaveBeenLastCalledWith(true);
    const [first, second] = within(list).getAllByRole("listitem");
    expect(first).toHaveTextContent("retention.pdf");
    expect(first).toHaveTextContent("repo://handbook/retention.pdf");
    expect(first).toHaveTextContent("Retention > Events");
    expect(first).toHaveTextContent("revision 3, page 7");
    expect(first).toHaveTextContent("Raw events are kept for 30 days");
    expect(second).toHaveTextContent("revision 1, key retention.days");
    expect(await axeViolations(document.body)).toEqual([]);
    expect(within(second).getByRole("button")).toHaveAccessibleName(
      // The matched key is the one the citation names: said once.
      "Open repo://conf/limits.toml, revision 1, key retention.days",
    );
    expect(within(first).getByRole("button")).toHaveAccessibleName(
      "Open repo://handbook/retention.pdf, Retention > Events, revision 3, page 7",
    );
    expect(within(first).getByRole("button")).toHaveAccessibleDescription(/Raw events are kept for 30 days/);
    await user.click(within(first).getByRole("button"));
    expect(onOpen).toHaveBeenCalledWith("repo://handbook/retention.pdf");
    // Gone (folded, or another place), the hits no longer show and the list below takes its
    // keys back.
    cleanup();
    expect(onHitsShown).toHaveBeenLastCalledWith(false);
  });

  it("says a full page is the best passages, not all of them", async () => {
    vi.mocked(searchKnowledge).mockResolvedValue(Array.from({ length: 10 }, (_, i) => ({ ...HIT, chunk_idx: i })));
    await openAndFind("events");
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("The best 10 passages"));
  });

  it("shows more of a full answer, ten at a time, for the query that was searched", async () => {
    const page = (n: number) => Array.from({ length: n }, (_, i) => ({ ...HIT, chunk_idx: i }));
    vi.mocked(searchKnowledge).mockResolvedValueOnce(page(10)).mockResolvedValueOnce(page(14));
    const { user } = await openAndFind("events");
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("The best 10 passages"));
    // Text typed since does not change what "Show more" asks.
    await user.type(screen.getByLabelText("Passage to find"), " and more");
    await user.click(screen.getByRole("button", { name: "Show more" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("14 passages"));
    expect(vi.mocked(searchKnowledge).mock.calls[1].slice(1, 3)).toEqual(["events", 20]);
    // Fewer than were asked for: that is all of them. The button that held the focus is
    // gone, and the focus is on the first hit that read added.
    expect(screen.queryByRole("button", { name: "Show more" })).toBeNull();
    const rows = within(screen.getByRole("list", { name: "Passages found" })).getAllByRole("button");
    await waitFor(() => expect(document.activeElement).toBe(rows[10]));
  });

  it("keeps the hits on screen when Show more fails", async () => {
    const page = Array.from({ length: 10 }, (_, i) => ({ ...HIT, chunk_idx: i }));
    vi.mocked(searchKnowledge).mockResolvedValueOnce(page).mockRejectedValueOnce(new TypeError("Failed to fetch"));
    const { user } = await openAndFind("events");
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("The best 10 passages"));
    await user.click(screen.getByRole("button", { name: "Show more" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/Lost connection to Loom/));
    expect(within(screen.getByRole("list", { name: "Passages found" })).getAllByRole("listitem")).toHaveLength(10);
    expect(screen.getByRole("button", { name: "Show more" })).toBeInTheDocument();
  });

  it("shows a hit from a server older than revisions and locators, uncited", async () => {
    const { revision, format, block_type, locator, keys_matched, ...old } = HIT;
    vi.mocked(searchKnowledge).mockResolvedValue([old]);
    await openAndFind("events");
    const [row] = within(await screen.findByRole("list", { name: "Passages found" })).getAllByRole("listitem");
    expect(row).toHaveTextContent("retention.pdf");
    expect(row).toHaveTextContent("Raw events are kept for 30 days");
    expect(row).not.toHaveTextContent("revision");
  });

  it("says when no passage answers, and when the search failed", async () => {
    vi.mocked(searchKnowledge).mockResolvedValueOnce([]);
    const { user } = await openAndFind("nothing like this");
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("No passage answers that."));
    vi.mocked(searchKnowledge).mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await user.click(screen.getByRole("button", { name: "Find" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/Lost connection to Loom/));
  });

  it("marks the hits on screen busy while a new search runs", async () => {
    vi.mocked(searchKnowledge).mockResolvedValueOnce([HIT]);
    const { user } = await openAndFind("events");
    const list = await screen.findByRole("list", { name: "Passages found" });
    expect(list).not.toHaveAttribute("aria-busy");
    vi.mocked(searchKnowledge).mockReturnValueOnce(new Promise(() => {}));
    await user.click(screen.getByRole("button", { name: "Find" }));
    expect(screen.getByRole("list", { name: "Passages found" })).toHaveAttribute("aria-busy", "true");
  });

  it("drops a search still running when the panel goes", async () => {
    let signal: AbortSignal | undefined;
    vi.mocked(searchKnowledge).mockImplementation((_id, _q, _k, opts) => {
      signal = opts?.signal;
      return new Promise(() => {});
    });
    await openAndFind("events");
    expect(signal?.aborted).toBe(false);
    cleanup();
    expect(signal?.aborted).toBe(true);
  });

  it("cuts a long excerpt between characters, not inside one", async () => {
    vi.mocked(searchKnowledge).mockResolvedValue([{ ...HIT, content: `${"a".repeat(278)}😀 and more after it` }]);
    await openAndFind("events");
    const [row] = within(await screen.findByRole("list", { name: "Passages found" })).getAllByRole("listitem");
    expect(row.textContent).toContain(`${"a".repeat(278)}😀…`);
  });
});
