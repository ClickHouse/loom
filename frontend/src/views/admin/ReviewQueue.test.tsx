import { act, cleanup, render, screen, waitFor } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AllowlistRow, AutoResolvedRow, ConflictRow, LearningEntry, MyNamespaceEntry, WhoAmI } from "../../api";
import { ApiError } from "../../lib/errors";
import { resetNamespaceRoleCache } from "../../lib/namespaceRole";
import { useStore } from "../../lib/state";
import { invalidateShared } from "../../lib/sharedReads";
import { axeViolations } from "../../test/axe";
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
    listAutoResolved: vi.fn(),
    listUnsuperseded: vi.fn(),
    restoreMemory: vi.fn(),
    getMemoryById: vi.fn(),
  };
});
// The page's yes/no probe is shared with the sidebar and lives in api/audit.
vi.mock("../../api/audit", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/audit")>()),
  hasAutoResolved: vi.fn(),
}));
import { hasAutoResolved } from "../../api/audit";
import {
  getMemoryById,
  listAllowlist,
  listAutoResolved,
  listConflicts,
  listLearnings,
  listUnsuperseded,
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
const panelReads = () => vi.mocked(listAutoResolved).mock.calls.filter(([, o]) => o?.limit !== 1);

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
      async (id) => (await listAutoResolved(id, { limit: 1 })).rows.length > 0,
    );
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ whoami: null, myNamespaces: null });
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
    expect(getMemoryById).toHaveBeenCalledWith(ORG, "id-elsewhere", expect.anything());
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
    expect(await screen.findByRole("button", { name: "Remove exception k/a ↔ k/b" }, WAIT)).toBeTruthy();
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
      if (o?.limit !== 1 && fail) throw new Error("boom");
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
});
