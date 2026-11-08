import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../../lib/state";
import { Health, OpMixPanel, WorkersPanel, colourOf, parseOpMix } from "./Health";

// The Workers tab shows the freshest report per worker from either this process's
// registry or loom.worker_status, says which colour it came from, and reads "no workers
// reporting" only when neither has one.

function respond(body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, json: async () => body })),
  );
}

const recent = new Date(Date.now() - 20_000).toISOString();

describe("WorkersPanel", () => {
  // workers.status is claim-gated: auth off is the operator, so the panel reads it.
  beforeEach(() => {
    useStore.setState({ whoami: { authenticated: false } });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    useStore.setState({ whoami: null });
  });

  it("makes no request without an admin claim and reads 'no workers reporting'", async () => {
    useStore.setState({ whoami: { authenticated: true, subject: "v@example.com", org: "dev", roles: ["VIEWER"] } });
    respond({ workers: {} });
    render(<WorkersPanel />);
    await waitFor(() => expect(screen.getByText("No workers reporting")).toBeInTheDocument());
    expect(fetch).not.toHaveBeenCalled();
  });

  it("renders registry and store rows with their pass verdict, duration and colour", async () => {
    respond({
      workers: {
        embedding_worker: {
          enabled: true,
          effective: true,
          interval_seconds: 30,
          last_run_at: recent,
          last_duration_ms: 420,
          consecutive_failures: 0,
          total_runs: 7,
          last_pass_status: "ok",
          reporting_from: "this process",
          source: "registry",
        },
        audit_verifier: {
          enabled: true,
          effective: true,
          interval_seconds: 3600,
          last_run_at: recent,
          last_duration_ms: 12_300,
          consecutive_failures: 0,
          last_pass_status: "idle",
          reporting_from: "deploy:workers:green",
          source: "store",
        },
        code_graph_stats: {
          enabled: true,
          effective: true,
          interval_seconds: 86400,
          last_run_at: recent,
          last_duration_ms: 900_000,
          consecutive_failures: 1,
          last_error: "pass exceeded its deadline of 900s and was cancelled",
          last_pass_status: "timeout",
          reporting_from: "deploy:workers:green",
          source: "store",
          namespace: "clickhouse-main",
          trigger: "interval",
        },
        code_ingest: {
          enabled: true,
          effective: true,
          interval_seconds: 300,
          last_run_at: recent,
          last_duration_ms: 5_000,
          consecutive_failures: 0,
          last_pass_status: "ok",
          reporting_from: "iso:wt:7801",
          source: "store",
          namespace: "reporter-public",
          trigger: "manual",
        },
        erasure_recovery: {
          enabled: true,
          effective: true,
          interval_seconds: 60,
          last_run_at: recent,
          last_duration_ms: 900,
          consecutive_failures: 3,
          last_error: "DEGRADED — 3 consecutive failures; ConnectionError: refused",
          last_pass_status: "failed",
          reporting_from: "deploy:workers:green",
          source: "store",
        },
      },
      store: {
        table: "loom.worker_status",
        rows: 2,
        reporters: ["deploy:workers:green"],
        in_process: 1,
        from_store: 2,
      },
    });
    render(<WorkersPanel />);
    await waitFor(() => expect(screen.getByText("Memory indexing")).toBeInTheDocument());
    expect(screen.getByText("Audit integrity")).toBeInTheDocument();
    expect(screen.getByText(/Idle — ran, nothing to do/)).toBeInTheDocument();
    expect(screen.getByText(/Last pass failed/)).toBeInTheDocument();
    expect(screen.getByText("3 consecutive failures, degraded")).toBeInTheDocument();
    expect(screen.getByText(/ConnectionError: refused/)).toBeInTheDocument();
    expect(screen.getByText(/took 12\.3 s/)).toBeInTheDocument();
    expect(screen.getByText(/took 420 ms/)).toBeInTheDocument();
    expect(screen.getAllByText("green").length).toBeGreaterThanOrEqual(2);
    // The namespace a pass reached and a manual run.
    expect(screen.getByText("clickhouse-main")).toBeInTheDocument();
    expect(screen.getByText("reporter-public")).toBeInTheDocument();
    expect(screen.getAllByText(/reached:/).length).toBe(2);
    expect(screen.getByText(/manual run/)).toBeInTheDocument();
    expect(screen.getByText(/Timed out at its deadline/)).toBeInTheDocument();
    expect(screen.getByTestId("reporting-colours")).toHaveTextContent("Reporting from green");
    expect(screen.getByText("2 issues")).toBeInTheDocument();
    expect(screen.queryByText("No workers reporting")).not.toBeInTheDocument();
  });

  it("says all systems running only when a report exists somewhere", async () => {
    respond({
      workers: {
        embedding_worker: { enabled: true, effective: true, interval_seconds: 30, source: null },
      },
      store: { table: "loom.worker_status", rows: 0, reporters: [], in_process: 0, from_store: 0 },
    });
    render(<WorkersPanel />);
    await waitFor(() => expect(screen.getByText("Memory indexing")).toBeInTheDocument());
    expect(screen.getByText("No workers reporting")).toBeInTheDocument();
    expect(screen.queryByText("All systems running")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(/loom\.worker_status/);
    expect(screen.getByText(/No pass reported yet/)).toBeInTheDocument();
  });

  it("keeps the workers on screen, marked busy, while Refresh reads again", async () => {
    const worker = {
      enabled: true,
      effective: true,
      interval_seconds: 30,
      last_run_at: recent,
      last_duration_ms: 420,
      consecutive_failures: 0,
      last_pass_status: "ok",
      source: "registry",
    };
    respond({ workers: { embedding_worker: worker } });
    render(<WorkersPanel />);
    await waitFor(() => expect(screen.getByText("Memory indexing")).toBeInTheDocument());
    let answer: (v: unknown) => void = () => undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise((r) => (answer = r))),
    );
    fireEvent.click(screen.getByRole("button", { name: /Refresh/ }));
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(screen.getByText("Memory indexing")).toBeInTheDocument();
    expect(screen.getByTestId("workers-list")).toHaveAttribute("aria-busy", "true");
    await act(async () => answer({ ok: true, json: async () => ({ workers: { embedding_worker: worker } }) }));
    await waitFor(() => expect(screen.getByTestId("workers-list")).not.toHaveAttribute("aria-busy"));
    cleanup();
  });

  it("with every worker disabled in config, its empty state offers Show all, which shows them", async () => {
    const worker = {
      enabled: false,
      effective: false,
      disabled_reason: "disabled_by_setting",
      interval_seconds: 30,
      last_run_at: null,
      last_duration_ms: 0,
      consecutive_failures: 0,
      last_pass_status: "",
      source: "registry",
    };
    respond({ workers: { embedding_worker: worker } });
    render(<WorkersPanel />);
    expect(await screen.findByText("Nothing to show")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show all" }));
    expect(await screen.findByText("Memory indexing")).toBeInTheDocument();
    cleanup();
  });

  it("lays the workers out as a table, one row each, its status a badge", async () => {
    const worker = {
      enabled: true,
      effective: true,
      interval_seconds: 30,
      last_run_at: recent,
      last_duration_ms: 420,
      consecutive_failures: 0,
      last_pass_status: "ok",
    };
    respond({ workers: { embedding_worker: worker, dedup_worker: { ...worker, consecutive_failures: 1 } } });
    render(<WorkersPanel />);
    await waitFor(() => expect(screen.getByText("Memory indexing")).toBeInTheDocument());
    const table = screen.getByRole("table", { name: "Workers" });
    expect(
      within(table)
        .getAllByRole("columnheader")
        .map((h) => h.textContent),
    ).toEqual(["Worker", "Status", "Last pass", "Every"]);
    const [, indexing, dedup] = within(table).getAllByRole("row");
    expect(within(indexing).getAllByRole("cell")[1]).toHaveTextContent("Healthy");
    expect(within(indexing).getAllByRole("cell")[3]).toHaveTextContent("30s");
    expect(within(dedup).getAllByRole("cell")[1]).toHaveTextContent("1 consecutive failure");
    expect(within(dedup).getAllByRole("cell")[1]).not.toHaveTextContent("Healthy");
  });

  it("draws the first read as the table's header over a skeleton, marked busy", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise(() => {})),
    );
    render(<WorkersPanel />);
    const table = await screen.findByRole("table", { name: "Workers" });
    expect(table).toHaveAttribute("aria-busy", "true");
    // The first read waits 300 ms before its loader shows, like every loader
    // (ui/LoadingSkeleton), under the header row already in place.
    expect(within(table).queryByText("Loading data")).toBeNull();
    expect(within(table).getByRole("status", { name: "Loading workers" })).toBeInTheDocument();
  });

  it("shows the empty state only when there is nothing from either source", async () => {
    respond({ workers: {}, store: { rows: 0, reporters: [] } });
    render(<WorkersPanel />);
    await waitFor(() => expect(screen.getByText("No workers reporting")).toBeInTheDocument());
    expect(screen.getByText(/Neither this process nor loom\.worker_status/)).toBeInTheDocument();
  });

  it("renders a malformed body as no rows instead of throwing", async () => {
    respond({ workers: { x: 5, y: null, z: "up" }, store: "loom.worker_status" });
    render(<WorkersPanel />);
    await waitFor(() => expect(screen.getByText("No workers reporting")).toBeInTheDocument());
    expect(screen.getByText(/Neither this process nor loom\.worker_status/)).toBeInTheDocument();
    cleanup();

    // A list whose entries are not named objects, then a body that is not an object at all.
    respond({ workers: [5, { name: 7 }] });
    render(<WorkersPanel />);
    await waitFor(() => expect(screen.getByText("No workers reporting")).toBeInTheDocument());
    cleanup();

    respond(null);
    render(<WorkersPanel />);
    await waitFor(() => expect(screen.getByText("No workers reporting")).toBeInTheDocument());
  });

  it("reads a field of the wrong type as absent", async () => {
    respond({
      workers: {
        embedding_worker: {
          effective: true,
          last_run_at: recent,
          last_error: 5,
          rows_written: "12",
          last_pass_status: "exploded",
          consecutive_failures: "3",
        },
      },
    });
    render(<WorkersPanel />);
    await waitFor(() => expect(screen.getByText("Memory indexing")).toBeInTheDocument());
    expect(screen.getByText("All systems running")).toBeInTheDocument();
    expect(screen.queryByText(/Error:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/rows written/)).not.toBeInTheDocument();
    expect(screen.queryByText(/consecutive failure/)).not.toBeInTheDocument();
  });

  it("reads the colour off a deployment label", () => {
    expect(colourOf("deploy:workers:green")).toBe("green");
    expect(colourOf("deploy:web:blue")).toBe("blue");
    expect(colourOf("deploy:web")).toBe("deploy:web");
    expect(colourOf("iso:w6v3:7864")).toBe("iso:w6v3:7864");
    expect(colourOf("this process")).toBe("this process");
    expect(colourOf("")).toBe("");
  });
});

