import { act, cleanup, render, screen, waitFor } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AllowlistRow, ConflictRow, LearningEntry } from "../../api";
import { useStore } from "../../lib/state";
import { ReviewQueue } from "./ReviewQueue";

// The review queue is keyed to the namespace in the header: a reload after a decision
// must not paint the previous namespace's rows, and switching namespace must not leave
// the viewer on a sub-tab the new namespace hides.

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    listConflicts: vi.fn(),
    resolveConflict: vi.fn(async () => ({ resolved: true, affected_memory_ids: [] })),
    listAllowlist: vi.fn(),
    removeAllowlist: vi.fn(async () => ({ removed: true })),
    listLearnings: vi.fn(),
    reviewLearning: vi.fn(),
  };
});
import { listAllowlist, listConflicts, listLearnings, resolveConflict } from "../../api";

const ORG = "dev";
const WAIT = { timeout: 5000 };

const conflict = (key: string): ConflictRow => ({
  memory_id: `id-${key}`,
  memory_key: key,
  memory_type: "semantic",
  value: `value of ${key}`,
  version: 1,
  pinned: false,
  created_at: "2026-09-20T00:00:00Z",
  conflict_with: ["peer-000000000000"],
  content_sha256: "",
});

const learning = (key: string): LearningEntry => ({
  candidate_id: `c-${key}`,
  memory_key: key,
  memory_type: "semantic",
  proposed_value: "v",
  supporter_count: 2,
  confidence_avg: 0.9,
  supporters: [],
  rationale: "",
  status: "pending",
  proposed_at: "2026-09-20T00:00:00Z",
  decided_at: null,
  decided_by: "",
  decision_reason: "",
});

const exception = (a: string, b: string): AllowlistRow => ({
  memory_key_a: a,
  memory_key_b: b,
  allowed_at: "2026-09-20T00:00:00Z",
  allowed_by: "op@x",
  reason: "",
});

function seed(namespace: string) {
  act(() => {
    useStore.setState({ identity: { org: ORG, namespace, agent: "loom-ui" } });
  });
}

