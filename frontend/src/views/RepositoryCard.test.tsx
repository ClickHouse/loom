import { cleanup, render, screen, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { CodeRepoSummary } from "../api";
import { buildCards } from "../lib/repositoryCard";
import type { TrackerSource, TrackerSourceNamespace } from "../lib/trackerSources";
import { axeViolations, unlabeledControls } from "../test/axe";
import { RepositoryCard, RESTRICTED_SENTENCE, type CodeActions, type RecordActionProps } from "./RepositoryCard";
import { must } from "../test/must";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, listKnowledge: vi.fn(async () => ({ rows: [], next_cursor: null })) };
});

// A repository is one card with two facets. The card is pure over the model; every verb
// is a callback, so the tests read what the person reads and check which callback a click
// reaches.

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
    indexed_at: new Date(Date.now() - 2 * 3600_000).toISOString().slice(0, 19).replace("T", " "),
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
function mount(repos: CodeRepoSummary[], sources: TrackerSource[], busy: string | null = null, { open = true } = {}) {
  const a = actions();
  const [card] = buildCards(repos, sources);
  const utils = render(<RepositoryCard card={card} busy={busy} code={a.code} record={a.record} defaultOpen={open} />);
  return { ...utils, ...a, card };
}

/** Every button except the card's own disclosure (its heading). */
const verbs = () => screen.getAllByRole("button").filter((b) => !b.hasAttribute("aria-expanded"));