describe("Health tabs", () => {
  // Each operator tab follows its endpoint's gate: Workers reads /v1/admin/workers.status
  // (an ADMIN claim), Operation mix reads ops.health (ORG_ADMIN).
  const as = (roles: string[]) =>
    useStore.setState({
      identity: { org: "dev", namespace: "team-a", agent: "loom-ui" },
      whoami: { authenticated: true, subject: "o@example.com", org: "dev", roles },
    });
  beforeEach(() => respond({ rows: [], workers: {} }));
  afterEach(() => {
    cleanup(); // unmount before identity goes null under it
    vi.unstubAllGlobals();
    useStore.setState({ whoami: null, identity: null });
  });

  it("an ADMIN claim sees Workers but not Operation mix", () => {
    as(["ADMIN"]);
    render(<Health />);
    expect(screen.getByRole("tab", { name: /Workers/ })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /Operation mix/ })).not.toBeInTheDocument();
  });

  it("an ORG_ADMIN claim sees both", () => {
    as(["ORG_ADMIN"]);
    render(<Health />);
    expect(screen.getByRole("tab", { name: /Workers/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Operation mix/ })).toBeInTheDocument();
  });

  it("falls back to Extraction quality when a switch hides the open Repairs tab", async () => {
    as(["VIEWER"]);
    useStore.setState({
      myNamespaces: [
        { namespace: "team-a", role: "admin", visibility: "private", description: "", joined_at: "", is_new: false },
        { namespace: "team-b", role: "viewer", visibility: "private", description: "", joined_at: "", is_new: false },
      ],
    });
    render(<Health />);
    fireEvent.mouseDown(screen.getByRole("tab", { name: /Repairs/ }));
    expect(screen.getByRole("tab", { name: /Repairs/ })).toHaveAttribute("aria-selected", "true");
    act(() => useStore.setState({ identity: { org: "dev", namespace: "team-b", agent: "loom-ui" } }));
    expect(screen.queryByRole("tab", { name: /Repairs/ })).toBeNull();
    expect(screen.getByRole("tab", { name: /Extraction quality/ })).toHaveAttribute("aria-selected", "true");
    useStore.setState({ myNamespaces: null });
  });
});

