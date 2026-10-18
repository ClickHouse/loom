import { cleanup, render, screen, waitFor, within } from "../test/rtl";
import { afterEach, describe, expect, it, vi } from "vitest";
import { listConversationMessages, listMemories, type ConversationThreadDetail } from "../api";
import { listTime } from "../lib/format";
import { ThreadReplayDialog, threadTitle } from "./ThreadReplayDialog";

// The replay's turns are told apart by a neutral role badge, not by coloured rails; the
// turn a search hit names takes the active stroke, and the first read is a labelled loader.

const getConversationThread = vi.fn<typeof import("../api").getConversationThread>();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getConversationThread: (...args: Parameters<typeof actual.getConversationThread>) => getConversationThread(...args),
    listMemories: vi.fn(async () => ({ entries: [], truncated: false, total: 0, today: 0 })),
    listConversationMessages: vi.fn(async () => ({ messages: [] })),
  };
});
vi.mock("./SessionBriefPanel", () => ({ SessionBriefPanel: () => null }));

const identity = { org: "dev", namespace: "ns", agent: "loom-ui" };
const now = new Date().toISOString();
const detail: ConversationThreadDetail = {
  found: true,
  thread_id: "t-1",
  title: "Fix",
  agent: "planner",
  msg_count: 3,
  messages: [
    { msg_id: "1", agent: "planner", role: "user", content: "plan the fix", ts: now },
    { msg_id: "2", agent: "planner", role: "tool", content: "ran the tests", ts: now },
    { msg_id: "3", agent: "planner", role: "assistant", content: "ship it", ts: now },
  ],
} as ConversationThreadDetail;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ThreadReplayDialog", () => {
  it("shows Click UI's labelled loader while the thread loads", async () => {
    getConversationThread.mockImplementation(() => new Promise(() => {}));
    render(
      <ThreadReplayDialog identity={identity} thread={{ thread_id: "t-1", namespace: "ns" }} onClose={() => {}} />,
    );
    expect(await screen.findByText("Loading thread", {}, { timeout: 2000 })).toBeInTheDocument();
    expect(screen.queryByText("Loading…")).toBeNull();
  });

  it("counts every memory the thread produced, and says when it shows the newest", async () => {
    getConversationThread.mockResolvedValue(detail);
    const entry = (memory_key: string) =>
      ({ memory_key, memory_type: "semantic", confidence: 1, version: 1, created_at: now, tags: [] }) as never;
    vi.mocked(listMemories).mockResolvedValueOnce({
      entries: Array.from({ length: 20 }, (_, i) => entry(`m${i}`)),
      truncated: true,
      total: 37,
      today: 0,
    });
    render(
      <ThreadReplayDialog identity={identity} thread={{ thread_id: "t-1", namespace: "ns" }} onClose={() => {}} />,
    );
    expect(await screen.findByText("Showing the newest 20.")).toBeInTheDocument();
    expect(screen.getByText("37")).toBeInTheDocument();
  });

  it("moves the focus to the transcript when the last page comes back empty", async () => {
    getConversationThread.mockResolvedValue({ ...detail, msg_count: 5, next_cursor: "c1" } as ConversationThreadDetail);
    vi.mocked(listConversationMessages).mockResolvedValueOnce({ rows: [], next_cursor: null } as never);
    render(
      <ThreadReplayDialog identity={identity} thread={{ thread_id: "t-1", namespace: "ns" }} onClose={() => {}} />,
    );
    const more = await screen.findByRole("button", { name: /^Load more/ }, { timeout: 2000 });
    more.focus();
    more.click();
    await waitFor(() => expect(screen.queryByRole("button", { name: /^Load more/ })).toBeNull());
    expect(screen.getByLabelText("Transcript")).toHaveFocus();
  });

  it("names each turn's role in a neutral badge on a neutral rail, and strokes the hit's turn", async () => {
    getConversationThread.mockResolvedValue(detail);
    render(
      <ThreadReplayDialog
        identity={identity}
        thread={{
          thread_id: "t-1",
          namespace: "ns",
          hit: { thread_id: "t-1", msg_id: "2", role: "tool", content_excerpt: "ran the tests", ts: now },
        }}
        onClose={() => {}}
      />,
    );
    await screen.findByText("ran the tests");
    for (const role of ["You", "Tool"]) {
      expect(screen.getByText(role).closest('[data-testid="ellipsed-badge-content"]')).not.toBeNull();
    }
    const turns = [...document.querySelectorAll<HTMLElement>("[data-msg-id]")];
    expect(turns).toHaveLength(3);
    for (const t of turns) expect(t.className).not.toMatch(/border-accent|border-warn|ring-accent/);
    const hit = document.querySelector<HTMLElement>("[data-highlighted]");
    expect(hit?.textContent).toContain("ran the tests");
    expect(hit?.className).toContain("--click-card-horizontal-default-color-stroke-active");
  });

  it("names the thread's agent in the neutral agent badge, and dates an old thread by its day", async () => {
    const old = new Date(Date.now() - 40 * 86_400_000).toISOString();
    getConversationThread.mockResolvedValue({
      ...detail,
      updated_at: old,
      messages: detail.messages.map((m) => ({ ...m, ts: old })),
    } as ConversationThreadDetail);
    render(
      <ThreadReplayDialog identity={identity} thread={{ thread_id: "t-1", namespace: "ns" }} onClose={() => {}} />,
    );
    const meta = (await screen.findByText("3 messages")).parentElement as HTMLElement;
    const agent = within(meta).getByText("planner");
    expect(agent.closest('.agent-badge [data-testid$="-badge-content"]')).not.toBeNull();
    expect(agent.closest(".font-mono")).toBeNull();
    expect(meta).toHaveTextContent(`updated ${listTime(old)}`);
    // The dialog's times are ListTime, the list's one time rule, the thread's update time
    // included.
    expect(meta.querySelector("time")).toHaveAttribute("dateTime", old);
    const dialog = screen.getByRole("dialog");
    expect(dialog.textContent).not.toMatch(/\d+\/\d+\/\d{4}/);
    const turns = [...dialog.querySelectorAll("[data-msg-id] time")];
    expect(turns.map((t) => t.firstChild?.textContent)).toEqual([listTime(old), listTime(old), listTime(old)]);
    expect(turns.every((t) => !t.hasAttribute("tabindex"))).toBe(true);
  });
});

// A thread is named by its title, else by the user's first message on one
// line; never by its id.
describe("threadTitle", () => {
  it("takes the title, else the user's message flattened, else nothing", () => {
    expect(threadTitle("  Release notes ", "ignored")).toBe("Release notes");
    expect(threadTitle("", "Can you draft\n  the notes?")).toBe("Can you draft the notes?");
    expect(threadTitle(undefined, "")).toBe("");
  });
});
