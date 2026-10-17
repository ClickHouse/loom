import { act, cleanup, render, screen, waitFor, within } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AllowlistRow, ConflictRow, LearningEntry } from "../../api";
import { useStore } from "../../lib/state";
import { ReviewQueue } from "./ReviewQueue";
import { ApiError } from "../../lib/errors";

// The review queue is keyed to the namespace in the header: a reload after a decision
// must not paint the previous namespace's rows, and switching namespace must not leave
// the viewer on a sub-tab the new namespace hides.

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    listConflicts: vi.fn(),
    countConflicts: vi.fn(async () => 0),
    resolveConflict: vi.fn(async () => ({ resolved: true, affected_memory_ids: [] })),
    listAllowlist: vi.fn(),
    removeAllowlist: vi.fn(async () => ({ removed: true })),
    listLearnings: vi.fn(),
    reviewLearning: vi.fn(),
  };
});
vi.mock("../../components/ConfirmDialog", () => ({ confirmAction: async () => true }));
import { countConflicts, listAllowlist, listConflicts, listLearnings, resolveConflict } from "../../api";

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
    expect(await screen.findByRole("button", { name: "Remove exception k/a and k/b" }, WAIT)).toBeTruthy();
    // The pair reads "k/a and k/b", not a spoken arrow glyph.
    const table = screen.getByRole("table", { name: "Allowed exceptions" });
    expect(within(table).getAllByRole("row")[1]).toHaveTextContent("k/a and k/b");
    expect(table).not.toHaveTextContent("↔");
  });
  it("keeps the exceptions on screen, marked busy, while the reload after a removal reads", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    vi.mocked(listAllowlist).mockResolvedValueOnce({ rows: [exception("k/a", "k/b"), exception("k/c", "k/d")] });
    seed("ns-a");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Exceptions/ }, WAIT));
    const remove = await screen.findByRole("button", { name: "Remove exception k/a and k/b" }, WAIT);
    let answer: (v: { rows: AllowlistRow[] }) => void = () => undefined;
    vi.mocked(listAllowlist).mockReturnValueOnce(new Promise((r) => (answer = r)));
    await userEvent.click(remove);
    await waitFor(() => expect(vi.mocked(listAllowlist)).toHaveBeenCalledTimes(2), WAIT);
    expect(screen.getByText("k/c")).toBeInTheDocument();
    expect(screen.getByTestId("allowlist")).toHaveAttribute("aria-busy", "true");
    await act(async () => answer({ rows: [exception("k/c", "k/d")] }));
    expect(screen.getByTestId("allowlist")).not.toHaveAttribute("aria-busy");
    expect(screen.queryByText("k/a")).toBeNull();
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

  it("lists the queue in a table, says how many of the total it holds, and loads more", async () => {
    vi.mocked(countConflicts).mockResolvedValue(3);
    vi.mocked(listConflicts).mockImplementation(async (_id, opts) =>
      opts?.before_version
        ? { rows: [conflict("k/three")], next_before_version: null }
        : { rows: [conflict("k/one"), conflict("k/two")], next_before_version: 7 },
    );
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of k/one", {}, WAIT);
    const table = screen.getByRole("table", { name: "Conflicts" });
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(await screen.findByText("2 of 3", {}, WAIT)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(within(table).getAllByRole("row")).toHaveLength(4), WAIT);
    expect(vi.mocked(listConflicts).mock.calls.at(-1)?.[1]?.before_version).toBe(7);
    expect(screen.getByText("3 of 3")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();

    // A row of the queue opens that conflict for a decision.
    await userEvent.click(within(table).getByRole("button", { name: "k/three" }));
    expect(screen.getByText("value of k/three")).toBeInTheDocument();
  });

  it("takes a decided conflict out of the queue", async () => {
    vi.mocked(countConflicts).mockResolvedValue(2);
    vi.mocked(listConflicts).mockResolvedValue({
      rows: [conflict("k/one"), conflict("k/two")],
      next_before_version: null,
    });
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of k/one", {}, WAIT);
    await userEvent.click(screen.getByRole("button", { name: /Keep A/ }));
    const table = screen.getByRole("table", { name: "Conflicts" });
    await waitFor(() => expect(within(table).queryByRole("button", { name: "k/one" })).toBeNull(), WAIT);
    expect(within(table).getByRole("button", { name: "k/two" })).toBeInTheDocument();
    expect(screen.getByText("1 of 1")).toBeInTheDocument();
  });

  it("keeps a conflict decided while Load more reads out of the queue", async () => {
    vi.mocked(countConflicts).mockResolvedValue(3);
    let answer: (v: { rows: ConflictRow[]; next_before_version: null }) => void = () => {};
    vi.mocked(listConflicts).mockImplementation(async (_id, opts) =>
      opts?.before_version
        ? new Promise((r) => (answer = r))
        : { rows: [conflict("k/one"), conflict("k/two")], next_before_version: 7 },
    );
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of k/one", {}, WAIT);
    const table = screen.getByRole("table", { name: "Conflicts" });
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    await userEvent.click(screen.getByRole("button", { name: /Keep A/ }));
    await waitFor(() => expect(within(table).queryByRole("button", { name: "k/one" })).toBeNull(), WAIT);
    await act(async () => answer({ rows: [conflict("k/three")], next_before_version: null }));
    await waitFor(() => expect(within(table).getByRole("button", { name: "k/three" })).toBeInTheDocument(), WAIT);
    expect(within(table).queryByRole("button", { name: "k/one" })).toBeNull();
    expect(within(table).getByRole("button", { name: "k/two" })).toBeInTheDocument();
  });

  it("counts the queue in its toolbar and shows each conflict's time as a list time", async () => {
    vi.mocked(countConflicts).mockResolvedValue(4);
    vi.mocked(listConflicts).mockResolvedValue({
      rows: [conflict("k/one"), conflict("k/two")],
      next_before_version: 7,
    });
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of k/one", {}, WAIT);
    const bar = document.querySelector<HTMLElement>('search[aria-label="Queue"]');
    await waitFor(() => expect(bar).toHaveTextContent("2 of 4"), WAIT);
    const table = screen.getByRole("table", { name: "Conflicts" });
    expect(within(table).getAllByText("Sep 20")[0].tagName).toBe("TIME");
  });

  it("says a failed read failed, with Retry, never 'No conflicts'", async () => {
    vi.mocked(listConflicts).mockRejectedValueOnce(new ApiError({ status: 503, message: "down", requestId: "rq-5" }));
    seed("ns-a");
    render(<ReviewQueue />);
    expect(await screen.findByText("Couldn't load conflicts", {}, WAIT)).toBeInTheDocument();
    expect(screen.getByText(/rq-5/)).toBeInTheDocument();
    expect(screen.queryByText("No conflicts")).toBeNull();
    vi.mocked(listConflicts).mockResolvedValue({ rows: [conflict("k/one")], next_before_version: null });
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("value of k/one", {}, WAIT)).toBeInTheDocument();
  });

  it("says a failed exceptions read failed, with Retry", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    vi.mocked(listAllowlist)
      .mockResolvedValueOnce({ rows: [exception("k/a", "k/b")] })
      .mockRejectedValueOnce(new ApiError({ status: 500, message: "boom" }));
    seed("ns-a");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Exceptions/ }, WAIT));
    await userEvent.click(await screen.findByRole("button", { name: /Remove exception/ }, WAIT));
    expect(await screen.findByText("Couldn't load allowed exceptions", {}, WAIT)).toBeInTheDocument();
    expect(screen.queryByText("No allowed exceptions")).toBeNull();
  });
});
