import { cleanup, render, screen, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { CodeRepoSummary } from "../api";
import { listKnowledgeSources } from "../api/knowledgeSources";
import { ApiError } from "../lib/errors";
import { buildCards } from "../lib/repositoryCard";
import type { TrackerSource, TrackerSourceNamespace } from "../lib/trackerSources";
import { axeViolations, unlabeledControls } from "../test/axe";
import {
  branchHealth,
  RepositoryCard,
  RESTRICTED_SENTENCE,
  worstHealth,
  type CodeActions,
  type RecordActionProps,
} from "./RepositoryCard";
import { must } from "../test/must";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, listKnowledge: vi.fn(async () => ({ rows: [], next_cursor: null })) };
});
vi.mock("../api/knowledgeSources", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/knowledgeSources")>();
  return { ...actual, listKnowledgeSources: vi.fn(async () => ({ sources: SOURCES, enabled: false })) };
});

// One synced document source, for the Sources facet (hoisted: the module mock reads it).
const SOURCES = vi.hoisted(() => [
  {
    name: "design-docs",
    kind: "gdrive",
    config: { folder_ids: ["1AbCdEfGhIjKlMnOp"], include: ["docs"] },
    created_by: "maintainer@example.com",
    created_at: "2026-10-01T00:00:00Z",
    sync: {
      requested_at: "",
      last_synced_at: "2026-10-02T00:00:00Z",
      status: "ok",
      documents_seen: 12,
      changed: 3,
      removed: 0,
      unconverted: 1,
      unconverted_reasons: { too_large: 1 },
      skipped: 0,
      error: "",
    },
  },
]);

// A repository is one list row: its name and the health of the worst branch the person
// can see, a muted summary line, and one inline action (a split button). Opened, it shows
// a table of its branches, the tracker line, a folded Settings for maintainers, and the
// docs. The card is pure over the model; every verb is a callback, so the tests read what
// the person reads and check which callback a click reaches.

const hoursAgo = (h: number) => new Date(Date.now() - h * 3600_000).toISOString().slice(0, 19).replace("T", " ");

function branch(over: Partial<CodeRepoSummary> = {}): CodeRepoSummary {
  return {
    org: "dev",
    namespace: "code-clickhouse",
    repo: "ClickHouse/ClickHouse",
    branch: "master",
    git_head: "abc123def456",
    clone_head: "abc123def456",
    fast_tier_lag: 0,
    scip_lag: 0,
    scip_py_lag: 0,
    indexed_at: hoursAgo(2),
    last_refresh_at: "",
    counts: { code_files: 31539, code_symbols: 400000 },
    join_policy: "open",
    my_status: "none",
    my_role: "",
    source: "config",
    ...over,
  };
}
function ns(over: Partial<TrackerSourceNamespace> = {}): TrackerSourceNamespace {
  return {
    namespace: "tracker-clickhouse",
    kind: "tracker",
    visibility: "private",
    repos: ["ClickHouse/ClickHouse"],
    classification: "standard",
    join_policy: "open",
    registered: true,
    member_role: "",
    pending_role: "",
    ...over,
  };
}
const PRIVATE = ns({ namespace: "tracker-example-private", classification: "restricted", join_policy: "approval" });
function source(over: Partial<TrackerSource> = {}): TrackerSource {
  return {
    name: "qa",
    origin: "config",
    host: "abc123xyz9.us-east-2.aws.example.com",
    port: 8443,
    database: "qa_intelligence",
    configured: true,
    contract: {
      ok: true,
      summary: "every table and column present",
      checked_at: "2026-09-16T17:27:00Z",
      optional_present: [],
      features: {},
    },
    breaker: { state: "CLOSED", consecutive_failures: 0 },
    freshness: { issues: 22583, prs: 65334, last_refresh_at: new Date(Date.now() - 3 * 60_000).toISOString() },
    namespaces: [ns(), PRIVATE],
    ...over,
  };
}

function actions() {
  const code: CodeActions = {
    onOpen: vi.fn(),
    onJoin: vi.fn(),
    onRequest: vi.fn(),
    onCancel: vi.fn(),
    onLeave: vi.fn(),
    onJoinAll: vi.fn(),
    onLeaveAll: vi.fn(),
    onManage: vi.fn(),
    onAddBranch: vi.fn(),
    onRefresh: vi.fn(),
    onRemove: vi.fn(),
  };
  const record: RecordActionProps = {
    onOpen: vi.fn(),
    onJoin: vi.fn(),
    onRequest: vi.fn(),
    onCancel: vi.fn(),
    onLeave: vi.fn(),
  };
  return { code, record };
}

