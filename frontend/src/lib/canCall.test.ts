import { afterEach, describe, expect, it, vi } from "vitest";
import { isAuthDisabled, type CodeRepoSummary, type WhoAmI } from "../api";
import type { MyNamespaceEntry } from "../api";
import { OP_MIN_ROLE, canCall, canCallCodeRepos, canCallOn } from "./canCall";
import { must } from "../test/must";

// The dashboard never issues a request the token cannot make; when the role is unknown it
// does not poll.

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  isAuthDisabled: vi.fn(() => false),
}));

const user = (roles: string[]): WhoAmI => ({ authenticated: true, subject: "u@example.com", org: "dev", roles });

describe("canCall", () => {
  it("an unknown role never sends a request — the token-only principal's case", () => {
    expect(canCall("conflicts.list", null, user(["VIEWER"]))).toBe(false);
    expect(canCall("cost.budget_status", null, user(["WRITER"]))).toBe(false);
    expect(canCall("agents.connected", null, null)).toBe(false);
  });

  it("a known role at or above the registry minimum may call", () => {
    expect(canCall("conflicts.list", "viewer", user(["VIEWER"]))).toBe(true);
    expect(canCall("cost.budget_status", "writer", user(["WRITER"]))).toBe(true);
    expect(canCall("audit.list", "admin", user(["WRITER"]))).toBe(true);
  });

  it("an admin claim, or auth off, opens everything", () => {
    expect(canCall("conflicts.list", null, user(["ADMIN"]))).toBe(true);
    expect(canCall("admin/workers.status", null, user(["org_admin"]))).toBe(true);
    expect(canCall("admin/workers.status", null, { authenticated: false })).toBe(true);
  });

  it("revision 3: the blacklist ops gate on the role the registry names", () => {
    // Setup's "Don't save these" panel — the deploy-delta's ten
    // AUTH_INSUFFICIENT from a viewer token with no memory namespace.
    expect(canCall("blacklist.list", null, user(["VIEWER"]))).toBe(false);
    expect(canCall("blacklist.list", "viewer", user(["VIEWER"]))).toBe(true);
    expect(canCall("blacklist.add", "viewer", user(["VIEWER"]))).toBe(false);
    expect(canCall("blacklist.add", "writer", user(["WRITER"]))).toBe(true);
    expect(canCall("blacklist.remove", "writer", user(["WRITER"]))).toBe(false);
    expect(canCall("blacklist.remove", "admin", user(["WRITER"]))).toBe(true);
    expect(canCall("blacklist.remove", null, user(["ADMIN"]))).toBe(true);
    expect(canCall("memory.list", null, user(["VIEWER"]))).toBe(false);
    expect(canCall("memory.list", "viewer", user(["VIEWER"]))).toBe(true);
  });

  it("a claim-gated /v1/admin/* handler ignores the namespace grant", () => {
    // require_admin reads the JWT roles claim and nothing else: an admin
    // grant row on the namespace does not open workers.status.
    expect(canCall("admin/workers.status", "admin", user(["WRITER"]))).toBe(false);
  });
});

describe("canCallOn: an op on one repository row's namespace", () => {
  afterEach(() => vi.mocked(isAuthDisabled).mockReturnValue(false));
  type Row = Pick<CodeRepoSummary, "org" | "namespace" | "my_role" | "my_status" | "classification">;
  const row = (over: Partial<Row> = {}): Row => ({ org: "dev", namespace: "code-x", my_role: "", ...over });
  const token = (roles: string[], namespaces: string[], org = "dev"): WhoAmI => ({
    authenticated: true,
    subject: "u@example.com",
    org,
    namespaces,
    roles,
  });

  it("a standard namespace: an admin claim, or the grant the row states", () => {
    expect(canCallOn("knowledge.source.add", row(), token(["ADMIN"], ["*"]))).toBe(true);
    expect(canCallOn("knowledge.source.add", row({ my_role: "admin" }), token(["WRITER"], []))).toBe(true);
    expect(canCallOn("knowledge.source.add", row({ my_role: "writer" }), token(["WRITER"], []))).toBe(false);
    expect(canCallOn("knowledge.add", row({ my_role: "writer" }), token(["VIEWER"], []))).toBe(true);
    expect(canCallOn("knowledge.delete", row({ my_role: "viewer" }), token(["VIEWER"], ["*"]))).toBe(false);
    expect(canCallOn("knowledge.add", row(), null)).toBe(false);
    expect(canCallOn("knowledge.add", row({ my_role: "writer" }), null)).toBe(true);
  });

  it("a claim counts only on a token covering the namespace, and a named one is held to its grant", () => {
    expect(canCallOn("knowledge.source.add", row(), token(["ADMIN"], ["code-x"]))).toBe(true);
    expect(canCallOn("knowledge.source.add", row(), token(["ADMIN"], ["team-x"]))).toBe(false);
    expect(canCallOn("knowledge.source.add", row(), token(["ADMIN"], ["*"], "other"))).toBe(false);
    expect(canCallOn("knowledge.source.add", row(), token(["ADMIN"], ["*"], "*"))).toBe(true);
    const viewer = row({ my_role: "viewer", my_status: "member" });
    expect(canCallOn("knowledge.source.add", viewer, token(["ADMIN"], ["code-x"]))).toBe(false);
    expect(canCallOn("knowledge.source.add", viewer, token(["ADMIN"], ["*"]))).toBe(true);
    const pending = row({ my_status: "pending" });
    expect(canCallOn("knowledge.add", pending, token(["WRITER"], ["code-x"]))).toBe(false);
  });

  it("a restricted namespace: the grant, and a token of its org naming it whose claim reaches", () => {
    const r = (my_role: Row["my_role"]) => row({ classification: "restricted", my_role });
    expect(canCallOn("knowledge.source.add", r("admin"), token(["ADMIN"], ["code-x"]))).toBe(true);
    expect(canCallOn("knowledge.source.add", r(""), token(["PLATFORM_ADMIN"], ["code-x"]))).toBe(false);
    expect(canCallOn("knowledge.source.add", r("admin"), token(["ADMIN"], ["*"]))).toBe(false);
    expect(canCallOn("knowledge.source.add", r("admin"), token(["ADMIN"], ["code-x"], "*"))).toBe(false);
    expect(canCallOn("knowledge.source.add", r("admin"), token(["WRITER"], ["code-x"]))).toBe(false);
    expect(canCallOn("knowledge.add", r("admin"), token(["WRITER"], ["code-x"]))).toBe(true);
    expect(canCallOn("knowledge.add", r("viewer"), token(["ADMIN"], ["code-x"]))).toBe(false);
    expect(canCallOn("knowledge.add", r("writer"), null)).toBe(false);
  });

  it("auth off: every op, restricted or not", () => {
    vi.mocked(isAuthDisabled).mockReturnValue(true);
    expect(canCallOn("knowledge.source.remove", row(), null)).toBe(true);
    expect(canCallOn("knowledge.source.remove", row({ classification: "restricted" }), null)).toBe(true);
  });
});

