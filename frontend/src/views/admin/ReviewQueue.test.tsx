import { act, cleanup, render, screen, waitFor, within } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";
import type { AllowlistRow, AutoResolvedRow, ConflictRow, LearningEntry, MyNamespaceEntry, WhoAmI } from "../../api";
import { resetNamespaceRoleCache } from "../../lib/namespaceRole";
import { useStore } from "../../lib/state";
import { invalidateShared } from "../../lib/sharedReads";
import { axeViolations } from "../../test/axe";
import { ReviewQueue } from "./ReviewQueue";
import { ApiError } from "../../lib/errors";
import { SENSITIVE_READ_BY_KEY } from "../../lib/format";

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
    listAutoResolved: vi.fn(),
    listUnsuperseded: vi.fn(),
    restoreMemory: vi.fn(),
    getMemoryById: vi.fn(),
    listProposals: vi.fn(async () => ({ proposals: [], total: 0 })),
    proposeRevision: vi.fn(),
    listMemoryDuplicates: vi.fn(async () => ({ clusters: [] })),
    listDocumentDuplicates: vi.fn(async () => ({ doc_clusters: [] })),
    listMemoryAgents: vi.fn(async () => []),
  };
});
// The page's yes/no probe is shared with the sidebar and lives in api/audit.
vi.mock("../../api/audit", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/audit")>()),
  hasAutoResolved: vi.fn(),
}));
vi.mock("../../components/ConfirmDialog", () => ({ confirmAction: async () => true }));
const toast = vi.hoisted(() => vi.fn());
vi.mock("../../components/Toast", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../components/Toast")>()),
  toast,
}));
import { hasAutoResolved } from "../../api/audit";
import {
  countConflicts,
  getMemoryById,
  listAllowlist,
  listAutoResolved,
  listConflicts,
  listLearnings,
  listUnsuperseded,
  proposeRevision,
  resolveConflict,
  restoreMemory,
} from "../../api";

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

const retired = (key: string, extra: Partial<AutoResolvedRow> = {}): AutoResolvedRow => ({
  memory_id: `id-${key}`,
  memory_key: key,
  agent: "claude",
  user_id: "u1",
  session_id: "s1",
  superseded_by: `id-${key}-new`,
  reason: "conflict_resolver",
  ts: "2026-10-01T10:00:00Z",
  // Served now: whether a record's Undo shows as Restored turns on the record.
  current: { status: "active", superseded_by: "" },
  ...extra,
});

const person: WhoAmI = {
  authenticated: true,
  subject: "o@example.com",
  org: ORG,
  roles: ["USER"],
  namespaces: ["ns-a", "ns-b"],
};

const member = (namespace: string, role: MyNamespaceEntry["role"]): MyNamespaceEntry => ({
  namespace,
  role,
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
});

/** `role`: the viewer's role on ns-a and ns-b; unset, the role is unknown. */
function seed(namespace: string, role?: MyNamespaceEntry["role"]) {
  act(() => {
    useStore.setState({
      identity: { org: ORG, namespace, agent: "loom-ui" },
      whoami: role ? person : null,
      myNamespaces: role ? [member("ns-a", role), member("ns-b", role)] : null,
    });
  });
}

const page = (rows: AutoResolvedRow[], next_cursor: string | null = null) => ({ rows, next_cursor });
/** The panel's own page reads, not the probe's one-record ask. */
const panelReads = () => vi.mocked(listAutoResolved).mock.calls.filter(([, o]) => o?.limit !== 5);