describe("ReviewQueue", () => {
  beforeEach(() => {
    vi.mocked(listConflicts).mockReset();
    vi.mocked(listAllowlist).mockReset();
    vi.mocked(listLearnings).mockReset();
    vi.mocked(listAllowlist).mockResolvedValue({ rows: [] });
    vi.mocked(listLearnings).mockResolvedValue({ rows: [] });
  });
  afterEach(() => {
    cleanup();
  });

  it("shows a high conflict as sensitive, not as an empty value", async () => {
    vi.mocked(listConflicts).mockResolvedValue({
      rows: [{ ...conflict("vault"), value: null, sensitivity: "high" }],
      next_before_version: null,
    });
    seed("ns-a");
    render(<ReviewQueue />);
    expect(await screen.findByText("Sensitive: open it to read", {}, WAIT)).toBeTruthy();
  });

  it("drops a post-decision reload that lands after a namespace switch", async () => {
    let finishStale: (v: { rows: ConflictRow[]; next_before_version: null }) => void = () => {};
    vi.mocked(listConflicts).mockImplementation(async (id) => {
      if (id.namespace === "ns-b") return { rows: [conflict("b/key")], next_before_version: null };
      // ns-a: the first read shows one conflict; the reload after deciding it hangs.
      if (vi.mocked(listConflicts).mock.calls.filter(([c]) => c.namespace === "ns-a").length === 1) {
        return { rows: [conflict("a/key")], next_before_version: null };
      }
      return new Promise((resolve) => {
        finishStale = resolve;
      });
    });
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of a/key", {}, WAIT);

    await userEvent.click(screen.getByRole("button", { name: /Keep A/ }));
    await waitFor(() => expect(resolveConflict).toHaveBeenCalled(), WAIT);
    await waitFor(
      () => expect(vi.mocked(listConflicts).mock.calls.filter(([c]) => c.namespace === "ns-a").length).toBe(2),
      WAIT,
    );

    seed("ns-b");
    await screen.findByText("value of b/key", {}, WAIT);
    await act(async () => {
      finishStale({ rows: [conflict("a/stale")], next_before_version: null });
    });
    expect(screen.queryByText("value of a/stale")).toBeNull();
    expect(screen.getByText("value of b/key")).toBeTruthy();
  });

  it("falls back to Conflicts when the new namespace hides the open sub-tab", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    vi.mocked(listLearnings).mockImplementation(async (id) => ({
      rows: id.namespace === "ns-a" ? [learning("a/learned")] : [],
    }));
    seed("ns-a");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Learnings/ }, WAIT));
    await screen.findByText("a/learned", {}, WAIT);

    seed("ns-b");
    await screen.findByText("No conflicts", {}, WAIT);
    expect(screen.queryByRole("tab")).toBeNull();
    // A lone Conflicts panel is the page, not a tab panel with no tabs.
    expect(screen.queryByRole("tabpanel")).toBeNull();
  });

  it("shows conflicts before the probes answer, and reads them once when a late probe reveals the Learnings tab", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [conflict("a/key")], next_before_version: null });
    let reveal: (v: { rows: ReturnType<typeof learning>[] }) => void = () => {};
    vi.mocked(listLearnings).mockReturnValue(new Promise((r) => (reveal = r)));
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of a/key", {}, WAIT);
    expect(screen.queryByRole("tabpanel")).toBeNull();
    await act(async () => reveal({ rows: [learning("a/learned")] }));
    await screen.findByRole("tab", { name: /Learnings/ }, WAIT);
    expect(screen.getByRole("tabpanel", { name: /Conflicts/ })).toHaveTextContent("value of a/key");
    expect(vi.mocked(listConflicts)).toHaveBeenCalledTimes(1);
  });

  it("names the icon-only remove button on an exception", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    vi.mocked(listAllowlist).mockResolvedValue({ rows: [exception("k/a", "k/b")] });
    seed("ns-a");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Exceptions/ }, WAIT));
    expect(await screen.findByRole("button", { name: "Remove exception k/a ↔ k/b" }, WAIT)).toBeTruthy();
  });
  it("is operable from the keyboard: tabs, status filter and the decision buttons", async () => {
    const user = userEvent.setup();
    vi.mocked(listConflicts).mockResolvedValue({
      rows: [conflict("k/one"), conflict("k/two")],
      next_before_version: null,
    });
    vi.mocked(listLearnings).mockImplementation(async (_id, status) => ({
      rows: status === "pending" ? [learning("k/pending")] : [learning("k/promoted")],
    }));
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of k/one", {}, WAIT);

    // The conflict's reason field and buttons are reached with Tab and pressed with Enter.
    const reason = screen.getByRole("textbox", { name: "Reason for the decision" });
    reason.focus();
    await user.keyboard("dup");
    // The footer reads secondary first, primary last: Skip, Allow both, Drop A, Keep A.
    for (const name of [/Skip/, /Allow both/, /Drop A/, /Keep A/]) {
      await user.tab();
      expect(document.activeElement).toBe(screen.getByRole("button", { name }));
    }
    await user.keyboard("{Enter}");
    await waitFor(
      () => expect(resolveConflict).toHaveBeenCalledWith(expect.anything(), "id-k/one", "revise", { reason: "dup" }),
      WAIT,
    );
    await screen.findByText("value of k/two", {}, WAIT);

    // Arrow keys move between the sub-tabs.
    const conflictsTab = screen.getByRole("tab", { name: /Conflicts/ });
    conflictsTab.focus();
    await user.keyboard("{ArrowRight}");
    const learningsTab = screen.getByRole("tab", { name: /Learnings/ });
    expect(document.activeElement).toBe(learningsTab);
    await waitFor(() => expect(learningsTab.getAttribute("aria-selected")).toBe("true"), WAIT);
    await screen.findByText("k/pending", {}, WAIT);

    // The status filter is a group of toggle buttons pressed with Space.
    const promoted = screen.getByRole("button", { name: "Promoted" });
    promoted.focus();
    await user.keyboard(" ");
    await screen.findByText("k/promoted", {}, WAIT);
    expect(promoted.getAttribute("aria-pressed")).toBe("true");
  });
});
