/**
 * Adding a tracker connects (a pasted console URL fills the address), then maps the
 * team's own tables onto what Loom reads (Loom proposes, the person confirms; a database
 * with Loom's views needs no mapping), then checks — `tracker.source.add(dry_run)`,
 * nothing written — with each result beside the check it answers. Add sends exactly the
 * form that passed. The Trackers list says each tracker's state in plain words and gives
 * its admin Check / Rotate / Retire.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../lib/errors";
import {
  bindableRecords,
  contractRows,
  defaultNamespaceFor,
  parseClickHouseUrl,
  sourceAddBody,
  sourceFormProblems,
  templatesFile,
  trackerStatus,
  type InspectedTable,
  type SourceMapping,
  type TrackerContractCatalogue,
  type TrackerInspectResult,
  type TrackerSource,
  type TrackerSourceAddResult,
  type TrackerSourceForm,
} from "../lib/trackerSources";
import { axeViolations, unlabeledControls } from "../test/axe";
import { proposeName, RegisterTrackerSourceDialog, RetireSourceDialog } from "./TrackerSourceDialogs";
import { TrackersPanel } from "../views/TrackersPanel";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    addTrackerSource: vi.fn(),
    inspectTrackerSource: vi.fn(),
    removeTrackerSource: vi.fn(),
    rotateTrackerSource: vi.fn(),
    checkTrackerSource: vi.fn(),
    getTrackerContract: vi.fn(),
  };
});
vi.mock("../components/Toast", () => ({ toast: vi.fn() }));
import {
  addTrackerSource,
  checkTrackerSource,
  getTrackerContract,
  inspectTrackerSource,
  removeTrackerSource,
} from "../api";

const FORM: TrackerSourceForm = {
  name: "kj",
  host: "abc.clickhouse.cloud",
  port: 8443,
  secure: true,
  database: "loom_tracker",
  user: "loom_ro",
  password: "pw",
  namespace: "tracker-kj",
  repos: ["Acme/kj"],
  restricted: false,
  join_policy: "open",
};

const CATALOGUE: TrackerContractCatalogue = {
  database: "loom_tracker",
  tables: [
    {
      table: "github_issues",
      required: true,
      note: "one row per issue",
      columns: [
        { name: "source_id", family: "String", type: "String", must_map: true, note: "'Org/repo#number'" },
        { name: "labels", family: "Array(String)", type: "Array(String)", must_map: false, note: "the item's labels" },
      ],
    },
    {
      table: "github_prs",
      required: true,
      note: "one row per pull request",
      columns: [{ name: "pr_number", family: "UInt", type: "UInt64", must_map: true, note: "the PR number" }],
    },
    {
      table: "edges",
      required: true,
      note: "the links between items",
      columns: [{ name: "rel_type", family: "String", type: "String", must_map: true, note: "fixes …" }],
    },
    {
      table: "core_incidents",
      required: false,
      note: "incidents",
      columns: [{ name: "title", family: "String", type: "String", must_map: true, note: "" }],
    },
  ],
  templates: {
    github_issues:
      "CREATE OR REPLACE VIEW loom_tracker.github_issues AS\nSELECT\n    '' AS source_id\nFROM <your_table>;",
    github_prs: "-- prs",
    edges: "-- edges",
    core_incidents: "-- inc",
  },
};

function result(over: Partial<TrackerSourceAddResult> = {}): TrackerSourceAddResult {
  return {
    name: "kj",
    host: "abc.clickhouse.cloud",
    database: "loom_tracker",
    namespaces: [{ namespace: "tracker-kj", repos: ["Acme/kj"], classification: "standard", join_policy: "open" }],
    contract: {
      ok: true,
      summary: "ok",
      database: "loom_tracker",
      missing_tables: [],
      missing_columns: {},
      optional_present: ["core_incidents"],
    },
    roles: [],
    my_role: "admin",
    dry_run: true,
    written: false,
    view_templates: {},
    ...over,
  };
}
const FAILING = result({
  contract: {
    ok: false,
    summary: "missing tables edges",
    database: "loom_tracker",
    missing_tables: ["edges"],
    missing_columns: { github_prs: ["pr_number"] },
    type_mismatches: { github_prs: { additions: ["UInt", "String"] } },
    aliased: { github_issues: { severity_level: "severity" } },
    optional_present: [],
  },
  view_templates: {
    github_prs:
      "CREATE OR REPLACE VIEW loom_tracker.github_prs AS\nSELECT\n    toUInt64(0) AS pr_number,  -- MUST map\n    id\nFROM <your_table>;",
    edges: "-- edges",
  },
});

function src(over: Partial<TrackerSource> = {}): TrackerSource {
  return {
    name: "kj",
    host: "abc.clickhouse.cloud",
    port: 8443,
    database: "loom_tracker",
    configured: true,
    origin: "registry",
    contract: { ok: true, summary: "ok", checked_at: "", optional_present: [], features: {} },
    breaker: { state: "CLOSED", consecutive_failures: 0 },
    freshness: { issues: 12, prs: 30, last_refresh_at: "" },
    namespaces: [
      {
        namespace: "tracker-kj",
        kind: "tracker",
        visibility: "private",
        repos: ["Acme/kj"],
        classification: "standard",
        join_policy: "open",
        registered: true,
        member_role: "admin",
        pending_role: "",
      },
    ],
    ...over,
  };
}

describe("the pure part", () => {
  it("reads a pasted ClickHouse address, whatever shape the console gave", () => {
    expect(parseClickHouseUrl("https://abc.us-east-1.aws.clickhouse.cloud:8443/?database=loom_tracker")).toEqual({
      host: "abc.us-east-1.aws.clickhouse.cloud",
      port: 8443,
      secure: true,
      database: "loom_tracker",
    });
    expect(parseClickHouseUrl("abc.clickhouse.cloud")).toEqual({
      host: "abc.clickhouse.cloud",
      port: 8443,
      secure: true,
    });
    expect(parseClickHouseUrl("http://ch.internal:8123/tracker")).toEqual({
      host: "ch.internal",
      port: 8123,
      secure: false,
      database: "tracker",
    });
    expect(parseClickHouseUrl("clickhouse://loom_ro@ch.example:9440")).toEqual({
      host: "ch.example",
      port: 9440,
      secure: true,
      user: "loom_ro",
    });
    expect(parseClickHouseUrl("")).toBeNull();
    expect(parseClickHouseUrl("not a host at all !")).toBeNull();
  });

  it("says a tracker's state in words that say what to do", () => {
    expect(trackerStatus(src())).toMatchObject({ tone: "ok", label: "ready" });
    expect(
      trackerStatus(
        src({
          contract: { ok: false, summary: "missing tables edges", checked_at: "", optional_present: [], features: {} },
        }),
      ),
    ).toEqual({
      tone: "danger",
      label: "schema failing",
      detail: "Reads are paused until the views match: missing tables edges.",
    });
    expect(trackerStatus(src({ breaker: { state: "OPEN", consecutive_failures: 5 } }))).toMatchObject({
      label: "paused",
    });
    expect(trackerStatus(src({ configured: false }))).toMatchObject({ label: "no credential" });
    expect(
      trackerStatus(src({ contract: { ok: null, summary: "", checked_at: "", optional_present: [], features: {} } })),
    ).toMatchObject({ label: "not checked" });
  });

  it("proposes a free name from the first repository, and validates like the server", () => {
    expect(proposeName(["Acme/kj"], new Set())).toBe("kj");
    expect(proposeName(["Acme/kj"], new Set(["kj", "kj-2"]))).toBe("kj-3");
    expect(proposeName([], new Set())).toBe("");
    expect(defaultNamespaceFor("kj_team")).toBe("tracker-kj-team");
    expect(sourceFormProblems(FORM)).toEqual([]);
    expect(sourceFormProblems({ ...FORM, repos: [] })).toContain(
      "Name at least one repository: a tracker reads only the repositories it lists.",
    );
    expect(sourceAddBody("dev", FORM, true)).toMatchObject({
      dry_run: true,
      namespaces: [{ namespace: "tracker-kj", repos: ["Acme/kj"], join_policy: "open" }],
    });
  });

  it("reads the check table by table, and writes one .sql with what to do on top", () => {
    expect(contractRows(FAILING.contract).map((r) => [r.table, r.state])).toEqual([
      ["github_issues", "aliased"],
      ["github_prs", "incomplete"],
      ["edges", "missing"],
    ]);
    const file = templatesFile(CATALOGUE, ["github_issues", "edges"]);
    expect(file.split("\n")[0]).toBe(
      "-- Views Loom reads, in loom_tracker. Replace <your_table> with the table each record lives in,",
    );
    expect(file).toContain("CREATE OR REPLACE VIEW loom_tracker.github_issues AS");
    expect(file).toContain("-- edges");
    expect(file).not.toContain("-- prs");
  });

  it("labels a record by its tracker and scope, those listing the repository first", () => {
    const two = src({
      name: "qa",
      namespaces: [
        { ...src().namespaces[0], namespace: "tracker-b", repos: ["Acme/other"] },
        { ...src().namespaces[0], namespace: "tracker-a", repos: ["Acme/kj"], classification: "restricted" },
      ],
    });
    expect(bindableRecords([two], "Acme/kj").map((r) => [r.label, r.covers])).toEqual([
      ["qa · private", true],
      ["qa · public", false],
    ]);
  });
});

function connect() {
  fireEvent.change(screen.getByLabelText(/^ClickHouse address/), {
    target: { value: "https://abc.clickhouse.cloud:8443/?database=helpdesk" },
  });
  fireEvent.change(screen.getByLabelText(/^Read-only user/), { target: { value: "loom_ro" } });
  fireEvent.change(screen.getByLabelText(/^Password/), { target: { value: "pw" } });
}
async function toMap() {
  connect();
  fireEvent.click(screen.getByRole("button", { name: /Next: map your tables/ }));
  await screen.findByRole("button", { name: /Step 2 of 3: Map/ });
}

// a help desk of its own shape: tickets keyed 'HD-7', no views anywhere
const TICKETS: InspectedTable = {
  table: "tickets",
  engine: "ReplacingMergeTree",
  rows: 42,
  columns: [
    { name: "ticket_key", type: "String" },
    { name: "summary", type: "String" },
    { name: "details", type: "String" },
    { name: "created", type: "DateTime" },
    { name: "resolved", type: "Nullable(DateTime)" },
    { name: "priority", type: "LowCardinality(String)" },
  ],
};
const PROPOSED: SourceMapping = {
  version: 1,
  tables: {
    github_issues: {
      source: "helpdesk.tickets",
      final: true,
      columns: {
        source_id: { concat: [{ lit: "Acme/kj#" }, { digits: "ticket_key" }] },
        number: { digits: "ticket_key" },
        title: "summary",
        body: "details",
        created_at: "created",
        closed_at: "resolved",
      },
    },
  },
};
const INSPECTED: TrackerInspectResult = {
  host: "abc.clickhouse.cloud",
  database: "helpdesk",
  tables: [TICKETS],
  roles: [],
  proposal: {
    mode: "mapping",
    roles: { github_issues: "tickets" },
    mapping: PROPOSED,
    fields: {
      github_issues: {
        title: { from: "summary", confidence: "high" },
        closed_at: { from: "resolved", confidence: "medium" },
      },
    },
  },
};
const MAPPED = result({
  database: "helpdesk",
  contract: {
    ok: true,
    mode: "mapping",
    summary: "ok; mapped github_issues",
    database: "helpdesk",
    missing_tables: [],
    missing_columns: {},
    optional_present: [],
    capabilities: {
      items: true,
      duplicate_check: true,
      links: false,
      pull_requests: false,
      bisect: false,
      incidents: false,
      ci_checks: false,
    },
  },
});

describe("Add a tracker", () => {
  beforeEach(() => {
    for (const f of [
      addTrackerSource,
      getTrackerContract,
      removeTrackerSource,
      checkTrackerSource,
      inspectTrackerSource,
    ])
      vi.mocked(f).mockReset();
    vi.mocked(getTrackerContract).mockResolvedValue(CATALOGUE);
    vi.mocked(inspectTrackerSource).mockResolvedValue(INSPECTED);
  });
  afterEach(cleanup);

  it("connect: a pasted URL fills the address, the repository is a chip, the name defaults free — axe-clean", async () => {
    render(
      <RegisterTrackerSourceDialog
        open
        org="dev"
        repo="Acme/kj"
        takenNames={["kj"]}
        onOpenChange={() => {}}
        onRegistered={() => {}}
      />,
    );
    const d = await screen.findByRole("dialog");
    expect(within(d).getByRole("button", { name: /Step 1 of 3: Connect/ })).toHaveAttribute("aria-current", "step");
    expect(within(d).getByText(/in whatever schema you have/)).toBeInTheDocument();
    connect();
    expect(screen.getByText("abc.clickhouse.cloud", { exact: true })).toBeInTheDocument();
    expect(screen.getByLabelText("Port")).toHaveValue("8443");
    expect(screen.getByLabelText(/^Database/)).toHaveValue("helpdesk");
    expect(screen.getByRole("button", { name: "Remove Acme/kj" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Advanced/ })).toHaveTextContent("name kj-2, public, joins at once");
    expect(unlabeledControls(document.body)).toEqual([]);
    expect(await axeViolations(document.body)).toEqual([]);
  });

  it("connect: the proposed name follows the repository chips", async () => {
    render(
      <RegisterTrackerSourceDialog
        open
        org="dev"
        repo="Acme/kj"
        takenNames={["kj"]}
        onOpenChange={() => {}}
        onRegistered={() => {}}
      />,
    );
    await screen.findByRole("dialog");
    const advanced = () => screen.getByRole("button", { name: /Advanced/ });
    expect(advanced()).toHaveTextContent("name kj-2,");
    fireEvent.click(screen.getByRole("button", { name: "Remove Acme/kj" }));
    expect(advanced()).toHaveTextContent("name …,");
    fireEvent.change(screen.getByLabelText("Repositories it holds"), { target: { value: "Acme/helpdesk " } });
    expect(advanced()).toHaveTextContent("name helpdesk,");
  });

  it("map: loom reads the tables and proposes; the person sees each field, and nothing is SQL", async () => {
    render(
      <RegisterTrackerSourceDialog open org="dev" repo="Acme/kj" onOpenChange={() => {}} onRegistered={() => {}} />,
    );
    await toMap();
    expect(vi.mocked(inspectTrackerSource).mock.calls[0][0]).toMatchObject({
      host: "abc.clickhouse.cloud",
      database: "helpdesk",
      user: "loom_ro",
      repos: ["Acme/kj"],
    });
    expect(screen.getByText(/Your schema, not Loom's:/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Items: your table" })).toHaveTextContent("tickets · 42 rows");
    expect(screen.getByLabelText(/^Title/)).toHaveTextContent("summary · String");
    expect(screen.getByRole("button", { name: /Reference: number/ })).toHaveTextContent("number in ticket_key");
    expect(screen.getByLabelText(/Reference: fixed repository/)).toHaveValue("Acme/kj");
    expect(screen.getAllByText("matched").length).toBeGreaterThan(0);
    expect(screen.queryByText(/CREATE (OR REPLACE )?VIEW/)).toBeNull();
    expect(screen.queryByRole("textbox", { name: /SQL/ })).toBeNull();
    expect(unlabeledControls(document.body)).toEqual([]);
    expect(await axeViolations(document.body)).toEqual([]);
  });

  it("a database with loom's tables needs no mapping, and the check sends none", async () => {
    vi.mocked(inspectTrackerSource).mockResolvedValueOnce({
      ...INSPECTED,
      proposal: { mode: "views", roles: {}, fields: {}, mapping: null },
    });
    vi.mocked(addTrackerSource).mockResolvedValueOnce(result());
    render(
      <RegisterTrackerSourceDialog open org="dev" repo="Acme/kj" onOpenChange={() => {}} onRegistered={() => {}} />,
    );
    await toMap();
    expect(screen.getByText(/already exposes Loom's tables — nothing to map/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^Check$/ }));
    expect(await screen.findByText("Ready to add. Nothing has been saved yet.")).toBeInTheDocument();
    expect(vi.mocked(addTrackerSource).mock.calls[0][0]).not.toHaveProperty("mapping");
  });

  it("check writes nothing and says what loom can do; Add sends the checked mapping; a changed mapping must be checked again", async () => {
    const onRegistered = vi.fn();
    vi.mocked(addTrackerSource)
      .mockResolvedValueOnce(MAPPED)
      .mockResolvedValueOnce(MAPPED)
      .mockResolvedValueOnce(result({ ...MAPPED, dry_run: false, written: true }));
    render(
      <RegisterTrackerSourceDialog open org="dev" repo="Acme/kj" onOpenChange={() => {}} onRegistered={onRegistered} />,
    );
    await toMap();
    fireEvent.click(screen.getByRole("button", { name: /^Check$/ }));
    expect(await screen.findByText("Ready to add. Nothing has been saved yet.")).toBeInTheDocument();
    expect(vi.mocked(addTrackerSource).mock.calls[0][0]).toMatchObject({
      dry_run: true,
      database: "helpdesk",
      mapping: PROPOSED,
    });
    const caps = screen.getByRole("region", { name: "What Loom can do with this tracker" });
    expect(within(caps).getByText(/duplicate/i)).toBeInTheDocument();
    expect(screen.getByText(/Your mapping fits the tables in helpdesk/)).toBeInTheDocument();
    // back to the mapping, change a field: the check no longer covers it
    fireEvent.click(screen.getByRole("button", { name: "Edit mapping" }));
    // Title is a Click UI Select: open it, pick the column
    fireEvent.click(screen.getByLabelText(/^Title/));
    fireEvent.click(within(screen.getAllByRole("dialog").at(-1) as HTMLElement).getByText("details · String"));
    fireEvent.click(screen.getByRole("button", { name: /Step 3 of 3: Check/ }));
    expect(screen.getByText(/The connection or the mapping changed since this check/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add and link Acme/kj" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Add and link Acme/kj" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Add and link Acme/kj" }));
    expect(await screen.findByText("kj is connected")).toBeInTheDocument();
    expect(onRegistered).toHaveBeenCalledWith("tracker-kj", expect.objectContaining({ written: true }));
    expect(vi.mocked(addTrackerSource).mock.calls[2][0]).toMatchObject({
      dry_run: false,
      mapping: { tables: { github_issues: { columns: { title: "details" } } } },
    });
  });

  it("a mapping with a required field unset cannot be checked, and says which", async () => {
    vi.mocked(inspectTrackerSource).mockResolvedValueOnce({
      ...INSPECTED,
      proposal: { ...INSPECTED.proposal, mapping: { version: 1, tables: {} } },
    });
    render(
      <RegisterTrackerSourceDialog open org="dev" repo="Acme/kj" onOpenChange={() => {}} onRegistered={() => {}} />,
    );
    await toMap();
    expect(
      within(screen.getByRole("list", { name: "Before the check" })).getByText("Pick the table that holds your items."),
    ).toBeInTheDocument();
    const check = screen.getByRole("button", { name: /^Check$/ });
    expect(check).toHaveAttribute("aria-disabled", "true");
    expect(check).toHaveAccessibleDescription("Pick the table that holds your items.");
  });

  it("a views database that fails the contract gets the fix per table", async () => {
    vi.mocked(inspectTrackerSource).mockResolvedValueOnce({
      ...INSPECTED,
      proposal: { mode: "views", roles: {}, fields: {}, mapping: null },
    });
    vi.mocked(addTrackerSource).mockResolvedValueOnce(FAILING);
    render(
      <RegisterTrackerSourceDialog open org="dev" repo="Acme/kj" onOpenChange={() => {}} onRegistered={() => {}} />,
    );
    await toMap();
    fireEvent.click(screen.getByRole("button", { name: /^Check$/ }));
    const checks = await screen.findByRole("list", { name: "Checks" });
    await waitFor(() =>
      expect(within(checks).getByText(/The views in helpdesk match what Loom reads/)).toHaveTextContent("failed"),
    );
    expect(within(checks).getByText(/Reach abc.clickhouse.cloud as loom_ro/)).toHaveTextContent("passed");
    expect(
      within(screen.getByRole("list", { name: "Views, table by table" })).getByText(/no such table or view/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy the view draft for github_prs" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add and link Acme/kj" })).toBeDisabled();
    expect(await axeViolations(document.body)).toEqual([]);
  });

  it("a refusal lands beside the check it belongs to", async () => {
    vi.mocked(addTrackerSource).mockRejectedValueOnce(
      new ApiError({
        status: 422,
        code: "CREDENTIAL_NOT_READONLY",
        message:
          "'loom_ro' holds more than read access (INSERT); loom takes a read-only user: create one with SELECT only, on the tables you map (or the views) and system.columns.",
      }),
    );
    render(
      <RegisterTrackerSourceDialog open org="dev" repo="Acme/kj" onOpenChange={() => {}} onRegistered={() => {}} />,
    );
    await toMap();
    fireEvent.click(screen.getByRole("button", { name: /^Check$/ }));
    const checks = await screen.findByRole("list", { name: "Checks" });
    await waitFor(() => expect(within(checks).getByText(/The user can only read/)).toHaveTextContent("failed"));
    expect(within(checks).getByText(/Reach abc.clickhouse.cloud/)).toHaveTextContent("passed");
    expect(
      within(checks).getByText(/holds more than read access \(INSERT\); loom takes a read-only user/),
    ).toBeInTheDocument();
    expect(within(checks).getByText(/Loom can read rows/)).toHaveTextContent("not checked");
  });

  it("a passing check names the roles the user holds, which the check found read-only", async () => {
    vi.mocked(addTrackerSource).mockResolvedValueOnce(result({ ...MAPPED, roles: ["analyst"] }));
    render(
      <RegisterTrackerSourceDialog open org="dev" repo="Acme/kj" onOpenChange={() => {}} onRegistered={() => {}} />,
    );
    await toMap();
    fireEvent.click(screen.getByRole("button", { name: /^Check$/ }));
    const checks = await screen.findByRole("list", { name: "Checks" });
    const roles = await within(checks).findByText(/It also holds the roles/);
    expect(roles).toHaveTextContent("It also holds the roles analyst, which grant read access only.");
  });

  it("a connection loom cannot read stays on Connect with the reason", async () => {
    vi.mocked(inspectTrackerSource)
      .mockReset()
      .mockRejectedValueOnce(
        new ApiError({ status: 422, code: "SOURCE_UNREACHABLE", message: "could not reach abc.clickhouse.cloud" }),
      );
    render(
      <RegisterTrackerSourceDialog open org="dev" repo="Acme/kj" onOpenChange={() => {}} onRegistered={() => {}} />,
    );
    connect();
    fireEvent.click(screen.getByRole("button", { name: /Next: map your tables/ }));
    expect(await screen.findByText(/could not reach abc.clickhouse.cloud/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Step 1 of 3: Connect/ })).toHaveAttribute("aria-current", "step");
  });
});

describe("the Trackers list", () => {
  afterEach(cleanup);

  it("says each tracker's state and what it lists; its admin checks it now and sees the fixes", async () => {
    vi.mocked(checkTrackerSource).mockResolvedValueOnce({
      name: "kj",
      database: "loom_tracker",
      reachable: true,
      contract: FAILING.contract,
      view_templates: FAILING.view_templates,
    });
    render(
      <main>
        <TrackersPanel
          org="dev"
          sources={[
            src(),
            src({
              name: "qa",
              origin: "config",
              namespaces: [{ ...src().namespaces[0], namespace: "tracker-clickhouse", member_role: "" }],
            }),
          ]}
          selfService
          loading={false}
          onAdd={() => {}}
          onChanged={() => {}}
        />
      </main>,
    );
    const rows = screen.getAllByRole("listitem").filter((li) => li.id.startsWith("tracker-"));
    expect(rows.map((r) => r.id)).toEqual(["tracker-kj", "tracker-qa"]);
    expect(within(rows[0]).getByText("ready")).toBeInTheDocument();
    expect(within(rows[0]).getByText("Acme/kj")).toBeInTheDocument();
    expect(within(rows[0]).getByText(/12 issues · 30 PRs/)).toBeInTheDocument();
    // an operator's tracker, or one the caller does not administer: no verbs
    expect(within(rows[1]).queryByRole("button", { name: "Check now" })).toBeNull();
    fireEvent.click(within(rows[0]).getByRole("button", { name: "Check now" }));
    expect(await within(rows[0]).findByRole("button", { name: "Copy the view draft for edges" })).toBeInTheDocument();
    expect(checkTrackerSource).toHaveBeenCalledWith("dev", "kj");
    expect(await axeViolations(document.body)).toEqual([]);
  });

  it("with none connected, says how and offers the one button", () => {
    const onAdd = vi.fn();
    render(
      <main>
        <TrackersPanel org="dev" sources={[]} selfService loading={false} onAdd={onAdd} onChanged={() => {}} />
      </main>,
    );
    expect(screen.getByText(/No tracker is connected yet/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add a tracker" }));
    expect(onAdd).toHaveBeenCalled();
  });

  // The heading is a SectionHeading, and the empty state's button is secondary, so the
  // Repositories page keeps one primary.
  it("heads the section with a SectionHeading, and its empty state adds no second primary", () => {
    render(
      <main>
        <TrackersPanel org="dev" sources={[]} selfService loading={false} onAdd={() => {}} onChanged={() => {}} />
      </main>,
    );
    const heading = screen.getByRole("heading", { level: 2, name: "Trackers" });
    expect(heading.querySelector("svg")).toBeNull();
    expect(screen.getByRole("region", { name: "Trackers" })).toContainElement(heading);
    expect(screen.getByRole("button", { name: "Add a tracker" }).className).toMatch(/secondary/);
  });
});

describe("Retire a tracker", () => {
  afterEach(cleanup);
  it("needs the name typed, then retires", async () => {
    vi.mocked(removeTrackerSource).mockResolvedValueOnce({
      name: "kj",
      deactivated: true,
      namespaces_retired: ["tracker-kj"],
    });
    const onRetired = vi.fn();
    render(
      <RetireSourceDialog
        open
        org="dev"
        name="kj"
        namespaces={["tracker-kj"]}
        onOpenChange={() => {}}
        onRetired={onRetired}
      />,
    );
    const go = screen.getByRole("button", { name: "Retire tracker" });
    expect(go).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/to confirm/), { target: { value: "kj" } });
    fireEvent.click(go);
    await waitFor(() => expect(onRetired).toHaveBeenCalledWith(["tracker-kj"]));
    expect(removeTrackerSource).toHaveBeenCalledWith("dev", "kj", "kj");
  });
});