describe("canCallCodeRepos", () => {
  const row = (
    namespace: string,
    kind: MyNamespaceEntry["kind"],
    status: "active" | "pending" = "active",
  ): MyNamespaceEntry => ({
    namespace,
    role: "jwt",
    status,
    visibility: "public",
    description: "",
    joined_at: null,
    is_new: false,
    kind,
  });

  it("unknown rows, or none of code kind, send nothing", () => {
    expect(canCallCodeRepos(null, user(["VIEWER"]))).toBe(false);
    expect(canCallCodeRepos([], user(["WRITER"]))).toBe(false);
    expect(canCallCodeRepos([row("team-x", "memory"), row("tracker-clickhouse", "qa")], user(["VIEWER"]))).toBe(false);
    expect(canCallCodeRepos([row("code-clickhouse", "code", "pending")], user(["VIEWER"]))).toBe(false);
  });

  it("an active code-kind row, an admin claim, or auth off reads repositories", () => {
    expect(canCallCodeRepos([row("team-x", "memory"), row("code-clickhouse", "code")], user(["VIEWER"]))).toBe(true);
    expect(canCallCodeRepos(null, user(["ADMIN"]))).toBe(true);
    expect(canCallCodeRepos([], { authenticated: false })).toBe(true);
  });
});

describe("OP_MIN_ROLE mirrors the registry", () => {
  it("carries every op the dashboard reads on mount", () => {
    for (const op of [
      "blacklist.list",
      "blacklist.add",
      "blacklist.remove",
      "memory.list",
      "memory.get",
      "memory.bulk_get",
      "memory.history",
      "memory.related",
      "memory.lineage",
      "memory.provenance",
      "memory.search_trace_log",
      "conversation.threads",
      "conversation.list",
      "conversation.set_logging",
      "namespace.get",
      "namespace.taxonomy",
      "knowledge.list",
      "facts.list",
      "run.list",
      "run.get",
      "grants.list",
      "learnings.list",
      "policy.decisions",
      "consolidations.list",
      "conflicts.allowlist_list",
    ]) {
      expect(OP_MIN_ROLE, `${op} missing`).toHaveProperty(op);
    }
  });

  // scripts/codegen/sdk_ts_gen.py writes every op's `required_role` into the TS
  // SDK's generated table from the same registry the transport gates on.
  const generated = new URL("../../../sdk/typescript/src/generated/ops.ts", import.meta.url).pathname;

  it("every namespace-gated entry matches sdk/typescript/src/generated/ops.ts", async () => {
    // vitest runs on node; the frontend tsconfig carries no node types, so
    // the module is reached through a computed specifier. A computed-specifier
    // dynamic import is untyped (Promise<any>), so the cast names the two fs
    // calls this test uses.
    type Fs = { existsSync(p: string): boolean; readFileSync(p: string, enc: "utf8"): string };
    const fs = (await import(/* @vite-ignore */ "node:" + "fs")) as Fs;
    if (!fs.existsSync(generated)) return; // a frontend-only checkout has no SDK tree
    const text = fs.readFileSync(generated, "utf8");
    for (const [op, gate] of Object.entries(OP_MIN_ROLE)) {
      if (gate.by !== "namespace") continue; // /v1/admin/* handlers are not registry ops
      const m = new RegExp(`\\{ name: "${op.replace(".", "\\.")}",[^\\n]*? role: "([a-z_]+)"`).exec(text);
      expect(must(m, `${op} is not in the generated op table`)[1], `${op} minimum role drifted from the registry`).toBe(
        gate.min,
      );
    }
  });
});