/** Mounts the card opened (the page folds it by default; `open: false` mounts it as the page does). */
function mount(
  repos: CodeRepoSummary[],
  sources: TrackerSource[],
  busy: string | null = null,
  { open = true, trackerNote }: { open?: boolean; trackerNote?: string } = {},
) {
  const a = actions();
  const [card] = buildCards(repos, sources);
  const utils = render(
    <RepositoryCard
      card={card}
      busy={busy}
      code={a.code}
      record={a.record}
      defaultOpen={open}
      trackerNote={trackerNote}
    />,
  );
  return { ...utils, ...a, card };
}

/** Opens the inline action's menu (the split button's chevron). */
async function openActionMenu(user: ReturnType<typeof userEvent.setup>, repo = "ClickHouse/ClickHouse") {
  await user.click(screen.getByRole("button", { name: `More actions for ${repo}` }));
  return screen.findByRole("menu");
}

/** Opens a branch row's menu in the branch table. */
async function openBranchMenu(user: ReturnType<typeof userEvent.setup>, branchName: string) {
  const name = `Actions for ClickHouse/ClickHouse @ ${branchName}`;
  await user.click(screen.getByRole("button", { name }));
  return screen.findByRole("menu", { name });
}

describe("branchHealth: one vocabulary for an index's health", () => {
  it("names every state with its badge colour", () => {
    expect(branchHealth(branch())).toMatchObject({ text: "Up to date", state: "success" });
    expect(branchHealth(branch({ fast_tier_lag: 412 }))).toMatchObject({
      text: "Behind 412 commits",
      state: "warning",
    });
    expect(branchHealth(branch({ fast_tier_lag: 1 })).text).toBe("Behind 1 commit");
    expect(branchHealth(branch({ scip_lag: 7 })).text).toBe("Behind 7 commits");
    expect(branchHealth(branch({ indexed_at: hoursAgo(30), clone_head: "ffff00001111" }))).toMatchObject({
      text: "Stale",
      state: "warning",
    });
    // An old index whose remote has not moved is up to date.
    expect(branchHealth(branch({ indexed_at: hoursAgo(30) })).text).toBe("Up to date");
    expect(branchHealth(branch({ git_head: "", indexed_at: "", stage: "indexing" }))).toMatchObject({
      text: "Indexing",
      state: "info",
    });
    expect(branchHealth(branch({ git_head: "", indexed_at: "" }))).toMatchObject({
      text: "Not indexed",
      state: "neutral",
    });
    expect(branchHealth(branch({ stage: "error", stage_detail: '{"reason":"auth refused"}' }))).toMatchObject({
      text: "Index failed",
      state: "danger",
      detail: expect.stringContaining("auth refused"),
    });
    expect(branchHealth(branch({ searchable: false }))).toMatchObject({ text: "Not searchable", state: "neutral" });
  });

  it("the row shows the worst branch, its detail naming the branch and its heads", () => {
    const worst = must(
      worstHealth([branch(), branch({ namespace: "b", branch: "25.8", fast_tier_lag: 412 })]),
      "worst branch",
    );
    expect(worst.text).toBe("Behind 412 commits");
    expect(worst.detail).toMatch(/^25\.8: 412 merged commits not yet indexed/);
    expect(worst.detail).toContain("abc123def456");
    expect(worstHealth([])).toBeNull();
  });
});

