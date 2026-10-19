import { act, cleanup, fireEvent, render, screen } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { invalidateShared } from "../lib/sharedReads";
import { CodeView } from "./Code";

// The cards are memoised and filter by the text a render behind the box: a keystroke that
// leaves the cards as they were renders none of them again. Each card is a ListRow, so
// ListRow counts the cards rendered.

const renders = { count: 0 };
vi.mock("../components/ui/ListRow", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../components/ui/ListRow")>();
  return {
    ...actual,
    ListRow: (props: Parameters<typeof actual.ListRow>[0]) => {
      renders.count += 1;
      return <actual.ListRow {...props} />;
    },
  };
});

const base = {
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

const REPOS = ["acme/app", "acme/api", "acme/web"].map((repo, i) => ({ ...base, repo, namespace: `code-${i}` }));

describe("Repositories card renders", () => {
  beforeEach(() => {
    renders.count = 0;
    invalidateShared();
    useStore.setState({ authed: true, identity: { org: "dev", namespace: "default", agent: "loom-ui" }, view: "code" });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        const path = url.replace(/^https?:\/\/[^/]+/, "").split("?")[0];
        const json = (body: unknown) =>
          new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
        if (path === "/v1/code.repos") return json({ repos: REPOS });
        if (path === "/v1/health") return json({ status: "ok", problems: [] });
        return json({ repos: [], rows: [], entries: [], sources: [], namespaces: [] });
      }),
    );
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("a keystroke whose filter every card still matches renders no card again", async () => {
    render(<CodeView />);
    await screen.findByText("acme/web", {}, { timeout: 2000 });
    renders.count = 0;
    await act(async () => {
      fireEvent.change(screen.getByLabelText("Filter repositories"), { target: { value: "acme" } });
    });
    expect(screen.getByText("acme/web")).toBeInTheDocument();
    expect(renders.count).toBe(0);
  });
});
