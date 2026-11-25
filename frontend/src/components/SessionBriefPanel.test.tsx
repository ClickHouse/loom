import { cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SessionBriefPanel, restorePhrase } from "./SessionBriefPanel";

// A conversation that a coding session wrote shows that session's brief,
// collapsed, with the words that restore it; a conversation with no brief
// shows nothing extra.

const getBrief = vi.fn();
vi.mock("../api/briefs", () => ({ getBrief: (...a: unknown[]) => getBrief(...a) }));

const found = (over: Record<string, unknown> = {}) => ({
  found: true,
  brief_key: "auto/loom/feat-x",
  revision: 2,
  rendered: "Goal:\nship it",
  project: "loom",
  branch: "feat/x",
  head_sha: "abc",
  author: "llm",
  trigger: "session_end",
  source_sessions: ["s-1"],
  updated_at: "2026-01-01T00:00:00Z",
  age_seconds: 60,
  revisions: 2,
  events_total: 10,
  source_chars: 100,
  brief_chars: 40,
  deleted: false,
  ...over,
});

describe("SessionBriefPanel", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(cleanup);

  it("says what to tell Claude Code: one restore, or the full transcript", () => {
    expect(restorePhrase("k")).toBe("Restore the session brief k.");
    expect(restorePhrase("k", "transcript")).toMatch(/full transcript\.$/);
  });

  it("renders nothing for a session that left no brief", async () => {
    getBrief.mockResolvedValue({ found: false });
    const { container } = render(<SessionBriefPanel org="o" namespace="ns" sessionId="s-9" />);
    await waitFor(() =>
      expect(getBrief).toHaveBeenCalledWith({ org: "o", namespace: "ns" }, "", { fromSession: "s-9", full: true }),
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("looks the brief up by session, stays collapsed, and opens to read and restore it", async () => {
    getBrief.mockResolvedValue(found());
    render(<SessionBriefPanel org="o" namespace="ns" sessionId="s-1" />);
    const toggle = await screen.findByRole("button", { name: /Session brief/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText(/ship it/)).toBeNull();
    fireEvent.click(toggle);
    expect(await screen.findByText(/ship it/)).toBeInTheDocument();
    expect(screen.getByText("Restore the session brief auto/loom/feat-x.")).toBeInTheDocument();
    // One level: the brief is read with the client's summary; no depth toggle.
    expect(getBrief).toHaveBeenCalledWith({ org: "o", namespace: "ns" }, "", { fromSession: "s-1", full: true });
    expect(screen.queryByRole("button", { name: "Full" })).toBeNull();
    // An earlier revision is fetched by the brief's key, with the summary too.
    fireEvent.click(screen.getByRole("button", { name: /Revision/ }));
    fireEvent.click(await screen.findByText("rev 1"));
    await waitFor(() =>
      expect(getBrief).toHaveBeenLastCalledWith({ org: "o", namespace: "ns" }, "auto/loom/feat-x", {
        revision: 1,
        full: true,
      }),
    );
  });

  it("copies the selected snapshot, including the revision and exact commit", async () => {
    const commit = "b".repeat(40);
    const snapshotKey = "~alice~auto/loom/feat-x";
    const snapshot = `9007199254740997.${"e".repeat(64)}`;
    getBrief
      .mockResolvedValueOnce(
        found({
          snapshot_id: `9007199254740999.${"f".repeat(64)}`,
          snapshot_key: snapshotKey,
          head_sha: "a".repeat(40),
        }),
      )
      .mockResolvedValueOnce(
        found({
          revision: 1,
          snapshot_id: snapshot,
          snapshot_key: snapshotKey,
          head_sha: commit,
        }),
      );
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", Object.create(navigator, { clipboard: { value: { writeText } } }));
    try {
      render(<SessionBriefPanel org="o" namespace="ns" briefKey="auto/loom/feat-x" />);
      fireEvent.click(await screen.findByRole("button", { name: /Session brief/ }));
      fireEvent.click(screen.getByRole("button", { name: /Revision/ }));
      fireEvent.click(await screen.findByText("rev 1"));
      fireEvent.click(await screen.findByRole("button", { name: "Copy handoff link" }));
      expect(writeText).toHaveBeenCalledOnce();
      const url = new URL(String(writeText.mock.calls[0][0]));
      expect(url.pathname).toBe("/conversations");
      expect(Object.fromEntries(url.searchParams)).toEqual({
        org: "o",
        namespace: "ns",
        brief: snapshotKey,
        revision: "1",
        snapshot,
        commit,
      });
      expect(screen.getByText("Restore revision 1 of the session brief auto/loom/feat-x.")).toBeInTheDocument();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