describe("parseOpMix", () => {
  const good = {
    window_days: 7,
    counts: { read: 40, create: 10 },
    writes: 10,
    reads_per_write: 4,
    update_share: 0,
    agents: 2,
    top: [{ operation: "memory.search", count: 30, class: "read" }],
  };

  it("keeps a well-formed op_mix block", () => {
    expect(parseOpMix(good)).toEqual(good);
  });

  it("is null when the block or a headline number is missing", () => {
    expect(parseOpMix(null)).toBeNull();
    expect(parseOpMix([])).toBeNull();
    expect(parseOpMix({ ...good, window_days: "7" })).toBeNull();
    expect(parseOpMix({ counts: {} })).toBeNull();
  });

  it("drops malformed counts and top entries and reads absent ratios as null", () => {
    const { window_days, writes, agents, update_share } = good;
    expect(parseOpMix({ window_days, writes, agents, update_share, counts: { read: "40", create: 10 } })).toEqual({
      window_days,
      writes,
      agents,
      update_share,
      counts: { create: 10 },
      reads_per_write: null,
      top: [],
    });
    expect(parseOpMix({ ...good, top: [5, { operation: "memory.get", count: "3", class: "read" }] })?.top).toEqual([]);
  });
});

describe("Extraction quality Refresh", () => {
  // Refresh goes through the org-keyed effect: a refresh still in flight when the org
  // switches is dropped, never rendered over the new org's rows.
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    useStore.setState({ whoami: null, identity: null });
  });

  it("drops a refresh answer that lands after an org switch", async () => {
    const qualityRow = (namespace: string) => ({ namespace, pending: 1, promoted: 0, rejected: 0, approve_ratio: 0 });
    let releaseStale: (rows: unknown) => void = () => {};
    let qualityCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (url !== "/v1/learnings.quality") return { ok: true, json: async () => ({ rows: [] }) };
        qualityCalls += 1;
        const body: unknown = JSON.parse(String(init?.body));
        const org = typeof body === "object" && body !== null && "org" in body ? String(body.org) : "";
        if (qualityCalls === 2) {
          const rows = await new Promise((resolve) => {
            releaseStale = resolve;
          });
          return { ok: true, json: async () => ({ rows }) };
        }
        return { ok: true, json: async () => ({ rows: [qualityRow(`${org}-ns`)] }) };
      }),
    );
    useStore.setState({
      identity: { org: "dev", namespace: "team-a", agent: "loom-ui" },
      whoami: { authenticated: true, subject: "v@example.com", org: "dev", roles: ["VIEWER"] },
    });
    render(<Health />);
    await waitFor(() => expect(screen.getByText("dev-ns")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /Refresh/ }));
    await waitFor(() => expect(qualityCalls).toBe(2));
    act(() => useStore.setState({ identity: { org: "acme", namespace: "team-a", agent: "loom-ui" } }));
    await waitFor(() => expect(screen.getByText("acme-ns")).toBeInTheDocument());

    await act(async () => releaseStale([qualityRow("dev-stale")]));
    expect(screen.getByText("acme-ns")).toBeInTheDocument();
    expect(screen.queryByText("dev-stale")).not.toBeInTheDocument();
  });
});

