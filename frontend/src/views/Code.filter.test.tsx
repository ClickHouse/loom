/**
 * The Repositories filter: a filter that matches nothing says so, and Clear filters empties
 * the box and brings the cards back.
 */
import { cleanup, render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
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

describe("Repositories, the filter", () => {
  beforeEach(() => {
    invalidateShared();
    useStore.setState({ authed: true, identity: { org: "dev", namespace: "default", agent: "loom-ui" }, view: "code" });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        const path = url.replace(/^https?:\/\/[^/]+/, "").split("?")[0];
        const json = (body: unknown) =>
          new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
        if (path === "/v1/code.repos") return json({ repos: [REPO] });
        if (path === "/v1/health") return json({ status: "ok", problems: [] });
        return json({ repos: [], rows: [], entries: [], sources: [], namespaces: [] });
      }),
    );
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("Clear filters on a filter that matches nothing empties the box and brings the cards back", async () => {
    render(<CodeView />);
    expect((await screen.findAllByText("acme/app", {}, { timeout: 2000 })).length).toBeGreaterThan(0);
    const box = screen.getByLabelText("Filter repositories");
    await userEvent.type(box, "nothing");
    await userEvent.click(await screen.findByRole("button", { name: "Clear filters" }));
    expect(box).toHaveValue("");
    await waitFor(() => expect(screen.getAllByText("acme/app").length).toBeGreaterThan(0));
    expect(screen.queryByRole("button", { name: "Clear filters" })).toBeNull();
  });
});
