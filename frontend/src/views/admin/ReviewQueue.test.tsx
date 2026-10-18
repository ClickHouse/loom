import { act, cleanup, render, screen, waitFor, within } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";
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
const toast = vi.hoisted(() => vi.fn());
vi.mock("../../components/Toast", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../components/Toast")>()),
  toast,
}));
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
      // ns-a: the first read shows one conflict with more past it; the reload after
      // deciding it hangs.
      if (vi.mocked(listConflicts).mock.calls.filter(([c]) => c.namespace === "ns-a").length === 1) {
        return { rows: [conflict("a/key")], next_before_version: 7 };
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

  it("a decision answered after a namespace switch leaves the new namespace's queue alone", async () => {
    vi.mocked(listConflicts).mockImplementation(async (id) =>
      id.namespace === "ns-b"
        ? { rows: [conflict("b/key")], next_before_version: null }
        : { rows: [conflict("a/key"), conflict("a/next")], next_before_version: null },
    );
    // Each namespace has its own count: a stale decision would take one off B's.
    vi.mocked(countConflicts).mockImplementation(async (id: { namespace: string }) =>
      id.namespace === "ns-b" ? 5 : 2,
    );
    onTestFinished(() => {
      vi.mocked(countConflicts).mockImplementation(async () => 0);
    });
    let answer: (v: { resolved: boolean; affected_memory_ids: string[] }) => void = () => {};
    vi.mocked(resolveConflict).mockImplementationOnce(() => new Promise((r) => (answer = r)));
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of a/key", {}, WAIT);
    await userEvent.click(screen.getByRole("button", { name: /Keep A/ }));
    seed("ns-b");
    await screen.findByText("value of b/key", {}, WAIT);
    await screen.findByText(/Conflict 1 of 5/, {}, WAIT);
    // A's decision is still out, but B's queue is free to decide.
    expect(screen.getByRole("button", { name: /Keep A/ })).toBeEnabled();
    await act(async () => answer({ resolved: true, affected_memory_ids: [] }));
    expect(screen.getByText("value of b/key")).toBeTruthy();
    expect(screen.queryByText("value of a/next")).toBeNull();
    expect(screen.getByText(/Conflict 1 of 5/)).toBeInTheDocument();
  });

  it("an answer from the namespace left frees none of the new queue's controls while its own decision is out", async () => {
    vi.mocked(listConflicts).mockImplementation(async (id) =>
      id.namespace === "ns-b"
        ? { rows: [conflict("b/key")], next_before_version: null }
        : { rows: [conflict("a/key")], next_before_version: null },
    );
    let answerA: (v: { resolved: boolean; affected_memory_ids: string[] }) => void = () => {};
    const sent = vi.mocked(resolveConflict).mock.calls.length;
    vi.mocked(resolveConflict)
      .mockImplementationOnce(() => new Promise((r) => (answerA = r)))
      .mockImplementationOnce(() => new Promise(() => {}));
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of a/key", {}, WAIT);
    await userEvent.click(screen.getByRole("button", { name: /Keep A/ }));
    seed("ns-b");
    await screen.findByText("value of b/key", {}, WAIT);
    await userEvent.click(screen.getByRole("button", { name: /Drop A/ }));
    expect(screen.getByRole("button", { name: /Keep A/ })).toBeDisabled();
    await act(async () => answerA({ resolved: true, affected_memory_ids: [] }));
    // B's decision is still out: its buttons stay held, so it cannot be sent twice.
    expect(screen.getByRole("button", { name: /Keep A/ })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: /Keep A/ }));
    expect(vi.mocked(resolveConflict).mock.calls.length - sent).toBe(2);
  });

  it("an answer from an earlier visit to this namespace frees none of its controls after a switch away and back", async () => {
    vi.mocked(listConflicts).mockImplementation(async (id) =>
      id.namespace === "ns-b"
        ? { rows: [conflict("b/key")], next_before_version: null }
        : { rows: [conflict("a/x"), conflict("a/y")], next_before_version: null },
    );
    let answerFirst: (v: { resolved: boolean; affected_memory_ids: string[] }) => void = () => {};
    vi.mocked(resolveConflict)
      .mockImplementationOnce(() => new Promise((r) => (answerFirst = r)))
      .mockImplementationOnce(() => new Promise(() => {}));
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of a/x", {}, WAIT);
    await userEvent.click(screen.getByRole("button", { name: /Keep A/ }));
    seed("ns-b");
    await screen.findByText("value of b/key", {}, WAIT);
    seed("ns-a");
    await screen.findByText("value of a/x", {}, WAIT);
    await userEvent.click(screen.getByRole("button", { name: /Drop A/ }));
    expect(screen.getByRole("button", { name: /Keep A/ })).toBeDisabled();
    await act(async () => answerFirst({ resolved: true, affected_memory_ids: [] }));
    expect(screen.getByRole("button", { name: /Keep A/ })).toBeDisabled();
  });

  it("a conflict decided on an earlier visit leaves the queue read again on the return", async () => {
    vi.mocked(listConflicts).mockImplementation(async (id) =>
      id.namespace === "ns-b"
        ? { rows: [conflict("b/key")], next_before_version: null }
        : { rows: [conflict("a/x"), conflict("a/y")], next_before_version: null },
    );
    let answerFirst: (v: { resolved: boolean; affected_memory_ids: string[] }) => void = () => {};
    vi.mocked(resolveConflict).mockImplementationOnce(() => new Promise((r) => (answerFirst = r)));
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of a/x", {}, WAIT);
    await userEvent.click(screen.getByRole("button", { name: /Keep A/ }));
    seed("ns-b");
    await screen.findByText("value of b/key", {}, WAIT);
    seed("ns-a");
    // The queue read again still lists a/x: its decision has not answered yet.
    await screen.findByText("value of a/x", {}, WAIT);
    await act(async () => answerFirst({ resolved: true, affected_memory_ids: [] }));
    const table = screen.getByRole("table", { name: "Conflicts" });
    await waitFor(() => expect(within(table).queryByRole("button", { name: "a/x" })).toBeNull(), WAIT);
    expect(within(table).getByRole("button", { name: "a/y" })).toBeInTheDocument();
  });

  it("a decision from an earlier visit leaves the conflict open now where it is", async () => {
    vi.mocked(listConflicts).mockImplementation(async (id) =>
      id.namespace === "ns-b"
        ? { rows: [conflict("b/key")], next_before_version: null }
        : { rows: [conflict("a/x"), conflict("a/y"), conflict("a/z")], next_before_version: null },
    );
    let answerFirst: (v: { resolved: boolean; affected_memory_ids: string[] }) => void = () => {};
    vi.mocked(resolveConflict).mockImplementationOnce(() => new Promise((r) => (answerFirst = r)));
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of a/x", {}, WAIT);
    await userEvent.click(screen.getByRole("button", { name: /Keep A/ }));
    seed("ns-b");
    await screen.findByText("value of b/key", {}, WAIT);
    seed("ns-a");
    await screen.findByText("value of a/x", {}, WAIT);
    const table = screen.getByRole("table", { name: "Conflicts" });
    await userEvent.click(within(table).getByRole("button", { name: "a/y" }));
    expect(screen.getByText("value of a/y")).toBeInTheDocument();
    await act(async () => answerFirst({ resolved: true, affected_memory_ids: [] }));
    await waitFor(() => expect(within(table).queryByRole("button", { name: "a/x" })).toBeNull(), WAIT);
    expect(within(table).getByRole("button", { name: "a/y" })).toHaveAttribute("aria-current", "true");
    expect(screen.getByText("value of a/y")).toBeInTheDocument();
  });

  it("a conflict decided while the queue is read again stays out of the read's answer", async () => {
    let reread: (v: { rows: ConflictRow[]; next_before_version: null }) => void = () => {};
    let aReads = 0;
    vi.mocked(listConflicts).mockImplementation(async (id) => {
      if (id.namespace === "ns-b") return { rows: [conflict("b/key")], next_before_version: null };
      aReads += 1;
      if (aReads === 1) return { rows: [conflict("a/x"), conflict("a/y")], next_before_version: null };
      return new Promise((r) => (reread = r));
    });
    let answerFirst: (v: { resolved: boolean; affected_memory_ids: string[] }) => void = () => {};
    vi.mocked(resolveConflict).mockImplementationOnce(() => new Promise((r) => (answerFirst = r)));
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of a/x", {}, WAIT);
    await userEvent.click(screen.getByRole("button", { name: /Keep A/ }));
    seed("ns-b");
    await screen.findByText("value of b/key", {}, WAIT);
    seed("ns-a");
    await waitFor(() => expect(aReads).toBe(2), WAIT);
    await act(async () => answerFirst({ resolved: true, affected_memory_ids: [] }));
    // The server read its queue before the decision was visible: it still lists a/x.
    await act(async () => reread({ rows: [conflict("a/x"), conflict("a/y")], next_before_version: null }));
    const table = await screen.findByRole("table", { name: "Conflicts" });
    await waitFor(() => expect(within(table).getByRole("button", { name: "a/y" })).toBeInTheDocument(), WAIT);
    expect(within(table).queryByRole("button", { name: "a/x" })).toBeNull();
  });

  it("offers a decided conflict again once a later write flags it at a newer version", async () => {
    let reads = 0;
    vi.mocked(listConflicts).mockImplementation(async () => {
      reads += 1;
      return reads === 1
        ? { rows: [conflict("a/x"), conflict("a/y")], next_before_version: null }
        : { rows: [{ ...conflict("a/x"), version: 2 }, conflict("a/y")], next_before_version: null };
    });
    vi.mocked(resolveConflict).mockResolvedValueOnce({ resolved: true, affected_memory_ids: [] });
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of a/x", {}, WAIT);
    await userEvent.click(screen.getByRole("button", { name: /Keep A/ }));
    const table = screen.getByRole("table", { name: "Conflicts" });
    await waitFor(() => expect(within(table).queryByRole("button", { name: "a/x" })).toBeNull(), WAIT);
    // A switch away and back reads the queue again: a/x is back at v2, a new conflict.
    seed("ns-b");
    seed("ns-a");
    await waitFor(
      () =>
        expect(
          within(screen.getByRole("table", { name: "Conflicts" })).getByRole("button", { name: "a/x" }),
        ).toBeInTheDocument(),
      WAIT,
    );
  });

  it("a resolve that resolves nothing keeps the row and reads the queue again", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [conflict("a/x")], next_before_version: null });
    vi.mocked(resolveConflict).mockResolvedValueOnce({ resolved: false, affected_memory_ids: [] });
    toast.mockClear();
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of a/x", {}, WAIT);
    const before = vi.mocked(listConflicts).mock.calls.length;
    await userEvent.click(screen.getByRole("button", { name: /Keep A/ }));
    await waitFor(() => expect(vi.mocked(listConflicts).mock.calls.length).toBeGreaterThan(before), WAIT);
    expect(screen.getByText("value of a/x")).toBeInTheDocument();
    // Said as not resolved, never as "Resolved".
    expect(toast).toHaveBeenCalledWith(expect.stringMatching(/could not be resolved/), "error");
    expect(toast).not.toHaveBeenCalledWith("Resolved", "ok");
  });

  it("a resolve takes the peers it wrote out of the queue too, and off the count", async () => {
    vi.mocked(countConflicts).mockResolvedValue(3);
    onTestFinished(() => {
      vi.mocked(countConflicts).mockImplementation(async () => 0);
    });
    vi.mocked(listConflicts).mockResolvedValue({
      rows: [conflict("a/x"), conflict("a/peer"), conflict("a/z")],
      next_before_version: null,
    });
    vi.mocked(resolveConflict).mockResolvedValueOnce({ resolved: true, affected_memory_ids: ["id-a/peer"] });
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of a/x", {}, WAIT);
    await screen.findByText(/Conflict 1 of 3/, {}, WAIT);
    await userEvent.click(screen.getByRole("button", { name: /Allow both/ }));
    const table = screen.getByRole("table", { name: "Conflicts" });
    await waitFor(() => expect(within(table).queryByRole("button", { name: "a/peer" })).toBeNull(), WAIT);
    expect(within(table).queryByRole("button", { name: "a/x" })).toBeNull();
    expect(screen.getByText("value of a/z")).toBeInTheDocument();
    expect(screen.getByText(/Conflict 1 of 1/)).toBeInTheDocument();
  });

  it("an earlier visit's decision that empties a page with more past it reads the queue again", async () => {
    let aReads = 0;
    vi.mocked(listConflicts).mockImplementation(async (id) => {
      if (id.namespace === "ns-b") return { rows: [conflict("b/key")], next_before_version: null };
      aReads += 1;
      return aReads <= 2
        ? { rows: [conflict("a/x")], next_before_version: 7 }
        : { rows: [conflict("a/next")], next_before_version: null };
    });
    let answerFirst: (v: { resolved: boolean; affected_memory_ids: string[] }) => void = () => {};
    vi.mocked(resolveConflict).mockImplementationOnce(() => new Promise((r) => (answerFirst = r)));
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of a/x", {}, WAIT);
    await userEvent.click(screen.getByRole("button", { name: /Keep A/ }));
    seed("ns-b");
    await screen.findByText("value of b/key", {}, WAIT);
    seed("ns-a");
    await screen.findByText("value of a/x", {}, WAIT);
    await act(async () => answerFirst({ resolved: true, affected_memory_ids: [] }));
    expect(await screen.findByText("value of a/next", {}, WAIT)).toBeInTheDocument();
  });

  it("a decision made after an earlier visit's answer moved the queue opens the conflict that follows it", async () => {
    vi.mocked(countConflicts).mockResolvedValue(6);
    onTestFinished(() => {
      vi.mocked(countConflicts).mockImplementation(async () => 0);
    });
    vi.mocked(listConflicts).mockImplementation(async (id) =>
      id.namespace === "ns-b"
        ? { rows: [conflict("b/key")], next_before_version: null }
        : {
            rows: ["a/x", "a/x2", "a/y", "a/z", "a/w", "a/v"].map((k) => conflict(k)),
            next_before_version: null,
          },
    );
    let answerFirst: (v: { resolved: boolean; affected_memory_ids: string[] }) => void = () => {};
    let answerSecond: (v: { resolved: boolean; affected_memory_ids: string[] }) => void = () => {};
    vi.mocked(resolveConflict)
      .mockImplementationOnce(() => new Promise((r) => (answerFirst = r)))
      .mockImplementationOnce(() => new Promise((r) => (answerSecond = r)));
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of a/x", {}, WAIT);
    await userEvent.click(screen.getByRole("button", { name: /Keep A/ }));
    seed("ns-b");
    await screen.findByText("value of b/key", {}, WAIT);
    seed("ns-a");
    await screen.findByText("value of a/x", {}, WAIT);
    const table = screen.getByRole("table", { name: "Conflicts" });
    await userEvent.click(within(table).getByRole("button", { name: "a/z" }));
    await userEvent.click(screen.getByRole("button", { name: /Keep A/ }));
    await act(async () => answerFirst({ resolved: true, affected_memory_ids: ["id-a/x2"] }));
    await act(async () => answerSecond({ resolved: true, affected_memory_ids: [] }));
    // a/z leaves; a/w, the one after it, is open: none is skipped.
    await waitFor(() => expect(screen.getByText("value of a/w")).toBeInTheDocument(), WAIT);
    // Both answers came off the count: three are left.
    expect(screen.getByText(/of 3 · resolve in order/)).toBeInTheDocument();
  });

  it("an earlier visit's answer takes its rows off the count read with them", async () => {
    vi.mocked(countConflicts).mockResolvedValue(3);
    onTestFinished(() => {
      vi.mocked(countConflicts).mockImplementation(async () => 0);
    });
    vi.mocked(listConflicts).mockImplementation(async (id) =>
      id.namespace === "ns-b"
        ? { rows: [conflict("b/key")], next_before_version: null }
        : { rows: [conflict("a/x"), conflict("a/y"), conflict("a/z")], next_before_version: null },
    );
    let answerFirst: (v: { resolved: boolean; affected_memory_ids: string[] }) => void = () => {};
    vi.mocked(resolveConflict).mockImplementationOnce(() => new Promise((r) => (answerFirst = r)));
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText(/Conflict 1 of 3/, {}, WAIT);
    await userEvent.click(screen.getByRole("button", { name: /Keep A/ }));
    seed("ns-b");
    await screen.findByText("value of b/key", {}, WAIT);
    seed("ns-a");
    await screen.findByText(/Conflict 1 of 3/, {}, WAIT);
    await act(async () => answerFirst({ resolved: true, affected_memory_ids: [] }));
    await screen.findByText(/Conflict 1 of 2/, {}, WAIT);
  });

  it("a Load more out while a resolve writes an off-screen peer leaves that peer out", async () => {
    let older: (v: { rows: ConflictRow[]; next_before_version: null }) => void = () => {};
    vi.mocked(listConflicts).mockImplementation(async (_id, opts) =>
      opts?.before_version
        ? new Promise((r) => (older = r))
        : { rows: [conflict("a/x"), conflict("a/y")], next_before_version: 7 },
    );
    vi.mocked(resolveConflict).mockResolvedValueOnce({ resolved: true, affected_memory_ids: ["id-a/peer"] });
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of a/x", {}, WAIT);
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    await userEvent.click(screen.getByRole("button", { name: /Allow both/ }));
    const table = screen.getByRole("table", { name: "Conflicts" });
    await waitFor(() => expect(within(table).queryByRole("button", { name: "a/x" })).toBeNull(), WAIT);
    // The page was read before the resolve wrote: it still lists the peer.
    await act(async () => older({ rows: [conflict("a/peer"), conflict("a/old")], next_before_version: null }));
    await waitFor(() => expect(within(table).getByRole("button", { name: "a/old" })).toBeInTheDocument(), WAIT);
    expect(within(table).queryByRole("button", { name: "a/peer" })).toBeNull();
  });

  it("deciding the last loaded conflict keeps the one before it open through a Load more", async () => {
    vi.mocked(listConflicts).mockImplementation(async (_id, opts) =>
      opts?.before_version
        ? { rows: [conflict("k/three")], next_before_version: null }
        : { rows: [conflict("k/one"), conflict("k/two")], next_before_version: 7 },
    );
    vi.mocked(resolveConflict).mockResolvedValueOnce({ resolved: true, affected_memory_ids: [] });
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of k/one", {}, WAIT);
    const table = screen.getByRole("table", { name: "Conflicts" });
    await userEvent.click(within(table).getByRole("button", { name: "k/two" }));
    await userEvent.click(screen.getByRole("button", { name: /Keep A/ }));
    await waitFor(() => expect(within(table).queryByRole("button", { name: "k/two" })).toBeNull(), WAIT);
    expect(screen.getByText("value of k/one")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(within(table).getByRole("button", { name: "k/three" })).toBeInTheDocument(), WAIT);
    // The open conflict is still k/one: the page that joined did not move it.
    expect(within(table).getByRole("button", { name: "k/one" })).toHaveAttribute("aria-current", "true");
  });

  it("peers a resolve wrote while the queue is read again stay out of that read", async () => {
    let reread: (v: { rows: ConflictRow[]; next_before_version: null }) => void = () => {};
    let aReads = 0;
    vi.mocked(listConflicts).mockImplementation(async (id) => {
      if (id.namespace === "ns-b") return { rows: [conflict("b/key")], next_before_version: null };
      aReads += 1;
      if (aReads === 1) return { rows: [conflict("a/x"), conflict("a/y")], next_before_version: null };
      return new Promise((r) => (reread = r));
    });
    let answerFirst: (v: { resolved: boolean; affected_memory_ids: string[] }) => void = () => {};
    vi.mocked(resolveConflict).mockImplementationOnce(() => new Promise((r) => (answerFirst = r)));
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of a/x", {}, WAIT);
    await userEvent.click(screen.getByRole("button", { name: /Allow both/ }));
    seed("ns-b");
    await screen.findByText("value of b/key", {}, WAIT);
    seed("ns-a");
    await waitFor(() => expect(aReads).toBe(2), WAIT);
    await act(async () => answerFirst({ resolved: true, affected_memory_ids: ["id-a/peer"] }));
    // Read before the resolve wrote: it lists the decided row and its peer.
    await act(async () =>
      reread({ rows: [conflict("a/x"), conflict("a/peer"), conflict("a/y")], next_before_version: null }),
    );
    const table = await screen.findByRole("table", { name: "Conflicts" });
    await waitFor(() => expect(within(table).getByRole("button", { name: "a/y" })).toBeInTheDocument(), WAIT);
    expect(within(table).queryByRole("button", { name: "a/peer" })).toBeNull();
    expect(within(table).queryByRole("button", { name: "a/x" })).toBeNull();
  });

  it("a count read before a decision wrote, answering after it, leaves that decision off", async () => {
    let countA: (n: number) => void = () => {};
    let aCounts = 0;
    vi.mocked(countConflicts).mockImplementation(async (id: { namespace: string }) => {
      if (id.namespace === "ns-b") return 1;
      aCounts += 1;
      return aCounts === 1 ? 3 : new Promise<number>((r) => (countA = r));
    });
    onTestFinished(() => {
      vi.mocked(countConflicts).mockImplementation(async () => 0);
    });
    vi.mocked(listConflicts).mockImplementation(async (id) =>
      id.namespace === "ns-b"
        ? { rows: [conflict("b/key")], next_before_version: null }
        : { rows: [conflict("a/x"), conflict("a/y"), conflict("a/z")], next_before_version: null },
    );
    let answerFirst: (v: { resolved: boolean; affected_memory_ids: string[] }) => void = () => {};
    vi.mocked(resolveConflict).mockImplementationOnce(() => new Promise((r) => (answerFirst = r)));
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText(/Conflict 1 of 3/, {}, WAIT);
    await userEvent.click(screen.getByRole("button", { name: /Keep A/ }));
    seed("ns-b");
    await screen.findByText("value of b/key", {}, WAIT);
    seed("ns-a");
    await screen.findByText("value of a/x", {}, WAIT);
    await waitFor(() => expect(aCounts).toBe(2), WAIT);
    await act(async () => answerFirst({ resolved: true, affected_memory_ids: [] }));
    // The count was read before the resolve wrote: it still says 3.
    await act(async () => countA(3));
    await screen.findByText(/Conflict 1 of 2/, {}, WAIT);
  });

  it("takes the count a resolve answers with, and asks for counts fresh", async () => {
    vi.mocked(countConflicts).mockResolvedValue(9);
    onTestFinished(() => {
      vi.mocked(countConflicts).mockImplementation(async () => 0);
    });
    vi.mocked(listConflicts).mockResolvedValue({ rows: [conflict("a/x"), conflict("a/y")], next_before_version: 7 });
    vi.mocked(resolveConflict).mockResolvedValueOnce({ resolved: true, affected_memory_ids: [], total: 6 });
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText(/Conflict 1 of 9/, {}, WAIT);
    expect(vi.mocked(countConflicts).mock.calls.at(-1)?.[1]).toMatchObject({ fresh: true });
    await userEvent.click(screen.getByRole("button", { name: /Keep A/ }));
    // The resolve's own count, not 9 minus one.
    await screen.findByText(/Conflict 1 of 6/, {}, WAIT);
  });

  it("drops a count read still out when a resolve answers with its own count", async () => {
    let lateCount: (n: number) => void = () => {};
    let aCounts = 0;
    vi.mocked(countConflicts).mockImplementation(async () => {
      aCounts += 1;
      return aCounts === 1 ? 5 : new Promise<number>((r) => (lateCount = r));
    });
    onTestFinished(() => {
      vi.mocked(countConflicts).mockImplementation(async () => 0);
    });
    let reads = 0;
    vi.mocked(listConflicts).mockImplementation(async () => {
      reads += 1;
      return { rows: [conflict(`a/x${reads}`), conflict("a/y")], next_before_version: 7 };
    });
    let answer: (v: { resolved: boolean; affected_memory_ids: string[]; total: number }) => void = () => {};
    vi.mocked(resolveConflict).mockImplementationOnce(() => new Promise((r) => (answer = r)));
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText(/Conflict 1 of 5/, {}, WAIT);
    await userEvent.click(screen.getByRole("button", { name: /Keep A/ }));
    // A switch away and back reads the count again; that read is still out.
    seed("ns-b");
    seed("ns-a");
    await waitFor(() => expect(aCounts).toBeGreaterThanOrEqual(2), WAIT);
    await act(async () => answer({ resolved: true, affected_memory_ids: [], total: 3 }));
    await screen.findByText(/of 3 · resolve in order/, {}, WAIT);
    await act(async () => lateCount(5));
    expect(screen.getByText(/of 3 · resolve in order/)).toBeInTheDocument();
  });

  it("ignores a resolve's count for a namespace no longer on screen", async () => {
    vi.mocked(countConflicts).mockImplementation(async (id: { namespace: string }) =>
      id.namespace === "ns-b" ? 4 : 2,
    );
    onTestFinished(() => {
      vi.mocked(countConflicts).mockImplementation(async () => 0);
    });
    vi.mocked(listConflicts).mockImplementation(async (id) =>
      id.namespace === "ns-b"
        ? { rows: [conflict("b/key")], next_before_version: 7 }
        : { rows: [conflict("a/x"), conflict("a/y")], next_before_version: null },
    );
    let answer: (v: { resolved: boolean; affected_memory_ids: string[]; total: number }) => void = () => {};
    vi.mocked(resolveConflict).mockImplementationOnce(() => new Promise((r) => (answer = r)));
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of a/x", {}, WAIT);
    await userEvent.click(screen.getByRole("button", { name: /Keep A/ }));
    seed("ns-b");
    await screen.findByText(/Conflict 1 of 4/, {}, WAIT);
    await act(async () => answer({ resolved: true, affected_memory_ids: [], total: 1 }));
    expect(screen.getByText(/Conflict 1 of 4/)).toBeInTheDocument();
  });

  it("a Load more answered after a namespace switch adds nothing to the new namespace's queue", async () => {
    let older: (v: { rows: ConflictRow[]; next_before_version: null }) => void = () => {};
    vi.mocked(listConflicts).mockImplementation(async (id, opts) => {
      if (id.namespace === "ns-b") return { rows: [conflict("b/key")], next_before_version: null };
      if (opts?.before_version) return new Promise((r) => (older = r));
      return { rows: [conflict("a/key")], next_before_version: 7 };
    });
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of a/key", {}, WAIT);
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    seed("ns-b");
    await screen.findByText("value of b/key", {}, WAIT);
    await act(async () => older({ rows: [conflict("a/older")], next_before_version: null }));
    const table = screen.getByRole("table", { name: "Conflicts" });
    expect(within(table).queryByRole("button", { name: "a/older" })).toBeNull();
    // Nor does the old queue's focus hand-off move focus into the new one.
    expect(within(table).getByRole("button", { name: "b/key" })).not.toHaveFocus();
  });

  it("opens Learnings from the probe without a second read, and reads afresh when it opens again", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    vi.mocked(listLearnings).mockResolvedValue({ rows: [learning("a/learned")] });
    seed("ns-a");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Learnings/ }, WAIT));
    await screen.findByText("a/learned", {}, WAIT);
    // The probe's read seeded it.
    expect(listLearnings).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("tab", { name: /Conflicts/ }));
    await userEvent.click(screen.getByRole("tab", { name: /Learnings/ }));
    await waitFor(() => expect(listLearnings).toHaveBeenCalledTimes(2), WAIT);
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

  it("does not count the old namespace's total over the new one's queue", async () => {
    vi.mocked(countConflicts).mockImplementation(async (id: { namespace: string }) =>
      id.namespace === "ns-a" ? 120 : new Promise<number>(() => {}),
    );
    vi.mocked(listConflicts).mockImplementation(async (id: { namespace: string }) =>
      id.namespace === "ns-a"
        ? { rows: [conflict("a/one"), conflict("a/two")], next_before_version: 7 }
        : { rows: [conflict("b/one")], next_before_version: null },
    );
    seed("ns-a");
    render(<ReviewQueue />);
    expect(await screen.findByText("2 of 120 loaded", {}, WAIT)).toBeInTheDocument();
    seed("ns-b");
    await screen.findByText("value of b/one", {}, WAIT);
    expect(screen.queryByText(/of 120/)).toBeNull();
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
    expect(await screen.findByText("2 of 3 loaded", {}, WAIT)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(within(table).getAllByRole("row")).toHaveLength(4), WAIT);
    expect(vi.mocked(listConflicts).mock.calls.at(-1)?.[1]?.before_version).toBe(7);
    expect(screen.getByText("3 of 3 loaded")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
    // The last page took the button that held the focus: it goes to the first conflict added.
    expect(within(table).getByRole("button", { name: "k/three" })).toHaveFocus();

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
    expect(screen.getByText("1 of 1 loaded")).toBeInTheDocument();
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

  it("keeps the page Load more added while a decision was out", async () => {
    vi.mocked(countConflicts).mockResolvedValue(3);
    vi.mocked(listConflicts).mockImplementation(async (_id, opts) =>
      opts?.before_version
        ? { rows: [conflict("k/three")], next_before_version: null }
        : { rows: [conflict("k/one"), conflict("k/two")], next_before_version: 7 },
    );
    let decided: (v: never) => void = () => {};
    vi.mocked(resolveConflict).mockImplementationOnce(() => new Promise((r) => (decided = r)));
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of k/one", {}, WAIT);
    const table = screen.getByRole("table", { name: "Conflicts" });
    await userEvent.click(screen.getByRole("button", { name: /Keep A/ }));
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(within(table).getByRole("button", { name: "k/three" })).toBeInTheDocument(), WAIT);
    await act(async () => decided({ resolved: true, affected_memory_ids: [] } as never));
    await waitFor(() => expect(within(table).queryByRole("button", { name: "k/one" })).toBeNull(), WAIT);
    expect(within(table).getByRole("button", { name: "k/two" })).toBeInTheDocument();
    expect(within(table).getByRole("button", { name: "k/three" })).toBeInTheDocument();
  });

  it("deciding the last loaded conflict while more wait shows the queue loading, not 'No conflicts'", async () => {
    vi.mocked(countConflicts).mockResolvedValue(3);
    let reads = 0;
    vi.mocked(listConflicts).mockImplementation(async () => {
      reads += 1;
      if (reads === 1) return { rows: [conflict("k/one")], next_before_version: 7 };
      return new Promise(() => {});
    });
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of k/one", {}, WAIT);
    await userEvent.click(screen.getByRole("button", { name: /Keep A/ }));
    await waitFor(() => expect(reads).toBe(2), WAIT);
    expect(screen.queryByText("No conflicts")).toBeNull();
    expect(screen.getByLabelText("Loading conflicts")).toBeInTheDocument();
  });

  it("deciding the only conflict, with none past it, says 'No conflicts' without reading again", async () => {
    vi.mocked(countConflicts).mockResolvedValue(1);
    vi.mocked(listConflicts).mockResolvedValue({ rows: [conflict("k/one")], next_before_version: null });
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of k/one", {}, WAIT);
    await userEvent.click(screen.getByRole("button", { name: /Keep A/ }));
    expect(await screen.findByText("No conflicts", {}, WAIT)).toBeInTheDocument();
    expect(listConflicts).toHaveBeenCalledTimes(1);
  });

  it("holds the queue's picks and the reason while a decision is out", async () => {
    vi.mocked(countConflicts).mockResolvedValue(2);
    vi.mocked(listConflicts).mockResolvedValue({
      rows: [conflict("k/one"), conflict("k/two")],
      next_before_version: null,
    });
    let decided: (v: never) => void = () => {};
    vi.mocked(resolveConflict).mockImplementationOnce(() => new Promise((r) => (decided = r)));
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of k/one", {}, WAIT);
    const table = screen.getByRole("table", { name: "Conflicts" });
    await userEvent.click(screen.getByRole("button", { name: /Keep A/ }));
    expect(within(table).getByRole("button", { name: "k/two" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByLabelText("Reason for the decision")).toBeDisabled();
    await act(async () => decided({ resolved: true, affected_memory_ids: [] } as never));
    await waitFor(
      () => expect(within(table).getByRole("button", { name: "k/two" })).not.toHaveAttribute("aria-disabled"),
      WAIT,
    );
  });

  it("counts the queue under its heading and shows each conflict's time as a list time", async () => {
    vi.mocked(countConflicts).mockResolvedValue(4);
    vi.mocked(listConflicts).mockResolvedValue({
      rows: [conflict("k/one"), conflict("k/two")],
      next_before_version: 7,
    });
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of k/one", {}, WAIT);
    const heading = screen.getByRole("heading", { name: "Queue", level: 2 });
    await waitFor(() => expect(heading.parentElement).toHaveTextContent("2 of 4 loaded"), WAIT);
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

  // The first read waits 300 ms before its loader shows, like every loader
  // (ui/LoadingSkeleton), under the header row already in place.
  it("draws the queue's first read as the table's header over a skeleton", async () => {
    vi.mocked(listConflicts).mockReturnValue(new Promise(() => {}));
    seed("ns-a");
    render(<ReviewQueue />);
    const queue = await screen.findByRole("table", { name: "Conflicts" }, WAIT);
    expect(within(queue).queryByText("Loading data")).toBeNull();
    expect(within(queue).getByRole("status", { name: "Loading conflicts" })).toHaveAttribute("aria-busy", "true");
  });
});
