import { cleanup, render, screen } from "../test/rtl";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ConversationThreadDetail } from "../api";
import { ThreadReplayDialog } from "./ThreadReplayDialog";

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
});
