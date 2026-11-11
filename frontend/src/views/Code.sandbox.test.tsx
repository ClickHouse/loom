/**
 * Where the server runs without the repository-job sandbox it refuses adding a repository
 * or branch from the page (`SANDBOX_UNAVAILABLE`), so the page offers neither.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useStore } from "../lib/state";
import { invalidateShared } from "../lib/sharedReads";
import { CodeView } from "./Code";

const UNSANDBOXED = { code: "repo_jobs_unsandboxed", severity: "warn", message: "", fix: "" };

/** Stubs the network: `/v1/health` lists `problems()`, `code.repo.add` refuses for want
 *  of the sandbox, everything else is an empty list. */
function stubNetwork(problems: () => unknown[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      const path = url.replace(/^https?:\/\/[^/]+/, "").split("?")[0];
      const json = (body: unknown, status = 200) =>
        new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
      if (path === "/v1/health") return json({ status: "ok", problems: problems() });
      if (path === "/v1/code.repo.add")
        return json({ detail: { code: "SANDBOX_UNAVAILABLE", message: "needs the sandbox" } }, 422);
      return json({ repos: [], rows: [], entries: [], sources: [], namespaces: [] });
    }),
  );
}

async function mount() {
  render(<CodeView />);
  await act(async () => {
    await new Promise((r) => setTimeout(r, 30));
  });
}

describe("Repositories page without the repository-job sandbox", () => {
  beforeEach(() => {
    invalidateShared();
    useStore.setState({
      authed: true,
      identity: { org: "dev", namespace: "default", agent: "loom-ui" },
      view: "code",
      whoami: null,
    });
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("a first visit to an empty install offers no add and names the operator's route", async () => {
    useStore.setState({
      whoami: { authenticated: true, subject: "op", org: "dev", roles: ["PLATFORM_ADMIN"], namespaces: ["*"] },
    });
    stubNetwork(() => [UNSANDBOXED]);
    await mount();
    expect(await screen.findByText(/Ask the operator to list them in LOOM_CODE_REPOS_JSON/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Add repository/ })).toBeNull();
  });

  it("names the operator's setting to a platform admin only", async () => {
    useStore.setState({
      whoami: { authenticated: true, subject: "n@x", org: "dev", roles: ["WRITER"], namespaces: ["default"] },
    });
    stubNetwork(() => [UNSANDBOXED]);
    await mount();
    expect(await screen.findByText(/Ask the operator to add them\./)).toBeTruthy();
    expect(screen.queryByText(/LOOM_CODE_REPOS_JSON/)).toBeNull();
  });

  it("with the sandbox required, the empty state does not offer LOOM_CODE_REPOS_JSON", async () => {
    stubNetwork(() => [{ ...UNSANDBOXED, severity: "error" }]);
    await mount();
    expect(await screen.findByText(/until its repository-job sandbox runs/)).toBeTruthy();
    expect(screen.queryByText(/LOOM_CODE_REPOS_JSON/)).toBeNull();
  });

  it("a refused add reads health again and hides the add", async () => {
    let problems: unknown[] = [];
    stubNetwork(() => problems);
    await mount();
    fireEvent.click(screen.getAllByRole("button", { name: /Add repository/ })[0]);
    fireEvent.change(await screen.findByLabelText("Repository"), { target: { value: "acme/app" } });
    problems = [UNSANDBOXED];
    fireEvent.click(screen.getByRole("button", { name: /Add and start indexing/ }));
    await waitFor(() => expect(screen.queryByText(/Ask the operator to add them/)).toBeTruthy());
    expect(screen.queryByRole("button", { name: /^Add repository$/ })).toBeNull();
  });
});
