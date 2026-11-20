import { describe, expect, it } from "vitest";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import { PAGE_OP_MIN_ROLE, ROLE_LOADING, callReason, codeMember, needsLine, roleGap } from "./callReason";

const row = (role: MyNamespaceEntry["role"], status: MyNamespaceEntry["status"] = "active"): MyNamespaceEntry => ({
  namespace: "team",
  role,
  status,
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
});
const token = (roles: string[], namespaces: string[]): WhoAmI => ({
  authenticated: true,
  subject: "u@example.com",
  org: "dev",
  roles,
  namespaces,
});
const SERVER = "The server decides this token's access to team; it does not show here.";

describe("roleGap", () => {
  it("says why a role is not known yet", () => {
    const who = token(["WRITER"], ["team"]);
    expect(roleGap({ role: null, loading: true, rows: null }, who, "team")).toBe(ROLE_LOADING);
    expect(roleGap({ role: null, loading: false, rows: null }, who, "team")).toBe(
      "Couldn't check your access to team.",
    );
    expect(roleGap({ role: null, loading: false, rows: [row("writer", "pending")] }, who, "team")).toBe(
      "Your request to join team is waiting for an admin.",
    );
    expect(roleGap({ role: null, loading: false, rows: [] }, token(["WRITER"], ["*"]), "team")).toBe(SERVER);
    // A `*` token that also names the namespace and has no row: its claim there is capped.
    expect(roleGap({ role: null, loading: false, rows: [] }, token(["WRITER"], ["*", "team"]), "team")).toBeNull();
    expect(
      callReason("memory.pin", { role: null, loading: false, rows: [] }, token(["WRITER"], ["*", "team"]), "team"),
    ).toBe("Needs writer on team. An admin of it can grant that.");
    // A read list with no row and no `*` claim is no grant: nothing to explain.
    expect(roleGap({ role: null, loading: false, rows: [] }, who, "team")).toBeNull();
  });
});

describe("callReason", () => {
  it("names the role a known role lacks", () => {
    expect(
      callReason("memory.pin", { role: "viewer", loading: false, rows: [row("viewer")] }, token(["USER"], []), "team"),
    ).toBe("Needs writer on team. An admin of it can grant that.");
  });

  it("leaves it to the server for a token scoped per namespace with no direct grant row there", () => {
    const scoped = token([], ["team"]);
    // No row, a claim-only row or an inherited one: no role the client can see.
    expect(callReason("memory.pin", { role: null, loading: false, rows: [] }, scoped, "team")).toBe(SERVER);
    expect(callReason("memory.pin", { role: null, loading: false, rows: [row("jwt")] }, scoped, "team")).toBe(SERVER);
    expect(
      callReason(
        "memory.pin",
        { role: "viewer", loading: false, rows: [{ ...row("viewer"), inherited: true }] },
        scoped,
        "team",
      ),
    ).toBe(SERVER);
    // A direct grant row caps the scoped token's own role (claim_cap, _scoped_role): it is the most.
    expect(callReason("memory.pin", { role: "viewer", loading: false, rows: [row("viewer")] }, scoped, "team")).toBe(
      "Needs writer on team. An admin of it can grant that.",
    );
    // The same with a flat role: the row is all there is.
    expect(
      callReason("memory.pin", { role: null, loading: false, rows: [] }, token(["USER"], ["team"]), "team"),
    ).toMatch(/^Needs writer/);
  });

  it("leaves it to the server when a `*` claim's flat role reaches the op over a lower grant row", () => {
    const read = { role: "viewer" as const, loading: false, rows: [row("viewer")] };
    expect(callReason("memory.pin", read, token(["WRITER"], ["*"]), "team")).toBe(SERVER);
    // A flat role below the op, or a token that names the namespace, keeps "Needs".
    expect(callReason("memory.pin", read, token(["VIEWER"], ["*"]), "team")).toMatch(/^Needs writer/);
    expect(callReason("memory.pin", read, token(["WRITER"], ["*", "team"]), "team")).toMatch(/^Needs writer/);
    expect(callReason("memory.unpin", read, token(["WRITER"], ["*"]), "team")).toMatch(/^Needs admin/);
  });
});

describe("needsLine", () => {
  it("names the role only when it is known to fall short", () => {
    const viewer = { role: "viewer" as const, loading: false, rows: [row("viewer")] };
    expect(needsLine("knowledge.delete", viewer, token(["USER"], []), "team")).toBe(
      "Needs writer on team. An admin of it can grant that.",
    );
    // A `*` writer claim is never capped by a grant row: the server decides.
    expect(needsLine("knowledge.delete", viewer, token(["WRITER"], ["*"]), "team")).toBeNull();
    // Not known yet, or allowed: no line.
    expect(
      needsLine("knowledge.delete", { role: null, loading: true, rows: null }, token(["USER"], []), "team"),
    ).toBeNull();
    expect(
      needsLine(
        "knowledge.delete",
        { role: "writer", loading: false, rows: [row("writer")] },
        token(["USER"], []),
        "team",
      ),
    ).toBeNull();
  });
});

describe("PAGE_OP_MIN_ROLE", () => {
  // The page writes' roles, mirrored from the registry the transport gates on, as canCall.ts's are.
  const generated = new URL("../../../sdk/typescript/src/generated/ops.ts", import.meta.url).pathname;

  it("every entry matches sdk/typescript/src/generated/ops.ts", async () => {
    type Fs = { existsSync(p: string): boolean; readFileSync(p: string, enc: "utf8"): string };
    const fs = (await import(/* @vite-ignore */ "node:" + "fs")) as Fs;
    if (!fs.existsSync(generated)) return; // a frontend-only checkout has no SDK tree
    const text = fs.readFileSync(generated, "utf8");
    for (const [op, min] of Object.entries(PAGE_OP_MIN_ROLE)) {
      const m = new RegExp(`\\{ name: "${op.replace(".", "\\.")}",[^\\n]*? role: "([a-z_]+)"`).exec(text);
      expect(m?.[1], `${op} minimum role drifted from the registry`).toBe(min);
    }
  });
});

describe("codeMember", () => {
  it("counts a live grant on a code namespace, never a claim-only or pending row", () => {
    const code = (role: MyNamespaceEntry["role"], status: MyNamespaceEntry["status"] = "active") => ({
      ...row(role, status),
      kind: "code" as const,
    });
    const read = (r: MyNamespaceEntry) => ({ role: null, loading: false, rows: [r] });
    expect(codeMember(read(code("viewer")), "team")).toBe(true);
    expect(codeMember(read(code("jwt")), "team")).toBe(false);
    expect(codeMember(read(code("viewer", "pending")), "team")).toBe(false);
    expect(codeMember(read(row("viewer")), "team")).toBe(false);
  });
});
