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

async function openAndFind(query: string, onOpen = vi.fn()) {
  render(<LibraryPassages identity={identity} onOpen={onOpen} />);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: /Find a passage/ }));
  await user.type(screen.getByLabelText("Passage to find"), query);
  await user.click(screen.getByRole("button", { name: "Find" }));
  return { user, onOpen };
}

describe("LibraryPassages", () => {
  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
  });

  it("is folded until opened", async () => {
    render(<LibraryPassages identity={identity} onOpen={vi.fn()} />);
    const toggle = screen.getByRole("button", { name: /Find a passage/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByLabelText("Passage to find")).toBeNull();
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
    const { user, onOpen } = await openAndFind("how long are events kept");
    const list = await screen.findByRole("list", { name: "Passages found" });
    expect(vi.mocked(searchKnowledge).mock.calls[0].slice(0, 2)).toEqual([identity, "how long are events kept"]);
    const [first, second] = within(list).getAllByRole("listitem");
    expect(first).toHaveTextContent("retention.pdf");
    expect(first).toHaveTextContent("Retention > Events");
    expect(first).toHaveTextContent("revision 3, page 7");
    expect(first).toHaveTextContent("Raw events are kept for 30 days");
    expect(second).toHaveTextContent("revision 1, key retention.days");
    expect(await axeViolations(document.body)).toEqual([]);
    await user.click(within(first).getByRole("button"));
    expect(onOpen).toHaveBeenCalledWith("repo://handbook/retention.pdf");
  });

  it("says when no passage answers, and when the search failed", async () => {
    vi.mocked(searchKnowledge).mockResolvedValueOnce([]);
    const { user } = await openAndFind("nothing like this");
    expect(await screen.findByText("No passage answers that.")).toBeInTheDocument();
    vi.mocked(searchKnowledge).mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await user.click(screen.getByRole("button", { name: "Find" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/Lost connection to Loom/));
  });
});