describe("OpMixPanel", () => {
  // A check the server ran but could not compute is a failure with a reason, not the
  // "needs an org-admin token" state a refused request reads as.
  beforeEach(() =>
    useStore.setState({
      identity: { org: "dev", namespace: "team-a", agent: "loom-ui" },
      whoami: { authenticated: true, subject: "o@example.com", org: "dev", roles: ["ORG_ADMIN"] },
    }),
  );
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    useStore.setState({ whoami: null, identity: null });
  });
  const opMix = (over: Record<string, unknown>) => ({
    checks: [{ name: "op_mix", status: "ok", detail: "", data: {}, repair: "", ...over }],
    overall: "ok",
  });

  it("shows a failed check's detail and repair instead of the org-admin text", async () => {
    respond(opMix({ status: "fail", detail: "boom", repair: "check loom.agent_events" }));
    render(<OpMixPanel />);
    expect(await screen.findByText("Operation mix check failed")).toBeInTheDocument();
    expect(screen.getByText(/boom/)).toHaveTextContent("boom — check loom.agent_events");
    expect(screen.queryByText(/org-admin/)).not.toBeInTheDocument();
  });

  it("says an answer without its op_mix check is a failed read", async () => {
    respond({ checks: [{ name: "clickhouse_parts", status: "ok", detail: "", data: {}, repair: "" }], overall: "ok" });
    render(<OpMixPanel />);
    expect(await screen.findByText("Couldn't load the operation mix")).toBeInTheDocument();
    expect(screen.queryByText("Operation mix unavailable")).toBeNull();
  });

  it("reads a refused request as unavailable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ detail: "forbidden" }), { status: 403 })),
    );
    render(<OpMixPanel />);
    expect(await screen.findByText("Operation mix unavailable")).toBeInTheDocument();
  });

  it("over a failed check, the badge names the scope that check was read for while a toggle reads", async () => {
    let calls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        calls += 1;
        if (calls > 1) return new Promise(() => {});
        return new Response(JSON.stringify(opMix({ status: "fail", detail: "boom" })), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
    render(<OpMixPanel />);
    expect(await screen.findByText("Operation mix check failed")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Whole org" }));
    await waitFor(() => expect(calls).toBe(2));
    expect(screen.queryByText("org · dev")).toBeNull();
  });

  it("a failed org-wide read keeps the scope toggle, so the reader can go back to the namespace", async () => {
    const report = opMix({
      detail: "healthy mix",
      data: { window_days: 7, writes: 10, agents: 2, counts: { create: 10, read: 40 }, top: [] },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) =>
        !("namespace" in JSON.parse(String(init?.body)))
          ? new Response("down", { status: 503 })
          : new Response(JSON.stringify(report), { status: 200, headers: { "Content-Type": "application/json" } }),
      ),
    );
    render(<OpMixPanel />);
    expect(await screen.findByText("healthy mix")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Whole org" }));
    expect(await screen.findByText("Couldn't load the operation mix")).toBeInTheDocument();
    // The badge names the scope of what is shown: the org read that failed.
    expect(screen.getByText("org · dev")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "This namespace" }));
    expect(await screen.findByText("healthy mix")).toBeInTheDocument();
  });

  it("renders a computed check's report", async () => {
    respond(
      opMix({
        detail: "healthy mix",
        data: { window_days: 7, writes: 10, agents: 2, counts: { create: 10, read: 40 }, top: [] },
      }),
    );
    render(<OpMixPanel />);
    expect(await screen.findByText("healthy mix")).toBeInTheDocument();
    expect(screen.getByText(/last 7 d · 50 calls · 2 agents/)).toBeInTheDocument();
    expect(screen.queryByText("Operation mix check failed")).not.toBeInTheDocument();
  });
});

