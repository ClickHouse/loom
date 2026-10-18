/**
 * The Repositories list polls every two minutes. A poll that fails after a good read keeps
 * the cards and says it could not refresh; only a failed first read replaces them.
 */
import { act, cleanup, render, screen, waitFor } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { invalidateShared } from "../lib/sharedReads";
import { CodeView } from "./Code";

const REPO = {
  org: "dev",
  namespace: "code-app",
  repo: "acme/app",
  branch: "main",
  git_head: "abc123def456",
  clone_head: "abc123def456",
  fast_tier_lag: 0,
  scip_lag: 0,
  scip_py_lag: 0,
  indexed_at: new Date().toISOString(),
  last_refresh_at: "",
  counts: { code_files: 10, code_symbols: 20 },
  join_policy: "open",
  my_status: "member",
  my_role: "viewer",
  source: "config",
};

describe("Repositories, a failed poll", () => {
  let reposFail = false;
  beforeEach(() => {
    invalidateShared();
    useStore.setState({ authed: true, identity: { org: "dev", namespace: "default", agent: "loom-ui" }, view: "code" });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        const path = url.replace(/^https?:\/\/[^/]+/, "").split("?")[0];
        const json = (body: unknown, status = 200) =>
          new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
        if (path === "/v1/code.repos") return reposFail ? json({ detail: "down" }, 503) : json({ repos: [REPO] });
        if (path === "/v1/health") return json({ status: "ok", problems: [] });
        return json({ repos: [], rows: [], entries: [], sources: [], namespaces: [] });
      }),
    );
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.useRealTimers();
    reposFail = false;
  });

  it("keeps the cards and says it could not refresh", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<CodeView />);
    expect((await screen.findAllByText("acme/app", {}, { timeout: 2000 })).length).toBeGreaterThan(0);
    reposFail = true;
    await act(async () => {
      vi.advanceTimersByTime(120_000);
    });
    await waitFor(() => expect(screen.getByText(/Couldn't refresh the repositories/)).toBeInTheDocument());
    expect(screen.getAllByText("acme/app").length).toBeGreaterThan(0);
    expect(screen.queryByText("Couldn't load repositories")).toBeNull();
  });
});