describe("RepositoryCard: the row", () => {
  it("folded: name and health on line 1, the muted summary on line 2, the split button; the body only once opened — and passes axe", async () => {
    const user = userEvent.setup();
    const { container } = mount(
      [branch({ my_status: "member", my_role: "admin" }), branch({ namespace: "code-ch-25-8", branch: "25.8" })],
      [source()],
      null,
      { open: false },
    );
    const card = screen.getByRole("region", { name: "ClickHouse/ClickHouse" });
    const toggle = within(card).getByRole("button", { name: "ClickHouse/ClickHouse" });
    expect(within(card).getByRole("heading", { level: 2, name: "ClickHouse/ClickHouse" })).toContainElement(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(within(card).getByText("Up to date")).toBeInTheDocument();
    // A join policy is not a status: an open repository carries no badge for it.
    expect(within(card).queryByText("open", { exact: true })).toBeNull();
    expect(within(card).queryByText("Restricted")).toBeNull();
    expect(within(card).getByText("2 branches · 31,539 files · indexed 2h ago · you read 1 of 2")).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Open" })).toBeInTheDocument();
    expect(within(card).queryByRole("table")).toBeNull();
    expect(within(card).queryByRole("group", { name: "Tracker" })).toBeNull();
    expect(within(card).queryByText("Settings")).toBeNull();
    expect(unlabeledControls(container)).toEqual([]);
    expect(await axeViolations(container)).toEqual([]);

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(document.getElementById(must(toggle.getAttribute("aria-controls"), "aria-controls"))).toBeInTheDocument();
    expect(within(card).getByRole("table", { name: "ClickHouse/ClickHouse branches" })).toBeInTheDocument();
    expect(within(card).getByRole("group", { name: "Tracker" })).toBeInTheDocument();
    expect(within(card).getByText("Settings")).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);

    await user.click(toggle);
    expect(within(card).queryByRole("table")).toBeNull();
  });

  it("restricted shows a neutral Restricted badge; the health badge is the worst branch's", () => {
    mount(
      [
        branch({ join_policy: "approval", classification: "restricted" }),
        branch({ namespace: "b", branch: "25.8", fast_tier_lag: 412, classification: "restricted" }),
      ],
      [],
      null,
      { open: false },
    );
    expect(screen.getByText("Restricted")).toBeInTheDocument();
    expect(screen.getByText("Behind 412 commits")).toBeInTheDocument();
    expect(screen.queryByText("Up to date")).toBeNull();
  });

  it("no branch name is ever in a button's label", () => {
    mount(
      [branch(), branch({ namespace: "b", branch: "25.8" }), branch({ namespace: "c", branch: "25.7" })],
      [source()],
    );
    const card = screen.getByRole("region", { name: "ClickHouse/ClickHouse" });
    for (const b of within(card).getAllByRole("button")) {
      const name = b.getAttribute("aria-label") ?? b.textContent ?? "";
      if (name.startsWith("Actions for ")) continue; // a row menu is named after its row
      expect(name, b.outerHTML).not.toMatch(/\b(master|25\.8|25\.7)\b/);
    }
  });

  it("not joined: Join joins the trunk directly; its menu offers each branch, Join all, Choose branches and maintainer access", async () => {
    const user = userEvent.setup();
    const { code, card } = mount(
      [
        branch(),
        branch({ namespace: "b", branch: "25.8" }),
        branch({ namespace: "c", branch: "25.7", join_policy: "approval" }),
      ],
      [],
      null,
      { open: false },
    );
    const [master, b258, b257] = must(card.code, "code facet").branches;
    expect(screen.getByText("3 branches · 31,539 files · indexed 2h ago · not joined")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Join" }));
    expect(code.onJoin).toHaveBeenCalledWith(master);

    let menu = await openActionMenu(user);
    expect(
      within(menu)
        .getAllByRole("menuitem")
        .map((m) => m.textContent),
    ).toEqual([
      "Join master",
      "Join 25.8",
      "Request access to 25.7",
      "Join all 3 branches",
      "Choose branches…",
      "Request maintainer access",
    ]);
    await user.click(within(menu).getByRole("menuitem", { name: "Join 25.8" }));
    expect(code.onJoin).toHaveBeenLastCalledWith(b258);
    menu = await openActionMenu(user);
    await user.click(within(menu).getByRole("menuitem", { name: "Request access to 25.7" }));
    expect(code.onRequest).toHaveBeenCalledWith(b257, "viewer");
    menu = await openActionMenu(user);
    await user.click(within(menu).getByRole("menuitem", { name: "Join all 3 branches" }));
    expect(code.onJoinAll).toHaveBeenCalledWith(must(card.code, "code").branches);
    menu = await openActionMenu(user);
    await user.click(within(menu).getByRole("menuitem", { name: "Choose branches…" }));
    expect(code.onManage).toHaveBeenCalledWith(card.code);
    menu = await openActionMenu(user);
    await user.click(within(menu).getByRole("menuitem", { name: "Request maintainer access" }));
    expect(code.onRequest).toHaveBeenLastCalledWith(master, "admin");
  });

  it("the split button's menu half is a named, focusable button that opens from the keyboard", async () => {
    const user = userEvent.setup();
    mount([branch()], [], null, { open: false });
    const more = screen.getByRole("button", { name: "More actions for ClickHouse/ClickHouse" });
    more.focus();
    expect(more).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(await screen.findByRole("menu")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(more).toHaveFocus();
  });

  it("read: Open opens the branch you read; the menu opens any of them and leaves them all", async () => {
    const user = userEvent.setup();
    const { code, card } = mount(
      [
        branch({ my_status: "member" }),
        branch({ namespace: "b", branch: "25.8", my_status: "member" }),
        branch({ namespace: "c", branch: "25.7" }),
      ],
      [],
      null,
      { open: false },
    );
    const [master, b258] = must(card.code, "code facet").branches;
    expect(screen.getByText("3 branches · 31,539 files · indexed 2h ago · you read 2 of 3")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open" }));
    expect(code.onOpen).toHaveBeenCalledWith(master);
    const menu = await openActionMenu(user);
    expect(
      within(menu)
        .getAllByRole("menuitem")
        .map((m) => m.textContent),
    ).toEqual([
      "Open master",
      "Open 25.8",
      "Join 25.7",
      "Leave all 2 branches",
      "Choose branches…",
      "Request maintainer access",
    ]);
    await user.click(within(menu).getByRole("menuitem", { name: "Open 25.8" }));
    expect(code.onOpen).toHaveBeenLastCalledWith(b258);
    await openActionMenu(user);
    await user.click(screen.getByRole("menuitem", { name: "Leave all 2 branches" }));
    expect(code.onLeaveAll).toHaveBeenCalledWith(must(card.code, "code").branches);
  });

  it("everything restricted: Request access; the restricted sentence shows once", async () => {
    const user = userEvent.setup();
    const { code, card } = mount([branch({ join_policy: "approval" })], [source({ namespaces: [PRIVATE] })]);
    expect(screen.getByText("Restricted")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Request access" }));
    expect(code.onRequest).toHaveBeenCalledWith(must(card.code, "code").branches[0], "viewer");
    expect(screen.getAllByText(RESTRICTED_SENTENCE)).toHaveLength(1);
  });

  it("a pending request: a Requested badge whose menu withdraws it", async () => {
    const user = userEvent.setup();
    const { code, card } = mount([branch({ my_status: "pending", join_policy: "approval" })], [], null, {
      open: false,
    });
    expect(screen.getByText("Requested")).toBeInTheDocument();
    expect(screen.getByText(/1 branch requested$/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Withdraw/ })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Actions for ClickHouse/ClickHouse" }));
    await user.click(await screen.findByRole("menuitem", { name: "Withdraw request" }));
    expect(code.onCancel).toHaveBeenCalledWith(must(card.code, "code").branches[0]);
  });

  it("a record-only row (a tracker naming no indexed repository) summarises the record and joins it", async () => {
    const user = userEvent.setup();
    const { record, card } = mount(
      [],
      [source({ namespaces: [ns({ namespace: "tracker-widgets", repos: ["Acme/widgets"] })] })],
      null,
      { open: false },
    );
    expect(screen.getByRole("region", { name: "Acme/widgets" })).toBeInTheDocument();
    expect(screen.getByText("22,583 issues · 65,334 PRs · not joined")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Join" }));
    expect(record.onJoin).toHaveBeenCalledWith(must(card.record, "record").namespaces[0], false);
    cleanup();
    const dry = source({
      configured: false,
      breaker: { state: "unconfigured", consecutive_failures: 0 },
      freshness: {},
      namespaces: [ns({ registered: false })],
    });
    mount([branch()], [dry], null, { open: false });
    expect(screen.getByText("1 branch · 31,539 files · indexed 2h ago · not joined")).toBeInTheDocument();
  });

  it("busy disables every verb; the disclosure stays usable", () => {
    mount([branch()], [source()], "code-clickhouse");
    const card = screen.getByRole("region", { name: "ClickHouse/ClickHouse" });
    const toggle = within(card).getByRole("button", { name: "ClickHouse/ClickHouse" });
    for (const b of within(card).getAllByRole("button")) {
      if (b === toggle || b.getAttribute("aria-label")?.startsWith("Actions for ")) continue;
      expect(b.hasAttribute("disabled") || b.getAttribute("aria-disabled") === "true", b.outerHTML).toBe(true);
    }
    expect(toggle).toBeEnabled();
  });
});

describe("RepositoryCard: the branch table", () => {
  it("one row per branch: health, lag, last indexed, files, symbols and your access — and passes axe", async () => {
    const { container } = mount(
      [
        branch({ my_status: "member" }),
        branch({ namespace: "b", branch: "25.8", fast_tier_lag: 412, counts: { code_files: 30000, code_symbols: 1 } }),
        branch({ namespace: "c", branch: "25.7", my_status: "pending", join_policy: "approval" }),
        branch({ namespace: "d", branch: "click-ui", git_head: "", indexed_at: "", counts: undefined }),
      ],
      [],
    );
    const table = screen.getByRole("table", { name: "ClickHouse/ClickHouse branches" });
    const rows = within(table).getAllByRole("row").slice(1);
    // Click UI repeats the column's header inside each cell for its phone layout; read past it.
    const heads = ["Branch", "Health", "Behind", "Last indexed", "Files", "Symbols", "Access", "Actions"];
    const values = rows.map((r) =>
      within(r)
        .getAllByRole("cell")
        // ListTime may add the absolute time for screen readers; the visible value leads.
        .map((c, i) =>
          (c.textContent ?? "")
            .replace(heads[i], "")
            .replace(/ \(.* UTC\)$/, "")
            .trim(),
        ),
    );
    expect(values).toEqual([
      ["master", "Up to date", "—", "2h ago", "31,539", "400,000", "Reading", ""],
      ["25.8", "Behind 412 commits", "412", "2h ago", "30,000", "1", "—", ""],
      ["25.7", "Up to date", "—", "2h ago", "31,539", "400,000", "Requested", ""],
      ["click-ui", "Not indexed", "—", "—", "—", "—", "—", ""],
    ]);
    expect(await axeViolations(container)).toEqual([]);
  });

  it("each row's menu: Open, Refresh and Leave on a branch you read; Join on an open one; Withdraw on a request", async () => {
    const user = userEvent.setup();
    const { code, card } = mount(
      [
        branch({ my_status: "member" }),
        branch({ namespace: "b", branch: "25.8" }),
        branch({ namespace: "c", branch: "25.7", my_status: "pending", join_policy: "approval" }),
      ],
      [],
    );
    const [master, b258, b257] = must(card.code, "code").branches;
    let menu = await openBranchMenu(user, "master");
    expect(
      within(menu)
        .getAllByRole("menuitem")
        .map((m) => m.textContent),
    ).toEqual(["Open", "Refresh", "Leave"]);
    await user.click(within(menu).getByRole("menuitem", { name: "Refresh" }));
    expect(code.onRefresh).toHaveBeenCalledWith(master);
    menu = await openBranchMenu(user, "master");
    await user.click(within(menu).getByRole("menuitem", { name: "Leave" }));
    expect(code.onLeave).toHaveBeenCalledWith(master);
    menu = await openBranchMenu(user, "25.8");
    await user.click(within(menu).getByRole("menuitem", { name: "Join" }));
    expect(code.onJoin).toHaveBeenCalledWith(b258);
    menu = await openBranchMenu(user, "25.7");
    await user.click(within(menu).getByRole("menuitem", { name: "Withdraw request" }));
    expect(code.onCancel).toHaveBeenCalledWith(b257);
  });

  it("a maintainer removes a branch added from the dashboard, and adds one from the table's foot", async () => {
    const user = userEvent.setup();
    const { code, card } = mount([branch({ my_status: "member", my_role: "admin", source: "registry" })], []);
    const master = must(card.code, "code").branches[0];
    const menu = await openBranchMenu(user, "master");
    await user.click(within(menu).getByRole("menuitem", { name: "Remove branch" }));
    expect(code.onRemove).toHaveBeenCalledWith(master);
    const add = screen.getByRole("button", { name: "Add branch" });
    expect(screen.getAllByRole("button", { name: "Add branch" })).toHaveLength(1);
    await user.click(add);
    expect(code.onAddBranch).toHaveBeenCalledWith(card.code);
  });

  it("no Remove branch for a configured branch, no Add branch for a reader or where the page cannot add", async () => {
    const user = userEvent.setup();
    mount([branch({ my_status: "member", my_role: "admin" })], []);
    const menu = await openBranchMenu(user, "master");
    expect(within(menu).queryByRole("menuitem", { name: "Remove branch" })).toBeNull();
    cleanup();
    mount([branch({ my_status: "member" })], []);
    expect(screen.queryByRole("button", { name: "Add branch" })).toBeNull();
    cleanup();
    const a = actions();
    const [card] = buildCards([branch({ my_role: "admin", my_status: "member" })], []);
    render(
      <RepositoryCard
        card={card}
        busy={null}
        code={{ ...a.code, onAddBranch: undefined }}
        record={a.record}
        defaultOpen
      />,
    );
    expect(screen.queryByRole("button", { name: "Add branch" })).toBeNull();
  });
});

describe("RepositoryCard: the tracker line", () => {
  it("names the tracker, its counts and its scopes; a scope chip joins after a confirm, a restricted one requests", async () => {
    const user = userEvent.setup();
    const { record, card } = mount([branch()], [source()]);
    const rec = screen.getByRole("group", { name: "Tracker" });
    expect(within(rec).getByText(/22,583 issues · 65,334 PRs/)).toBeInTheDocument();
    expect(within(rec).getByText(/refreshed 3m ago/)).toBeInTheDocument();
    const [pub, priv] = must(card.record, "record").namespaces;
    await user.click(within(rec).getByRole("button", { name: "Public: not joined, join" }));
    expect(record.onJoin).toHaveBeenCalledWith(pub, true);
    await user.click(within(rec).getByRole("button", { name: "Private (restricted): not joined, request access" }));
    expect(record.onRequest).toHaveBeenCalledWith(priv);
    // Diagnostics stay off a reader's card.
    expect(screen.queryByText(/example\.com/)).toBeNull();
    expect(screen.queryByText("Settings")).toBeNull();
  });

  it("a read scope opens and leaves from the line", async () => {
    const user = userEvent.setup();
    const { record, card } = mount(
      [branch({ my_status: "member" })],
      [source({ namespaces: [ns({ member_role: "viewer" }), PRIVATE] })],
    );
    const tracker = must(card.record, "record").namespaces[0];
    const rec = screen.getByRole("group", { name: "Tracker" });
    await user.click(within(rec).getByRole("button", { name: "Public: reading" }));
    expect(record.onOpen).toHaveBeenCalledWith(tracker);
    await user.click(within(rec).getByRole("button", { name: "Leave · tracker" }));
    expect(record.onLeave).toHaveBeenCalledWith(tracker);
  });

  it("says Not linked when no tracker covers the repository, and says when trackers are not available here", () => {
    mount([branch()], []);
    expect(within(screen.getByRole("group", { name: "Tracker" })).getByText("Not linked")).toBeInTheDocument();
    cleanup();
    mount([branch()], [], null, { trackerNote: "Trackers are not available on this deployment yet." });
    expect(
      within(screen.getByRole("group", { name: "Tracker" })).getByText(
        "Trackers are not available on this deployment yet.",
      ),
    ).toBeInTheDocument();
  });

  it("an unconfigured record says 'no credential'; a registering scope has nothing to click", () => {
    const dry = source({
      configured: false,
      breaker: { state: "unconfigured", consecutive_failures: 0 },
      freshness: {},
      namespaces: [ns({ registered: false })],
    });
    mount([branch()], [dry]);
    expect(screen.getByText("No credential")).toBeInTheDocument();
    expect(screen.getByText("Public · registering…")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Public/ })).toBeNull();
  });
});

describe("RepositoryCard: Settings, for maintainers", () => {
  it("is folded; opened it holds the access and the details — source, host, database, origin, branch namespaces — and passes axe", async () => {
    const user = userEvent.setup();
    const a = actions();
    a.code.onSetAccess = vi.fn();
    const [card] = buildCards([branch({ my_role: "admin", my_status: "member" })], [source()]);
    const { container } = render(
      <RepositoryCard card={card} busy={null} code={a.code} record={a.record} defaultOpen />,
    );
    const settings = screen.getByRole("button", { name: "Settings" });
    expect(settings).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("button", { name: "Access" })).toBeNull();
    await user.click(settings);
    expect(settings).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: "Access" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Join policy" })).toBeInTheDocument();
    const src = screen.getByRole("group", { name: "Tracker qa" });
    expect(within(src).getByText("abc123xyz9.us-east-2.aws.example.com:8443")).toBeInTheDocument();
    expect(within(src).getByText("qa_intelligence")).toBeInTheDocument();
    expect(within(src).getByText("set up by the operator")).toBeInTheDocument();
    expect(within(src).getByText("ready — Read live; every table and column Loom needs is there.")).toBeInTheDocument();
    const codeDetails = screen.getByRole("group", { name: "Code details" });
    expect(within(codeDetails).getByText(/code-clickhouse · abc123def456/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /rotate|retire|check now/i })).toBeNull();
    expect(await axeViolations(container)).toEqual([]);
  });

  it("a reader never sees Settings or the source's host", () => {
    mount([branch({ my_status: "member" })], [source()]);
    expect(screen.queryByText("Settings")).toBeNull();
    expect(screen.queryByText(/8443/)).toBeNull();
  });

  it("a failing schema reads as such, and its admin gets a link to the Trackers list", async () => {
    const user = userEvent.setup();
    const bad = source({
      contract: {
        ok: false,
        summary: "missing tables: github_checks",
        checked_at: "2026-09-16T17:27:00Z",
        optional_present: [],
        features: {},
      },
      breaker: { state: "OPEN", consecutive_failures: 5 },
      namespaces: [ns({ member_role: "admin" })],
      origin: "registry",
    });
    const a = actions();
    const [card] = buildCards([branch()], [bad]);
    render(<RepositoryCard card={card} busy={null} code={a.code} record={a.record} adminOpsPresent defaultOpen />);
    await user.click(screen.getByText("Settings"));
    expect(
      screen.getByText("schema failing — Reads are paused until the views match: missing tables: github_checks."),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Check, rotate or retire in Trackers" })).toHaveAttribute(
      "href",
      "#tracker-qa",
    );
  });
});

// The repository's trackers as they are (`bound_trackers`, primary first), each in a line
// that says what agents get from it; Link another (trackers listing the repository
// first), Make primary, Unlink; Add a tracker when self-service is on. A maintainer's
// tracker line carries them.
describe("RepositoryCard — the tracker links", () => {
  const other = source({
    name: "kj",
    origin: "registry",
    namespaces: [ns({ namespace: "tracker-kj", repos: ["Acme/kj"], member_role: "admin" })],
  });
  const desk = source({
    name: "helpdesk",
    origin: "registry",
    namespaces: [ns({ namespace: "tracker-helpdesk", repos: ["ClickHouse/ClickHouse"], member_role: "admin" })],
  });

  function mountAdmin(bound: string | string[], { register = true } = {}) {
    const a = actions();
    a.code.onSetSource = vi.fn();
    a.code.onSetAccess = vi.fn();
    if (register) a.code.onRegisterSource = vi.fn();
    const sources = [source(), other, desk];
    a.code.trackerSources = sources;
    const row = branch({
      my_status: "member",
      my_role: "admin",
      bound_trackers: Array.isArray(bound) ? bound : bound ? [bound] : [],
    });
    const card = must(
      buildCards([row], sources).find((c) => c.repo === "ClickHouse/ClickHouse"),
      "ClickHouse card",
    );
    render(<RepositoryCard card={card} busy={null} code={a.code} record={a.record} defaultOpen />);
    return a;
  }
  const line = () => screen.getByRole("group", { name: "Tracker link" });

  it("lives in the tracker line, not in Settings", () => {
    mountAdmin("tracker-clickhouse");
    expect(screen.getByRole("group", { name: "Tracker" })).toContainElement(line());
    cleanup();
  });

  it("one link: says it with its counts; Link another offers the trackers listing the repository first and appends", async () => {
    const a = mountAdmin("tracker-clickhouse");
    expect(line()).toHaveTextContent(
      "Linked to qa · public — 22,583 issues · 65,334 PRs. Agents in this repository's checkout see them.",
    );
    expect(within(line()).queryByRole("button", { name: /Make .* primary/ })).toBeNull();
    await userEvent.click(within(line()).getByRole("button", { name: "Link another tracker" }));
    const select = screen.getByRole("button", { name: "Tracker" });
    expect(select).toHaveTextContent("qa · private");
    expect(screen.getByText(/stays primary/)).toBeInTheDocument();
    await userEvent.click(select);
    expect(Array.from(document.querySelectorAll("[cui-select-group-name]")).map((g) => g.textContent)).toEqual([
      "Trackers listing ClickHouse/ClickHouse",
      "Other trackers — they answer nothing for ClickHouse/ClickHouse",
    ]);
    await userEvent.click(screen.getByText("kj"));
    expect(
      screen.getByText("kj lists Acme/kj, not ClickHouse/ClickHouse: it would answer nothing here."),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Link" }));
    expect(a.code.onSetSource).toHaveBeenCalledWith(expect.objectContaining({ repo: "ClickHouse/ClickHouse" }), [
      "tracker-clickhouse",
      "tracker-kj",
    ]);
    expect(await axeViolations(document.body)).toEqual([]);
    cleanup();
  });

  it("several links: the primary is marked and said; Make primary reorders, Unlink removes", async () => {
    const a = mountAdmin(["tracker-clickhouse", "tracker-helpdesk"]);
    const items = within(screen.getByRole("list", { name: "Linked trackers" })).getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(within(items[0]).getByText("Primary", { exact: true })).toBeInTheDocument();
    expect(within(items[1]).queryByText("Primary", { exact: true })).toBeNull();
    expect(line()).toHaveTextContent("Agents search all 2; a lookup by number (#123, a PR's facts) reads qa · public");
    await userEvent.click(screen.getByRole("button", { name: "Make helpdesk primary" }));
    expect(a.code.onSetSource).toHaveBeenLastCalledWith(expect.anything(), ["tracker-helpdesk", "tracker-clickhouse"]);
    await userEvent.click(screen.getByRole("button", { name: "Unlink qa · public" }));
    expect(a.code.onSetSource).toHaveBeenLastCalledWith(expect.anything(), ["tracker-helpdesk"]);
    expect(unlabeledControls(document.body)).toEqual([]);
    expect(await axeViolations(document.body)).toEqual([]);
    cleanup();
  });

  it("warns inline when a link answers nothing, and says when there is none", () => {
    mountAdmin("tracker-kj");
    expect(line()).toHaveTextContent(
      "Linked to kj, but it lists Acme/kj, not ClickHouse/ClickHouse — agents get nothing from it here.",
    );
    cleanup();
    mountAdmin("");
    expect(line()).toHaveTextContent("Not linked. Agents in this repository's checkout see no issues or PRs.");
    expect(within(line()).getByRole("button", { name: "Link a tracker" })).toBeInTheDocument();
    cleanup();
    mountAdmin("tracker-gone");
    expect(line()).toHaveTextContent(
      "Linked to tracker-gone, which is not in the tracker list — retired, or private to you.",
    );
    cleanup();
  });

  it("offers Add a tracker only when self-service is on", async () => {
    const a = mountAdmin("tracker-clickhouse");
    await userEvent.click(screen.getByRole("button", { name: /Add a tracker/ }));
    expect(a.code.onRegisterSource).toHaveBeenCalled();
    cleanup();
    mountAdmin("tracker-clickhouse", { register: false });
    expect(screen.queryByRole("button", { name: /Add a tracker/ })).toBeNull();
    cleanup();
  });
});

// The Docs facet reads the docs namespace the rows name, and offers Add to the maintainer
// of that branch only.
describe("RepositoryCard — docs", () => {
  it("shows the Docs facet for the trunk's docs namespace; Add only for its maintainer", async () => {
    const rows = [
      branch({ my_status: "member", my_role: "", docs_namespace: "code-clickhouse" }),
      branch({
        namespace: "code-ch-25-8",
        branch: "25.8",
        my_status: "member",
        my_role: "admin",
        docs_namespace: "code-clickhouse",
      }),
    ];
    mount(rows, []);
    expect(screen.getByRole("group", { name: "Docs" })).toBeInTheDocument();
    expect(await screen.findByText(/No documents yet\. A maintainer adds them\./)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add a document" })).toBeNull();
    cleanup();
    mount([branch({ my_status: "member", my_role: "admin", docs_namespace: "code-clickhouse" })], []);
    expect(await screen.findByRole("button", { name: "Add a document" })).toBeInTheDocument();
    cleanup();
    mount([branch({ my_status: "member" })], []); // no docs namespace: no Docs facet
    expect(screen.queryByRole("group", { name: "Docs" })).toBeNull();
    cleanup();
  });
});

// The Sources facet follows Docs on the same namespace: a reader sees each source and its
// last sync, and only that namespace's admin gets the verbs.
describe("RepositoryCard — sources", () => {
  it("lists the sources and their last sync to a reader, with no verb", async () => {
    mount([branch({ my_status: "member", my_role: "viewer", docs_namespace: "code-clickhouse" })], []);
    const group = screen.getByRole("group", { name: "Sources" });
    expect(await within(group).findByText("design-docs")).toBeInTheDocument();
    expect(within(group).getByText("Synced")).toBeInTheDocument();
    expect(within(group).getByText("12 documents listed · 3 written · 1 not converted")).toBeInTheDocument();
    expect(within(group).getByText(/Sync is off on this deployment/)).toBeInTheDocument();
    expect(within(group).queryByRole("button", { name: "Add a source" })).toBeNull();
    expect(within(group).queryByRole("button", { name: "Actions for design-docs" })).toBeNull();
    cleanup();
    mount([branch({ my_status: "member" })], []); // no docs namespace: no Sources facet
    expect(screen.queryByRole("group", { name: "Sources" })).toBeNull();
  });

  it("says so when the read is refused or fails, and shows a failed turn's error", async () => {
    const reader = () =>
      mount([branch({ my_status: "member", my_role: "viewer", docs_namespace: "code-clickhouse" })], []);
    const group = () => screen.getByRole("group", { name: "Sources" });
    vi.mocked(listKnowledgeSources).mockRejectedValueOnce(new ApiError({ status: 403, code: "AUTH_INSUFFICIENT" }));
    reader();
    expect(
      await within(group()).findByText(/Join the repository to see where its documents come from/),
    ).toBeInTheDocument();
    cleanup();
    vi.mocked(listKnowledgeSources).mockRejectedValueOnce(new ApiError({ status: 500, message: "boom" }));
    reader();
    expect(await within(group()).findByText(/Couldn't list the document sources/)).toHaveAttribute("role", "status");
    cleanup();
    const failed = { ...SOURCES[0], sync: { ...SOURCES[0].sync, status: "failed", error: "http_403" } };
    vi.mocked(listKnowledgeSources).mockResolvedValueOnce({ sources: [failed], enabled: true });
    reader();
    expect(await within(group()).findByText("Sync failed")).toBeInTheDocument();
    expect(within(group()).getByText("http_403")).toBeInTheDocument();
    expect(within(group()).queryByText(/Sync is off/)).toBeNull();
  });

  it("gives the namespace's admin Add a source and each source's verbs", async () => {
    const user = userEvent.setup();
    mount([branch({ my_status: "member", my_role: "admin", docs_namespace: "code-clickhouse" })], []);
    const group = screen.getByRole("group", { name: "Sources" });
    expect(await within(group).findByRole("button", { name: "Add a source" })).toBeInTheDocument();
    await user.click(within(group).getByRole("button", { name: "Actions for design-docs" }));
    const menu = await screen.findByRole("menu");
    expect(
      within(menu)
        .getAllByRole("menuitem")
        .map((m) => m.textContent),
    ).toEqual(["Check", "Sync now", "Rotate credential", "Retire"]);
  });
});

describe("RepositoryCard while a write is out", () => {
  it("disables every branch row's menu item, so a second write waits for the first", async () => {
    const user = userEvent.setup();
    mount(
      [
        branch({ namespace: "a", branch: "master", my_status: "member", my_role: "viewer" }),
        branch({ namespace: "b", branch: "25.8", my_role: "", join_policy: "open" }),
      ],
      [],
      "a",
    );
    let menu = await openBranchMenu(user, "master");
    const member = within(menu).getAllByRole("menuitem");
    expect(member.map((m) => m.textContent)).toEqual(expect.arrayContaining(["Open", "Leave"]));
    for (const item of member) expect(item).toHaveAttribute("aria-disabled", "true");
    await user.keyboard("{Escape}");
    menu = await openBranchMenu(user, "25.8");
    for (const item of within(menu).getAllByRole("menuitem")) expect(item).toHaveAttribute("aria-disabled", "true");
  });

  it("disables the Withdraw request item behind a pending badge", async () => {
    const user = userEvent.setup();
    mount([branch({ namespace: "a", branch: "master", my_status: "pending" })], [], "a", { open: false });
    await user.click(screen.getByRole("button", { name: "Actions for ClickHouse/ClickHouse" }));
    const menu = await screen.findByRole("menu");
    const withdraw = within(menu).getByRole("menuitem", { name: "Withdraw request" });
    expect(withdraw).toHaveAttribute("aria-disabled", "true");
  });
});