describe("WorkersPanel count, times and failure", () => {
  beforeEach(() => useStore.setState({ whoami: { authenticated: false } }));
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    useStore.setState({ whoami: null });
  });
  const worker = {
    enabled: true,
    effective: true,
    interval_seconds: 30,
    last_run_at: recent,
    last_duration_ms: 420,
    consecutive_failures: 0,
    last_pass_status: "ok",
  };

  it("counts the workers shown at the end of the toolbar, and shows the last pass as a list time", async () => {
    respond({ workers: { embedding_worker: worker, dedup_worker: worker } });
    render(<WorkersPanel />);
    await waitFor(() => expect(screen.getByText("Memory indexing")).toBeInTheDocument());
    const bar = document.querySelector<HTMLElement>('[role="group"][aria-label="Workers status"]');
    expect(bar).toHaveTextContent("2 workers");
    expect(within(bar!).getByRole("button", { name: /Refresh/ })).toBeInTheDocument();
    expect(screen.getAllByText("just now")[0].tagName).toBe("TIME");
  });

  it("says a failed read failed, with Retry, never 'No workers reporting'", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ detail: "down" }), { status: 503 })),
    );
    render(<WorkersPanel />);
    expect(await screen.findByText("Couldn't load workers")).toBeInTheDocument();
    expect(screen.queryByText("No workers reporting")).toBeNull();
    expect(document.querySelector('[role="group"][aria-label="Workers status"]')).not.toHaveTextContent(/worker/);
    respond({ workers: { embedding_worker: worker } });
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByText("Memory indexing")).toBeInTheDocument());
  });
});

describe("Extraction quality failure", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    useStore.setState({ whoami: null, identity: null });
  });

  it("says a failed read failed, with Retry", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        url === "/v1/learnings.quality"
          ? new Response(JSON.stringify({ detail: "down" }), { status: 503 })
          : new Response(JSON.stringify({ rows: [] }), { status: 200 }),
      ),
    );
    useStore.setState({
      identity: { org: "dev", namespace: "team-a", agent: "loom-ui" },
      whoami: { authenticated: true, subject: "v@example.com", org: "dev", roles: ["VIEWER"] },
    });
    render(<Health />);
    expect(await screen.findByText("Couldn't load extraction quality")).toBeInTheDocument();
    respond({ rows: [{ namespace: "dev-ns", pending: 1, promoted: 0, rejected: 0, approve_ratio: 0 }] });
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByText("dev-ns")).toBeInTheDocument());
  });
});