describe("RepositoryCard", () => {
  it("folded by default: the heading is the disclosure; the row says the standing and a one-line summary, keeps the primary verb, and shows no facet, Leave, or Details until opened — and passes axe", async () => {
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
    expect(within(card).getByText("you read 1 of 2 branches · tracker: not joined")).toBeInTheDocument();
    expect(within(card).getByText("2 branches · 31,539 files · 22,583 issues · 65,334 PRs")).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Open · master" })).toBeInTheDocument();
    expect(within(card).queryByRole("group", { name: "Code" })).toBeNull();
    expect(within(card).queryByRole("group", { name: "Tracker" })).toBeNull();
    expect(within(card).queryByRole("button", { name: /^Leave/ })).toBeNull();
    expect(within(card).queryByText("Details")).toBeNull();
    expect(await axeViolations(container)).toEqual([]);

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveAttribute("aria-controls");
    expect(document.getElementById(must(toggle.getAttribute("aria-controls"), "aria-controls"))).toBeInTheDocument();
    // Open: the summary gives way to the facet rows, and the standing stays.
    expect(within(card).queryByText("2 branches · 31,539 files · 22,583 issues · 65,334 PRs")).toBeNull();
    expect(within(card).getByText("you read 1 of 2 branches · tracker: not joined")).toBeInTheDocument();
    expect(within(card).getByRole("group", { name: "Code" })).toBeInTheDocument();
    expect(within(card).getByRole("group", { name: "Tracker" })).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Leave · master" })).toBeInTheDocument();
    expect(within(card).getByText("Details")).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(within(card).queryByRole("group", { name: "Code" })).toBeNull();
  });

  it("a record-only folded row summarises the record; an unconfigured one says so", () => {
    mount([], [source({ namespaces: [ns({ namespace: "tracker-widgets", repos: ["Acme/widgets"] })] })], null, {
      open: false,
    });
    expect(screen.getByText("22,583 issues · 65,334 PRs")).toBeInTheDocument();
    cleanup();
    const dry = source({
      configured: false,
      breaker: { state: "unconfigured", consecutive_failures: 0 },
      freshness: {},
      namespaces: [ns({ registered: false })],
    });
    mount([branch()], [dry], null, { open: false });
    expect(screen.getByText("1 branch · 31,539 files · tracker: no credential")).toBeInTheDocument();
  });

  it("one card: header, standing phrase, both facets, chips named by branch and by scope — and passes axe", async () => {
    const { container } = mount([branch()], [source()]);
    const card = screen.getByRole("region", { name: "ClickHouse/ClickHouse" });
    expect(within(card).getByRole("heading", { level: 2, name: "ClickHouse/ClickHouse" })).toBeInTheDocument();
    expect(within(card).getByText("open")).toBeInTheDocument();
    expect(within(card).getByText("not joined")).toBeInTheDocument();
    // No per-card "read-only", no "tracker sources" heading, no source slug outside the disclosure.
    expect(within(card).queryByText("read-only")).toBeNull();
    expect(within(card).queryByText(/tracker source/)).toBeNull();
    expect(within(card).queryByText(/tracker-clickhouse/)).toBeNull();
    // Facet rows.
    const code = within(card).getByRole("group", { name: "Code" });
    expect(within(code).getByText(/1 branch/)).toBeInTheDocument();
    expect(within(code).getByText(/31,539 files · 400,000 symbols at master/)).toBeInTheDocument();
    expect(within(code).getByText(/indexed 2h ago/)).toBeInTheDocument();
    expect(within(code).getByRole("button", { name: "master: not joined, join" })).toBeInTheDocument();
    const rec = within(card).getByRole("group", { name: "Tracker" });
    expect(within(rec).getByText(/22,583 issues · 65,334 PRs/)).toBeInTheDocument();
    expect(within(rec).getByText(/refreshed 3m ago/)).toBeInTheDocument();
    expect(within(rec).getByRole("button", { name: "Public: not joined, join" })).toBeInTheDocument();
    expect(
      within(rec).getByRole("button", { name: "Private (restricted): not joined, request access" }),
    ).toBeInTheDocument();
    // Diagnostics are not on the card for a reader.
    expect(within(card).queryByText(/example\.com/)).toBeNull();
    expect(within(card).queryByText(/contract/)).toBeNull();
    expect(within(card).queryByText("Details")).toBeNull();
    // One primary button: Join the open branch.
    expect(within(card).getByRole("button", { name: "Join · master" })).toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: /^Leave/ })).toBeNull();
    expect(await axeViolations(container)).toEqual([]);
  });

  it("the header verb follows the first facet the person cannot read: a restricted code facet makes it 'Join · tracker', which calls the record's join once and directly", async () => {
    const user = userEvent.setup();
    const { record, code, card } = mount([branch({ join_policy: "approval" })], [source()]);
    const tracker = must(card.record, "record facet").namespaces[0];
    const master = must(card.code, "code facet").branches[0];
    await user.click(screen.getByRole("button", { name: "Join · tracker" }));
    expect(record.onJoin).toHaveBeenCalledTimes(1);
    expect(record.onJoin).toHaveBeenCalledWith(tracker, false);
    expect(code.onJoin).not.toHaveBeenCalled();
    // A bare click on the open record chip confirms first (`confirm: true`); only the header joins directly.
    await user.click(screen.getByRole("button", { name: "Public: not joined, join" }));
    expect(record.onJoin).toHaveBeenLastCalledWith(tracker, true);
    // The restricted branch chip goes to the request dialog, never joins by itself.
    await user.click(screen.getByRole("button", { name: "master: not joined, request access" }));
    expect(code.onRequest).toHaveBeenCalledWith(master, "viewer");
    expect(code.onJoin).not.toHaveBeenCalled();
  });

  it("everything restricted: 'Request access · master', the private record chip requests, the 6820 sentence shows once", async () => {
    const user = userEvent.setup();
    const { record, card } = mount([branch({ join_policy: "approval" })], [source({ namespaces: [PRIVATE] })]);
    const tracker = must(card.record, "record facet").namespaces[0];
    expect(screen.getByText("restricted")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Request access · master" })).toBeInTheDocument();
    expect(screen.getAllByText(RESTRICTED_SENTENCE)).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Private (restricted): not joined, request access" }));
    expect(record.onRequest).toHaveBeenCalledWith(tracker);
  });

  it("a read branch: 'Open · master', a quiet 'Leave · master', the chip opens, the standing phrase counts", async () => {
    const user = userEvent.setup();
    const { code, card } = mount(
      [branch({ my_status: "member" }), branch({ namespace: "code-ch-25-8", branch: "25.8" })],
      [source()],
    );
    const master = must(card.code, "code facet").branches[0];
    expect(screen.getByText("you read 1 of 2 branches · tracker: not joined")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open · master" }));
    expect(code.onOpen).toHaveBeenCalledWith(master);
    await user.click(screen.getByRole("button", { name: "Leave · master" }));
    expect(code.onLeave).toHaveBeenCalledWith(master);
    await user.click(screen.getByRole("button", { name: "master: reading" }));
    expect(code.onOpen).toHaveBeenCalledTimes(2);
    // The branch verbs stay on a partly joined card: Choose branches…, Request maintainer access.
    await user.click(screen.getByRole("button", { name: "Choose branches…" }));
    expect(code.onManage).toHaveBeenCalledWith(card.code);
    await user.click(screen.getByRole("button", { name: "Request maintainer access" }));
    expect(code.onRequest).toHaveBeenCalledWith(master, "admin");
    expect(screen.queryByRole("button", { name: /Ask to maintain/ })).toBeNull();
  });

  it("a read record and a read branch: no header Leave; each facet carries its own; the record's Open reaches the record", async () => {
    const user = userEvent.setup();
    const { record, code, card } = mount(
      [branch({ my_status: "member" })],
      [source({ namespaces: [ns({ member_role: "viewer" }), PRIVATE] })],
    );
    const tracker = must(card.record, "record facet").namespaces[0];
    expect(screen.getByText("you read 1 of 1 branch · tracker: reading")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open · master" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Leave · master" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Leave · tracker" }));
    expect(record.onLeave).toHaveBeenCalledWith(tracker);
    await user.click(screen.getByRole("button", { name: "Public: reading" }));
    expect(record.onOpen).toHaveBeenCalledWith(tracker);
    expect(code.onOpen).not.toHaveBeenCalled();
  });

  it("a pending request: 'Withdraw request · master' and the chip withdraws", async () => {
    const user = userEvent.setup();
    const { code, card } = mount([branch({ my_status: "pending", join_policy: "approval" })], []);
    const master = must(card.code, "code facet").branches[0];
    expect(screen.getByText("1 branch requested")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Withdraw request · master" }));
    expect(code.onCancel).toHaveBeenCalledWith(master);
    await user.click(screen.getByRole("button", { name: "master: requested" }));
    expect(code.onCancel).toHaveBeenCalledTimes(2);
  });

  it("ten branches: the trunk and up to four chips show, 'Show 6 more' reveals the rest; Join all and Leave all stay", async () => {
    const user = userEvent.setup();
    const rows = Array.from({ length: 10 }, (_, i) =>
      branch({ namespace: `c${i}`, branch: i === 0 ? "master" : `25.${i}`, my_status: i < 2 ? "member" : "none" }),
    );
    const { code } = mount(rows, []);
    const list = screen.getByRole("list", { name: "ClickHouse/ClickHouse branches" });
    expect(list.querySelectorAll("li")).toHaveLength(4);
    await user.click(screen.getByRole("button", { name: "Show 6 more" }));
    expect(list.querySelectorAll("li")).toHaveLength(10);
    expect(screen.getByRole("button", { name: "Show fewer" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Join all 8 branches" }));
    expect(code.onJoinAll).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "Leave all 2 branches" }));
    expect(code.onLeaveAll).toHaveBeenCalledTimes(1);
  });

  it("a record-only card (a tracker namespace naming no configured repository) is headed by the repository it declares", () => {
    mount([], [source({ namespaces: [ns({ namespace: "tracker-widgets", repos: ["Acme/widgets"] })] })]);
    expect(screen.getByRole("region", { name: "Acme/widgets" })).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Code" })).toBeNull();
    expect(screen.getByRole("group", { name: "Tracker" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Join · tracker" })).toBeInTheDocument();
  });

  it("an unconfigured record says 'no credential'; a registering namespace has nothing to click", () => {
    const dry = source({
      configured: false,
      breaker: { state: "unconfigured", consecutive_failures: 0 },
      freshness: {},
      namespaces: [ns({ registered: false })],
    });
    mount([branch()], [dry]);
    expect(screen.getByText("no credential")).toBeInTheDocument();
    expect(screen.getByText("Public · registering…")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Public/ })).toBeNull();
  });

  it("the Details disclosure renders only for a maintainer: source, host, database, origin, contract, breaker, branch namespaces — and passes axe", async () => {
    const user = userEvent.setup();
    const { container } = mount([branch({ my_role: "admin", my_status: "member" })], [source()]);
    const details = screen.getByText("Details");
    expect(details).toBeInTheDocument();
    await user.click(details);
    const src = screen.getByRole("group", { name: "Tracker qa" });
    expect(within(src).getByText("qa")).toBeInTheDocument();
    expect(within(src).getByText("abc123xyz9.us-east-2.aws.example.com:8443")).toBeInTheDocument();
    expect(within(src).getByText("qa_intelligence")).toBeInTheDocument();
    expect(within(src).getByText("set up by the operator")).toBeInTheDocument();
    // One state line in plain words, not a contract summary and a breaker
    expect(within(src).getByText("ready — Read live; every table and column Loom needs is there.")).toBeInTheDocument();
    const codeDetails = screen.getByRole("group", { name: "Code details" });
    expect(within(codeDetails).getByText(/code-clickhouse · abc123def456/)).toBeInTheDocument();
    // Add branch lives in the Code row, not a second copy in Details.
    expect(within(codeDetails).queryByRole("button", { name: "Add branch" })).toBeNull();
    // Admin verbs live in the Trackers list; an operator's tracker links nowhere.
    expect(screen.queryByRole("button", { name: /rotate|retire|check now/i })).toBeNull();
    expect(screen.queryByRole("link", { name: /Check, rotate or retire/ })).toBeNull();
    expect(await axeViolations(container)).toEqual([]);
  });

  it("a maintainer's Add branch sits in the Code row beside the branch chips, without opening Details", async () => {
    const user = userEvent.setup();
    const { code, card } = mount([branch({ my_role: "admin", my_status: "member" })], [source()]);
    const row = screen.getByRole("group", { name: "Code" });
    const add = within(row).getByRole("button", { name: "Add branch" });
    expect(screen.getAllByRole("button", { name: "Add branch" })).toHaveLength(1);
    expect(screen.getByText("Details").closest("details")).not.toHaveAttribute("open");
    await user.click(add);
    expect(code.onAddBranch).toHaveBeenCalledWith(card.code);
  });

  it("no Add branch where the deployment cannot add branches from the page", () => {
    const a = actions();
    const [card] = buildCards([branch({ my_role: "admin", my_status: "member" })], [source()]);
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

  it("a reader never sees Details, Add branch, or the source's host", () => {
    mount([branch({ my_status: "member" })], [source()]);
    expect(screen.queryByText("Details")).toBeNull();
    expect(screen.queryByRole("button", { name: "Add branch" })).toBeNull();
    expect(screen.queryByText(/8443/)).toBeNull();
  });

  it("a failing schema reads as such in the disclosure, and its admin gets a link to the Trackers list", async () => {
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
    await user.click(screen.getByText("Details"));
    expect(
      screen.getByText("schema failing — Reads are paused until the views match: missing tables: github_checks."),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Check, rotate or retire in Trackers ↓" })).toHaveAttribute(
      "href",
      "#tracker-qa",
    );
  });

  it("busy disables every verb and chip", () => {
    mount([branch()], [source()], "code-clickhouse");
    // A chip with a tip is aria-disabled rather than disabled, so its tip stays reachable.
    for (const b of verbs())
      expect(b.hasAttribute("disabled") || b.getAttribute("aria-disabled") === "true", b.outerHTML).toBe(true);
    // The disclosure itself stays usable: folding is not a write.
    expect(screen.getByRole("button", { name: "ClickHouse/ClickHouse" })).toBeEnabled();
  });
});

// The repository's trackers as they are (`bound_trackers`, primary first), each in a line
// that says what agents get from it; Link another (trackers listing the repository
// first), Make primary, Unlink; Add a tracker when self-service is on.
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

  it("one link: says it with its counts; Link another offers the trackers listing the repository first and appends", async () => {
    const a = mountAdmin("tracker-clickhouse");
    expect(line()).toHaveTextContent(
      "Linked to qa · public — 22,583 issues · 65,334 PRs. Agents in this repository's checkout see them.",
    );
    expect(within(line()).queryByRole("button", { name: /Make .* primary/ })).toBeNull();
    await userEvent.click(within(line()).getByRole("button", { name: "Link another tracker" }));
    const select = screen.getByRole("button", { name: "Tracker" });
    expect(select).toHaveTextContent("qa · private"); // the first tracker listing the repository
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
    expect(within(items[0]).getByText("primary", { exact: true })).toBeInTheDocument();
    expect(within(items[1]).queryByText("primary", { exact: true })).toBeNull();
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

  it("the page offers Add a tracker only when self-service is on", async () => {
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
