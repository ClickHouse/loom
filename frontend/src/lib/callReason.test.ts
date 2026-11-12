import { describe, expect, it } from "vitest";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import { ROLE_LOADING, callReason, roleGap } from "./callReason";

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
    // A read list with no row and no `*` claim is no grant: nothing to explain.
    expect(roleGap({ role: null, loading: false, rows: [] }, who, "team")).toBeNull();
  });
});

describe("callReason", () => {
  it("names the role a known role lacks", () => {
    expect(
      callReason("memory.pin", { role: "viewer", loading: false, rows: [row("viewer")] }, token([], []), "team"),
    ).toBe("Needs writer on team. An admin of it can grant that.");
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
