import { cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getBrief } from "../api/briefs";
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
vi.mock("../api/briefs", () => ({ getBrief: vi.fn() }));
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
      sessionHandoff: null,
    });
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

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

  it("opens the pinned handoff in Conversations after a reload", async () => {
    const head = "c".repeat(40);
    const brief = "handoff/case";
    const key = "~alice~handoff/case";
    const snapshot = `9007199254740997.${"e".repeat(64)}`;
    vi.mocked(getBrief).mockResolvedValueOnce({
      found: true,
      brief_key: brief,
      revision: 1,
      snapshot_id: snapshot,
      snapshot_key: key,
      rendered: "Last recorded verification: pytest passed. Next: ship the fix.",
      project: "proj",
      branch: "fix/case",
      head_sha: head,
      author: "model",
      trigger: "manual",
      source_sessions: [],
      updated_at: null,
      age_seconds: 0,
      revisions: 2,
      events_total: 0,
      source_chars: 0,
      brief_chars: 65,
      deleted: false,
    });
    history.replaceState(
      null,
      "",
      `/conversations?namespace=ns&org=dev&brief=${encodeURIComponent(key)}&revision=1&snapshot=${snapshot}&commit=${head}`,
    );
    useStore.getState().bootView("sessions");
    render(<Conversations />);
    expect(await screen.findByText(/Next: ship the fix/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy restore prompt" })).toBeInTheDocument();
    expect(getBrief).toHaveBeenCalledWith({ org: "dev", namespace: "ns" }, key, {
      revision: 1,
      snapshotId: snapshot,
    });
    useStore.getState().setIdentity({ org: "dev", namespace: "other", agent: "loom-ui" });
    await waitFor(() => expect(screen.queryByText(/Next: ship the fix/)).toBeNull());
    expect(new URLSearchParams(location.search).has("brief")).toBe(false);
  });

  it("does not load a handoff in a different organization", async () => {
    history.replaceState(
      null,
      "",
      `/conversations?namespace=ns&org=elsewhere&brief=handoff%2Fcase&revision=1&snapshot=6.${"e".repeat(64)}&commit=${"c".repeat(40)}`,
    );
    useStore.getState().bootView("sessions");
    render(<Conversations />);
    expect(await screen.findByText(/Open this session handoff in its organization/)).toBeInTheDocument();
    expect(getBrief).not.toHaveBeenCalled();
  });
});
