import { cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { Conversations } from "./Conversations";

// The open thread is the page's URL: opening one writes `?thread=` into the entry (no new
// entry), closing drops it, and a `?thread=` link opens it on arrival.

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listConversationThreads: vi.fn(async () => ({
      threads: [
        {
          thread_id: "t-7",
          agent: "claude-code",
          msg_count: 1,
          first_seen: "2026-09-18T09:00:00Z",
          last_seen: "2026-09-18T09:00:00Z",
          last_role: "user",
          last_content_excerpt: "hello",
          title: "Deploy chat",
        },
      ],
    })),
    getConversationThread: vi.fn(async () => ({
      found: true,
      thread_id: "t-7",
      title: "Deploy chat",
      agent: "claude-code",
      msg_count: 1,
      created_at: null,
      updated_at: null,
      messages: [{ msg_id: "m1", agent: "claude-code", role: "user", content: "ship it", ts: "2026-09-18T09:00:00Z" }],
    })),
    listMemories: vi.fn(async () => ({ entries: [], truncated: false, total: 0, today: 0 })),
    getNamespace: vi.fn(async () => ({ conversation_logging_enabled: true })),
  };
});
vi.mock("../components/SessionBriefsSection", () => ({ SessionBriefsSection: () => null }));
vi.mock("../components/SessionBriefPanel", () => ({ SessionBriefPanel: () => null }));

describe("Conversations keeps the open thread in its URL", () => {
  beforeEach(() => {
    useStore.setState({
      authed: true,
      whoami: { authenticated: false },
      identity: { org: "dev", namespace: "ns", agent: "loom-ui" },
      view: "sessions",
      pendingConversationThread: null,
    });
  });
  afterEach(() => cleanup());

  it("opening a thread writes ?thread=, closing drops it, and neither adds an entry", async () => {
    history.replaceState(null, "", "/conversations");
    const before = history.length;
    render(<Conversations />);
    await userEvent.click(await screen.findByRole("button", { name: /^Deploy chat/ }));
    const dialog = await screen.findByRole("dialog", { name: "Deploy chat" });
    expect(location.search).toBe("?thread=t-7");
    await userEvent.click(within(dialog).getByRole("button", { name: /close/i }));
    await waitFor(() => expect(location.search).toBe(""));
    expect(history.length).toBe(before);
  });

  it("a ?thread= link opens the thread on arrival", async () => {
    history.replaceState(null, "", "/conversations?thread=t-7");
    useStore.getState().bootView("sessions");
    render(<Conversations />);
    const dialog = await screen.findByRole("dialog", { name: "Deploy chat" });
    expect(await within(dialog).findByText("ship it")).toBeInTheDocument();
  });
});
