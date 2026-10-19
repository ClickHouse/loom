import { vi } from "vitest";
import type { CodeRepoSummary } from "../api";

/** One indexed branch the caller reads, as `code.repos` lists it. */
export const codeRepoFixture: CodeRepoSummary = {
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

/** Answers the Repositories page's reads: `code.repos` with what `repos` returns at the
 *  time of the call (null answers 503), a healthy server, each path in `answers` with its
 *  body, and empty lists for the rest. */
export function stubCodeFetch(repos: () => CodeRepoSummary[] | null, answers: Record<string, unknown> = {}) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      const path = url.replace(/^https?:\/\/[^/]+/, "").split("?")[0];
      const json = (body: unknown, status = 200) =>
        new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
      if (path === "/v1/code.repos") {
        const rows = repos();
        return rows ? json({ repos: rows }) : json({ detail: "down" }, 503);
      }
      if (path === "/v1/health") return json({ status: "ok", problems: [] });
      if (path in answers) return json(answers[path]);
      return json({ repos: [], rows: [], entries: [], sources: [], namespaces: [] });
    }),
  );
}