describe("ReviewQueue", () => {
  beforeEach(() => {
    vi.mocked(listConflicts).mockReset();
    vi.mocked(listAllowlist).mockReset();
    vi.mocked(listLearnings).mockReset();
    vi.mocked(listAllowlist).mockResolvedValue({ rows: [] });
    vi.mocked(listLearnings).mockResolvedValue({ rows: [] });
    vi.mocked(listAutoResolved).mockReset();
    vi.mocked(listUnsuperseded).mockReset();
    vi.mocked(restoreMemory).mockReset();
    vi.mocked(listAutoResolved).mockResolvedValue(page([]));
    resetNamespaceRoleCache();
    invalidateShared();
    vi.mocked(listUnsuperseded).mockResolvedValue(new Map());
    vi.mocked(getMemoryById).mockReset();
    vi.mocked(getMemoryById).mockResolvedValue({
      found: false,
      memory_id: "",
      namespace: "",
      memory_key: "",
      value: null,
      version: null,
      pinned: false,
      created_at: null,
    });
    // Like the real one: the newest record of the same read.
    vi.mocked(hasAutoResolved).mockImplementation(
      async (id) => (await listAutoResolved(id, { limit: 5 })).rows.length > 0,
    );
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ whoami: null, myNamespaces: null });
  });

  it("suggests a decision over every peer, in the words of the buttons", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [conflict("city")], next_before_version: null });
    vi.mocked(proposeRevision).mockResolvedValue({
      found: true,
      suggestions: [{ decision: "contract", target_local_id: 1, peer_local_ids: [2], rationale: "a peer is pinned" }],
    });
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("value of city", {}, WAIT);
    await userEvent.click(screen.getByRole("button", { name: "Suggest a decision" }));
    expect(await within(await screen.findByRole("status")).findByText("Drop A")).toBeInTheDocument();
    expect(screen.getByText(/a peer is pinned/)).toBeInTheDocument();
    expect(vi.mocked(proposeRevision).mock.calls[0]).toHaveLength(2);
    expect(resolveConflict).not.toHaveBeenCalled();
  });

  it("shows a high conflict as sensitive, not as an empty value", async () => {
    vi.mocked(listConflicts).mockResolvedValue({
      rows: [{ ...conflict("vault"), value: null, sensitivity: "high" }],
      next_before_version: null,
    });
    seed("ns-a");
    render(<ReviewQueue />);
    expect(await screen.findByText("Sensitive: read it by key in Memories", {}, WAIT)).toBeTruthy();
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

  it("shows a bot's rule with its agent, its users, sessions and writers, and what promoting it does", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    vi.mocked(listLearnings).mockResolvedValue({
      rows: [
        {
          ...learning("assistant_rule_refunds"),
          source_kind: "assistant_turn",
          agent: "support-bot",
          supporter_count: 2,
          supporter_sessions: 3,
          supporter_writers: 1,
        },
        // Filed with no agent ("-"): it applies namespace-wide, and says so.
        { ...learning("assistant_rule_any"), source_kind: "assistant_turn", agent: "-", supporter_count: 1 },
        // A server a release behind sends no agent and no user count: no agent line.
        { ...learning("assistant_rule_old"), source_kind: "assistant_turn", supporter_count: 1 },
      ],
    });
    seed("ns-a");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Learnings/ }, WAIT));
    await screen.findByText("assistant_rule_refunds", {}, WAIT);
    expect(screen.getByText(/2 users, 3 sessions, 1 writer/)).toBeTruthy();
    expect(screen.getAllByText(/A bot's rule about itself/)).toHaveLength(3);
    expect(screen.getByText(/agent support-bot/)).toBeTruthy();
    expect(screen.getAllByText(/1 supporter ·/)).toHaveLength(2);
    expect(screen.getAllByText(/every agent in this namespace/)).toHaveLength(1);
    // The rows that name an agent ("-" included) say which agents Promote reaches; the old one does not.
    expect(screen.getAllByText(/Promote publishes this rule to the namespace's knowledge/)).toHaveLength(2);
    expect(
      screen.getByText(
        "Promote publishes this rule to the namespace's knowledge for support-bot, for every user; reads under agent * and knowledge.search also find it.",
      ),
    ).toBeTruthy();
    expect(screen.getByText(/knowledge for every agent and every user\./)).toBeTruthy();
  });

  it("says a bot rule's text went with a user erasure and its decision stands", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    vi.mocked(listLearnings).mockResolvedValueOnce({ rows: [learning("k/pending")] });
    vi.mocked(listLearnings).mockResolvedValue({
      rows: [
        {
          ...learning("assistant_rule_refunds"),
          source_kind: "assistant_turn",
          agent: "support-bot",
          status: "rejected",
          proposed_value: null,
        },
      ],
    });
    seed("ns-a");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Learnings/ }, WAIT));
    await userEvent.click(await screen.findByRole("button", { name: "Rejected" }, WAIT));
    expect(await screen.findByText("Rule removed by a user erasure; the decision stands.", {}, WAIT)).toBeTruthy();
  });

  it("says what a learning is when the queue is empty", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    vi.mocked(listLearnings).mockResolvedValueOnce({ rows: [learning("k/pending")] });
    vi.mocked(listLearnings).mockResolvedValue({ rows: [] });
    seed("ns-a");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Learnings/ }, WAIT));
    await userEvent.click(await screen.findByRole("button", { name: "Rejected" }, WAIT));
    expect(await screen.findByText("No rejected learnings", {}, WAIT)).toBeTruthy();
    expect(screen.getByText(/Nothing is promoted without a review/)).toBeTruthy();
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

  it("shows the peer's own key and value, not the conflict's", async () => {
    const a = { ...conflict("lesson_a"), conflict_with: ["id-lesson_b"] };
    // The peer is conflicted too, so the queue already holds it: no read by id.
    const b = { ...conflict("lesson_b"), conflict_with: ["id-lesson_a"] };
    vi.mocked(listConflicts).mockResolvedValue({ rows: [a, b], next_before_version: null });
    seed("ns-a");
    render(<ReviewQueue />);
    const peer = (await screen.findByText("B · conflicting peer", {}, WAIT)).parentElement as HTMLElement;
    const self = screen.getByText("A · this memory").parentElement as HTMLElement;
    expect(self.textContent).toContain("lesson_a");
    expect(self.textContent).toContain("value of lesson_a");
    expect(peer.textContent).toContain("lesson_b");
    expect(peer.textContent).toContain("value of lesson_b");
    expect(peer.textContent).not.toContain("lesson_a");
    expect(getMemoryById).not.toHaveBeenCalled();
  });

  it("reads a peer the queue does not hold by its id", async () => {
    const a = { ...conflict("lesson_a"), conflict_with: ["id-elsewhere"] };
    vi.mocked(listConflicts).mockResolvedValue({ rows: [a], next_before_version: null });
    vi.mocked(getMemoryById).mockResolvedValue({
      found: true,
      memory_id: "id-elsewhere",
      namespace: "ns-a",
      memory_key: "lesson_other",
      value: "the other value",
      version: 3,
      pinned: false,
      created_at: "2026-09-21T00:00:00Z",
    });
    seed("ns-a");
    render(<ReviewQueue />);
    const peer = (await screen.findByText("B · conflicting peer", {}, WAIT)).parentElement as HTMLElement;
    await waitFor(() => expect(peer.textContent).toContain("the other value"), WAIT);
    expect(peer.textContent).toContain("lesson_other");
    expect(peer.textContent).not.toContain("lesson_a");
    expect(getMemoryById).toHaveBeenCalledWith(ORG, "id-elsewhere", expect.objectContaining({ omit_high_value: true }));
  });

  it("shows a high peer the queue holds as read by key, without reading it by id", async () => {
    const peer = { ...conflict("vault"), value: null, sensitivity: "high", conflict_with: ["id-lesson_a"] };
    const a = { ...conflict("lesson_a"), conflict_with: ["id-vault"] };
    vi.mocked(listConflicts).mockResolvedValue({ rows: [a, peer], next_before_version: null });
    seed("ns-a");
    render(<ReviewQueue />);
    const side = (await screen.findByText("B · conflicting peer", {}, WAIT)).parentElement as HTMLElement;
    await waitFor(() => expect(side.textContent).toContain(SENSITIVE_READ_BY_KEY), WAIT);
    expect(getMemoryById).not.toHaveBeenCalled();
  });

  it("shows a high peer read by its id as read by key, not its value", async () => {
    const a = { ...conflict("lesson_a"), conflict_with: ["id-elsewhere"] };
    vi.mocked(listConflicts).mockResolvedValue({ rows: [a], next_before_version: null });
    vi.mocked(getMemoryById).mockResolvedValue({
      found: true,
      memory_id: "id-elsewhere",
      namespace: "ns-a",
      memory_key: "vault_code",
      value: "the code is 4417",
      version: 3,
      pinned: false,
      created_at: "2026-09-21T00:00:00Z",
      sensitivity: "high",
    });
    seed("ns-a");
    render(<ReviewQueue />);
    const side = (await screen.findByText("B · conflicting peer", {}, WAIT)).parentElement as HTMLElement;
    await waitFor(() => expect(side.textContent).toContain(SENSITIVE_READ_BY_KEY), WAIT);
    expect(side.textContent).not.toContain("4417");
  });

  it("names a peer that reads nothing by its id only", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [conflict("lesson_a")], next_before_version: null });
    seed("ns-a");
    render(<ReviewQueue />);
    const peer = (await screen.findByText("B · conflicting peer", {}, WAIT)).parentElement as HTMLElement;
    await waitFor(() => expect(getMemoryById).toHaveBeenCalled(), WAIT);
    expect(peer.textContent).toContain("peer-0000000");
    expect(peer.textContent).not.toContain("lesson_a");
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

  it("says nothing was retired when the tab's read finds none", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    // The probe saw a record (the tab shows); by the panel's own read it is gone.
    vi.mocked(listAutoResolved).mockImplementation(async (_id, o) =>
      o?.limit === 5 ? page([retired("user_city")]) : page([]),
    );
    seed("ns-a", "writer");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Auto-resolved/ }, WAIT));
    expect(await screen.findByText("Nothing retired automatically", {}, WAIT)).toBeTruthy();
    expect(screen.queryByRole("table", { name: "Auto-resolved memories" })).toBeNull();
  });

  it("lists what Loom retired on its own, why and behind what, with an Undo", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    vi.mocked(listAutoResolved).mockResolvedValue(
      page([
        retired("user_city", {
          superseded_by_key: "user_city_current",
          label: "contradiction",
        }),
        retired("user_job", {
          reason: "value_update",
          superseded_by: "abc123",
          ts: "2026-09-30T10:00:00Z",
          unconfirmed: true,
        }),
      ]),
    );
    vi.mocked(restoreMemory).mockResolvedValue({ restored: true, from_status: "superseded" });
    seed("ns-a", "writer");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Auto-resolved/ }, WAIT));

    expect(await screen.findByText("user_city", {}, WAIT)).toBeTruthy();
    expect(screen.getByText("Replaced by a later statement of the same attribute")).toBeTruthy();
    expect(screen.getByText("user_city_current")).toBeTruthy();
    expect(screen.getByText("Replaced by an updated value")).toBeTruthy();
    // No key for the row it retired behind: its id.
    expect(screen.getByText("abc123")).toBeTruthy();
    // A retirement the re-read could not confirm says so.
    expect(screen.getAllByText(/may not have taken effect/)).toHaveLength(1);
    // Restores are read back to the oldest retirement on show, not further.
    await waitFor(
      () =>
        expect(listUnsuperseded).toHaveBeenCalledWith(
          { org: ORG, namespace: "ns-a" },
          expect.objectContaining({ since: "2026-09-30T10:00:00Z" }),
        ),
      WAIT,
    );

    await userEvent.click(await screen.findByRole("button", { name: "Undo retirement of user_city" }, WAIT));
    await waitFor(() =>
      expect(restoreMemory).toHaveBeenCalledWith({ org: ORG, namespace: "ns-a", agent: "claude" }, "user_city", {
        user_id: "u1",
        session_id: "s1",
        if_superseded_by: "id-user_city-new",
      }),
    );
    expect(await screen.findByText("Restored", {}, WAIT)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Undo retirement of user_city" })).toBeNull();
    expect(screen.getByRole("button", { name: "Undo retirement of user_job" })).toBeTruthy();
  });

  it("shows a retirement already undone as Restored, unless it was retired again since", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    vi.mocked(listAutoResolved).mockResolvedValue(
      page([retired("undone"), retired("again", { ts: "2026-10-03T10:00:00Z" })]),
    );
    vi.mocked(listUnsuperseded).mockResolvedValue(
      new Map([
        ["id-undone", "2026-10-02T10:00:00Z"],
        ["id-again", "2026-10-02T10:00:00Z"],
      ]),
    );
    seed("ns-a", "writer");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Auto-resolved/ }, WAIT));
    expect(await screen.findByText("Restored", {}, WAIT)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Undo retirement of undone" })).toBeNull();
    expect(screen.getByRole("button", { name: "Undo retirement of again" })).toBeTruthy();
  });

  it("offers Undo for a row an Undo record names but that is still retired behind that memory", async () => {
    // The record was written and its restore failed.
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    vi.mocked(listAutoResolved).mockResolvedValue(
      page([
        retired("orphan", { current: { status: "superseded", superseded_by: "id-orphan-new" } }),
        retired("moved", { current: { status: "superseded", superseded_by: "id-elsewhere" } }),
      ]),
    );
    vi.mocked(listUnsuperseded).mockResolvedValue(
      new Map([
        ["id-orphan", "2026-10-02T10:00:00Z"],
        ["id-moved", "2026-10-02T10:00:00Z"],
      ]),
    );
    seed("ns-a", "writer");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Auto-resolved/ }, WAIT));
    expect(await screen.findByRole("button", { name: "Undo retirement of orphan" }, WAIT)).toBeTruthy();
    // Retired behind another memory since: this retirement is undone.
    expect(screen.queryByRole("button", { name: "Undo retirement of moved" })).toBeNull();
    expect(screen.getAllByText("Restored")).toHaveLength(1);
  });

  it("offers Undo for a row whose state could not be read, whatever its records say", async () => {
    // A repeat Undo of a restored row answers restored, so offering it is harmless.
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    vi.mocked(listAutoResolved).mockResolvedValue(page([retired("unknown", { current: undefined })]));
    vi.mocked(listUnsuperseded).mockResolvedValue(new Map([["id-unknown", "2026-10-02T10:00:00Z"]]));
    seed("ns-a", "writer");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Auto-resolved/ }, WAIT));
    expect(await screen.findByRole("button", { name: "Undo retirement of unknown" }, WAIT)).toBeTruthy();
    expect(screen.queryByText("Restored")).toBeNull();
  });

  it("orders audit times by instant, not by their text", async () => {
    // A whole second is sent without a fraction, and "." sorts before "Z".
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    vi.mocked(listAutoResolved).mockResolvedValue(
      page([
        retired("later", { ts: "2026-10-01T12:00:00.500000Z" }),
        retired("undone", { ts: "2026-10-01T12:00:00Z" }),
      ]),
    );
    vi.mocked(listUnsuperseded).mockResolvedValue(new Map([["id-undone", "2026-10-01T12:00:00.400000Z"]]));
    seed("ns-a", "writer");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Auto-resolved/ }, WAIT));
    expect(await screen.findByText("Restored", {}, WAIT)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Undo retirement of undone" })).toBeNull();
    expect(vi.mocked(listUnsuperseded).mock.calls[0][1]).toMatchObject({ since: "2026-10-01T12:00:00Z" });
  });

  it("keeps each record of one memory apart", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    vi.mocked(listAutoResolved).mockResolvedValue(
      page([
        retired("user_city", { superseded_by: "id-third", ts: "2026-10-05T10:00:00Z" }),
        retired("user_city", { superseded_by: "id-second", ts: "2026-10-03T10:00:00Z" }),
        retired("user_city", { superseded_by: "id-first" }),
      ]),
    );
    // Restored once, between the first and second retirement.
    vi.mocked(listUnsuperseded).mockResolvedValue(new Map([["id-user_city", "2026-10-02T10:00:00Z"]]));
    vi.mocked(restoreMemory).mockImplementation(async (_id, _key, opts) =>
      opts?.if_superseded_by === "id-third"
        ? { restored: true, from_status: "superseded" }
        : { restored: false, from_status: "" },
    );
    seed("ns-a", "writer");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Auto-resolved/ }, WAIT));
    const undos = await screen.findAllByRole("button", { name: "Undo retirement of user_city" }, WAIT);
    expect(undos).toHaveLength(2);
    expect(screen.getAllByText("Restored")).toHaveLength(1);

    await userEvent.click(undos[0]);
    await waitFor(() => expect(screen.getAllByText("Restored")).toHaveLength(2), WAIT);
    expect(restoreMemory).toHaveBeenLastCalledWith(
      expect.anything(),
      "user_city",
      expect.objectContaining({ if_superseded_by: "id-third" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Undo retirement of user_city" }));
    expect(await screen.findByText("Changed since", {}, WAIT)).toBeTruthy();
    expect(restoreMemory).toHaveBeenLastCalledWith(
      expect.anything(),
      "user_city",
      expect.objectContaining({ if_superseded_by: "id-second" }),
    );
    expect(screen.getAllByText("Restored")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Undo retirement of user_city" })).toBeNull();
  });

  it("offers no Undo before the restores are read", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    vi.mocked(listAutoResolved).mockResolvedValue(page([retired("user_city")]));
    let finish: (m: Map<string, string>) => void = () => {};
    vi.mocked(listUnsuperseded).mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    seed("ns-a", "writer");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Auto-resolved/ }, WAIT));
    await screen.findByText("user_city", {}, WAIT);
    expect(screen.queryByRole("button", { name: /Undo retirement/ })).toBeNull();
    await act(async () => {
      finish(new Map());
    });
    expect(await screen.findByRole("button", { name: "Undo retirement of user_city" }, WAIT)).toBeTruthy();
  });

  it("says a row is no longer retired when the restore finds nothing to undo", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    vi.mocked(listAutoResolved).mockResolvedValue(page([retired("user_city")]));
    vi.mocked(restoreMemory).mockResolvedValue({ restored: false, from_status: "" });
    seed("ns-a", "writer");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Auto-resolved/ }, WAIT));
    await userEvent.click(await screen.findByRole("button", { name: "Undo retirement of user_city" }, WAIT));
    expect(await screen.findByText("Changed since", {}, WAIT)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Undo retirement of user_city" })).toBeNull();
  });

  it("keeps the Undo after a refused restore", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    vi.mocked(listAutoResolved).mockResolvedValue(page([retired("user_city")]));
    vi.mocked(restoreMemory).mockRejectedValue(
      new ApiError({ status: 403, code: "AUTH_INSUFFICIENT", message: "writer role required" }),
    );
    seed("ns-a", "writer");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Auto-resolved/ }, WAIT));
    const undo = await screen.findByRole("button", { name: "Undo retirement of user_city" }, WAIT);
    await userEvent.click(undo);
    await waitFor(() => expect(restoreMemory).toHaveBeenCalledTimes(1), WAIT);
    await waitFor(() => expect(undo).not.toBeDisabled(), WAIT);
    expect(screen.queryByText("Restored")).toBeNull();
    expect(screen.queryByText("Changed since")).toBeNull();
  });

  it("offers Undo only for a record that names the row it retired behind", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    vi.mocked(listAutoResolved).mockResolvedValue(
      page([retired("user_city"), retired("user_job", { superseded_by: "" })]),
    );
    seed("ns-a", "writer");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Auto-resolved/ }, WAIT));
    await screen.findByRole("button", { name: "Undo retirement of user_city" }, WAIT);
    expect(screen.getAllByRole("button", { name: /Undo retirement/ })).toHaveLength(1);
    expect(screen.queryByText("Changed since")).toBeNull();
  });

  it("shows a viewer the retirements without an Undo", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    vi.mocked(listAutoResolved).mockResolvedValue(page([retired("user_city")]));
    seed("ns-a", "viewer");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Auto-resolved/ }, WAIT));
    await screen.findByText("user_city", {}, WAIT);
    await waitFor(() => expect(listUnsuperseded).toHaveBeenCalled(), WAIT);
    expect(screen.queryByRole("button", { name: /Undo retirement/ })).toBeNull();
  });

  it("pages older retirements with Load more", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    vi.mocked(listAutoResolved).mockImplementation(async (_id, opts) =>
      opts?.cursor === "c1" ? page([retired("older", { ts: "2026-09-01T10:00:00Z" })]) : page([retired("newer")], "c1"),
    );
    seed("ns-a", "writer");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Auto-resolved/ }, WAIT));
    await userEvent.click(await screen.findByRole("button", { name: "Load more" }, WAIT));
    expect(await screen.findByText("older", {}, WAIT)).toBeTruthy();
    expect(screen.getByText("newer")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
    await waitFor(
      () =>
        expect(listUnsuperseded).toHaveBeenLastCalledWith(
          { org: ORG, namespace: "ns-a" },
          expect.objectContaining({ since: "2026-09-01T10:00:00Z" }),
        ),
      WAIT,
    );
  });

  it("loads one older page however often Load more is pressed, and keeps what it knows meanwhile", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    let finishPage: (p: ReturnType<typeof page>) => void = () => {};
    vi.mocked(listAutoResolved).mockImplementation((_id, opts) =>
      opts?.cursor
        ? new Promise((resolve) => {
            finishPage = resolve;
          })
        : Promise.resolve(page([retired("newer")], "c1")),
    );
    let finishOlderRestores: (m: Map<string, string>) => void = () => {};
    vi.mocked(listUnsuperseded).mockImplementation((_id, opts) =>
      opts.since === "2026-09-01T10:00:00Z"
        ? new Promise((resolve) => {
            finishOlderRestores = resolve;
          })
        : Promise.resolve(new Map()),
    );
    seed("ns-a", "writer");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Auto-resolved/ }, WAIT));
    await screen.findByRole("button", { name: "Undo retirement of newer" }, WAIT);
    const more = screen.getByRole("button", { name: "Load more" });
    await userEvent.click(more);
    await userEvent.click(more);
    expect(vi.mocked(listAutoResolved).mock.calls.filter(([, o]) => o?.cursor === "c1")).toHaveLength(1);
    await act(async () => {
      finishPage(page([retired("older", { ts: "2026-09-01T10:00:00Z" })]));
    });
    expect(await screen.findByText("older", {}, WAIT)).toBeTruthy();
    // The newer row's Undo stays while the older stretch's restores are read; the older
    // row waits for them.
    expect(screen.getByRole("button", { name: "Undo retirement of newer" })).toBeTruthy();
    expect(screen.getByText("Checking…")).toBeTruthy();
    expect(screen.getByText("Nothing older within 30 days.")).toBeTruthy();
    await act(async () => {
      finishOlderRestores(new Map([["id-older", "2026-09-02T10:00:00Z"]]));
    });
    expect(await screen.findByText("Restored", {}, WAIT)).toBeTruthy();
    expect(screen.queryByText("Checking…")).toBeNull();
  });

  it("drops an older page that lands after a namespace switch", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    let finishPage: (p: ReturnType<typeof page>) => void = () => {};
    vi.mocked(listAutoResolved).mockImplementation((id, opts) => {
      if (opts?.cursor)
        return new Promise((resolve) => {
          finishPage = resolve;
        });
      return Promise.resolve(page([retired(`${id.namespace}-row`)], "c1"));
    });
    seed("ns-a", "writer");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Auto-resolved/ }, WAIT));
    await userEvent.click(await screen.findByRole("button", { name: "Load more" }, WAIT));
    seed("ns-b", "writer");
    await act(async () => {
      finishPage(page([retired("a-stale")]));
    });
    await userEvent.click(await screen.findByRole("tab", { name: /Auto-resolved/ }, WAIT));
    expect(await screen.findByText("ns-b-row", {}, WAIT)).toBeTruthy();
    expect(screen.queryByText("a-stale")).toBeNull();
    expect(screen.queryByText("ns-a-row")).toBeNull();
  });

  it("keeps an Undo's outcome across a tab switch", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    vi.mocked(listAutoResolved).mockResolvedValue(page([retired("user_city")]));
    vi.mocked(restoreMemory).mockResolvedValue({ restored: false, from_status: "" });
    seed("ns-a", "writer");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Auto-resolved/ }, WAIT));
    await userEvent.click(await screen.findByRole("button", { name: "Undo retirement of user_city" }, WAIT));
    await screen.findByText("Changed since", {}, WAIT);
    await userEvent.click(screen.getByRole("tab", { name: /Conflicts/ }));
    await userEvent.click(screen.getByRole("tab", { name: /Auto-resolved/ }));
    expect(screen.getByText("Changed since")).toBeTruthy();
    expect(panelReads()).toHaveLength(1);
  });

  // The route budget: loading the page asks the audit log one yes/no, shared with the
  // sidebar; the tab's rows are read when it is opened.
  it("asks the audit log once on load and reads the rows when the tab opens", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    vi.mocked(listAutoResolved).mockResolvedValue(page([retired("user_city")]));
    seed("ns-a", "writer");
    render(<ReviewQueue />);
    const tab = await screen.findByRole("tab", { name: /Auto-resolved/ }, WAIT);
    expect(listAutoResolved).toHaveBeenCalledTimes(1);
    expect(panelReads()).toHaveLength(0);
    await userEvent.click(tab);
    expect(await screen.findByText("user_city", {}, WAIT)).toBeTruthy();
    expect(panelReads()).toHaveLength(1);
  });

  it("does not read the audit log while the role is unknown", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    vi.mocked(listAutoResolved).mockResolvedValue(page([retired("user_city")]));
    seed("ns-a");
    render(<ReviewQueue />);
    await screen.findByText("No conflicts", {}, WAIT);
    expect(listAutoResolved).not.toHaveBeenCalled();
    expect(screen.queryByRole("tab", { name: /Auto-resolved/ })).toBeNull();
  });

  it("hides the Auto-resolved tab when Loom retired nothing", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    seed("ns-a", "writer");
    render(<ReviewQueue />);
    await screen.findByText("No conflicts", {}, WAIT);
    await waitFor(() => expect(listAutoResolved).toHaveBeenCalled(), WAIT);
    expect(screen.queryByRole("tab", { name: /Auto-resolved/ })).toBeNull();
  });
  it("announces an Undo's outcome in one live region for the panel", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    vi.mocked(listAutoResolved).mockResolvedValue(page([retired("user_city"), retired("user_job")]));
    vi.mocked(restoreMemory).mockResolvedValue({ restored: false, from_status: "" });
    seed("ns-a", "writer");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Auto-resolved/ }, WAIT));
    await screen.findByRole("button", { name: "Undo retirement of user_city" }, WAIT);
    const panel = screen.getByRole("tabpanel", { name: /Auto-resolved/ });
    // Rows changing (the restores read, a page loaded) announce nothing.
    expect(panel.querySelectorAll("[aria-live]")).toHaveLength(1);
    expect(panel.querySelectorAll('[role="status"]')).toHaveLength(0);
    await userEvent.click(screen.getByRole("button", { name: "Undo retirement of user_city" }));
    await screen.findByText("Changed since", {}, WAIT);
    expect(panel.querySelector("[aria-live]")?.textContent).toBe("user_city changed since; nothing undone");
    expect(await axeViolations(panel)).toEqual([]);
  });

  it("re-announces a second Undo of the same key", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    // Two retirements of one key, from two sessions.
    vi.mocked(listAutoResolved).mockResolvedValue(
      page([retired("user_city"), retired("user_city", { memory_id: "id-user_city-2", session_id: "s2" })]),
    );
    vi.mocked(restoreMemory).mockResolvedValue({ restored: true, from_status: "superseded" });
    seed("ns-a", "writer");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Auto-resolved/ }, WAIT));
    const [first, second] = await screen.findAllByRole("button", { name: "Undo retirement of user_city" }, WAIT);
    const live = () => screen.getByRole("tabpanel", { name: /Auto-resolved/ }).querySelector("[aria-live]");
    await userEvent.click(first);
    await waitFor(() => expect(live()?.textContent?.trim()).toBe("Restored user_city"));
    const once = live()?.textContent;
    await userEvent.click(second);
    await waitFor(() =>
      expect(screen.queryAllByRole("button", { name: "Undo retirement of user_city" })).toHaveLength(0),
    );
    // A live region repeats nothing when its text does not change.
    expect(live()?.textContent?.trim()).toBe("Restored user_city");
    expect(live()?.textContent).not.toBe(once);
  });

  it("offers a retry, not an empty list, when the Auto-resolved tab's own read fails", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    let fail = true;
    vi.mocked(listAutoResolved).mockImplementation(async (_id, o) => {
      if (o?.limit !== 5 && fail) throw new Error("boom");
      return page([retired("user_city")]);
    });
    seed("ns-a", "writer");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Auto-resolved/ }, WAIT));
    const retry = await screen.findByRole("button", { name: "Retry" }, WAIT);
    expect(screen.getByText("Couldn't load retired memories")).toBeTruthy();
    expect(screen.getByText("boom")).toBeTruthy();
    expect(screen.queryByText("Nothing older within 30 days.")).toBeNull();
    fail = false;
    await userEvent.click(retry);
    expect(await screen.findByText("user_city", {}, WAIT)).toBeTruthy();
  });

  it("falls back to Conflicts when the audit log stops being readable on the Auto-resolved tab", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    vi.mocked(listAutoResolved).mockResolvedValue(page([retired("user_city")]));
    seed("ns-a", "writer");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Auto-resolved/ }, WAIT));
    await screen.findByText("user_city", {}, WAIT);
    seed("ns-a");
    expect(await screen.findByText("No conflicts", {}, WAIT)).toBeTruthy();
    expect(screen.queryByText("user_city")).toBeNull();
  });

  it("keeps apart two records of one memory at one time behind different rows", async () => {
    vi.mocked(listConflicts).mockResolvedValue({ rows: [], next_before_version: null });
    vi.mocked(listAutoResolved).mockResolvedValue(
      page([retired("user_city", { superseded_by: "id-a" }), retired("user_city", { superseded_by: "id-b" })]),
    );
    vi.mocked(restoreMemory).mockResolvedValue({ restored: true, from_status: "superseded" });
    seed("ns-a", "writer");
    render(<ReviewQueue />);
    await userEvent.click(await screen.findByRole("tab", { name: /Auto-resolved/ }, WAIT));
    const undos = await screen.findAllByRole("button", { name: "Undo retirement of user_city" }, WAIT);
    expect(undos).toHaveLength(2);
    await userEvent.click(undos[0]);
    await screen.findByText("Restored", {}, WAIT);
    expect(screen.getAllByText("Restored")).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Undo retirement of user_city" })).toHaveLength(1);
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
    expect(within(queue).getByRole("status", { name: "Loading conflicts" })).toBeInTheDocument();
  });
});
