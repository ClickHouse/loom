import { act, cleanup, fireEvent, render, screen } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ConversationThreadSummary } from "../api";
import { useStore } from "../lib/state";
import { Conversations } from "./Conversations";

// The search is sent on Enter and the box keeps what is typed until then: a keystroke
// renders the box, not the threads. Each thread row renders its last-active time, so
// ListTime counts the rows rendered.

const renders = { count: 0 };
vi.mock("../components/ui/ListTime", () => ({
  ListTime: ({ iso }: { iso: string }) => {
    renders.count += 1;
    return <time>{iso}</time>;
  },
}));

const thread = (i: number): ConversationThreadSummary => ({
  thread_id: `t-${i}`,
  agent: "oncall",
  msg_count: 4,
  first_seen: "2026-09-19T08:00:00Z",
  last_seen: "2026-09-19T09:00:00Z",
  last_role: "assistant",
  last_content_excerpt: "Rolled back payments-api.",
  title: `Thread ${i}`,
});
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  listConversationThreads: vi.fn(async () => ({ threads: [thread(1), thread(2), thread(3)] })),
  searchConversations: vi.fn(async () => ({ hits: [] })),
  getNamespace: vi.fn(async () => ({ conversation_logging_enabled: true })),
}));
vi.mock("../components/SessionBriefsSection", () => ({ SessionBriefsSection: () => null }));

describe("Conversations row renders", () => {
  beforeEach(() => {
    renders.count = 0;
    useStore.setState({
      authed: true,
      whoami: { authenticated: false },
      identity: { org: "dev", namespace: "ns-a", agent: "loom-ui" },
      view: "sessions",
    });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ identity: null, whoami: null });
  });

  it("a keystroke in the search renders no thread row again", async () => {
    render(<Conversations />);
    await screen.findByText("Thread 3");
    renders.count = 0;
    act(() => {
      fireEvent.change(screen.getByLabelText("Search conversation transcripts"), { target: { value: "refund" } });
    });
    expect(renders.count).toBe(0);
  });
});
