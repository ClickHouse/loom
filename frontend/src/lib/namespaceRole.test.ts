import { describe, expect, it } from "vitest";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import { canWrite, roleOnNamespace, canEditGuide } from "./namespaceRole";

// The role chip and the write-CTA gate share this one rule.

const row = (
  namespace: string,
  role: MyNamespaceEntry["role"],
  status: MyNamespaceEntry["status"] = "active",
): MyNamespaceEntry => ({
  namespace,
  role,
  status,
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
});

const user = (roles: string[]): WhoAmI => ({ authenticated: true, subject: "u@example.com", org: "dev", roles });

describe("roleOnNamespace", () => {
  it("is unknown without whoami or a namespace", () => {
    expect(roleOnNamespace(null, [], "team")).toBeNull();
    expect(roleOnNamespace(user(["WRITER"]), [], "")).toBeNull();
  });

  it("auth off: the operator is admin everywhere", () => {
    expect(roleOnNamespace({ authenticated: false }, null, "local")).toBe("admin");
  });

  it("an admin claim wins before list_mine is even read", () => {
    expect(roleOnNamespace(user(["PLATFORM_ADMIN"]), null, "team")).toBe("admin");
    expect(roleOnNamespace(user(["org_admin"]), [row("team", "viewer")], "team")).toBe("admin");
  });

  it("is unknown while list_mine is loading or failed", () => {
    expect(roleOnNamespace(user(["WRITER"]), null, "team")).toBeNull();
  });

  it("takes the grant row's role", () => {
    const mine = [row("team", "viewer"), row("mine", "admin")];
    expect(roleOnNamespace(user(["WRITER"]), mine, "team")).toBe("viewer");
    expect(roleOnNamespace(user(["WRITER"]), mine, "mine")).toBe("admin");
  });

  it("a claim-only namespace takes the token's flat role — the audit's viewer case", () => {
    const mine = [row("reporter-public", "jwt")];
    expect(roleOnNamespace(user(["VIEWER"]), mine, "reporter-public")).toBe("viewer");
    expect(roleOnNamespace(user(["WRITER"]), mine, "reporter-public")).toBe("writer");
    expect(roleOnNamespace(user([]), mine, "reporter-public")).toBeNull();
  });

  it("a pending join or a namespace not in the list is unknown, not viewer", () => {
    expect(roleOnNamespace(user(["WRITER"]), [row("team", "writer", "pending")], "team")).toBeNull();
    expect(roleOnNamespace(user(["WRITER"]), [row("other", "writer")], "team")).toBeNull();
  });
});

describe("canWrite", () => {
  it("locks only a known viewer", () => {
    expect(canWrite("viewer")).toBe(false);
    expect(canWrite("writer")).toBe(true);
    expect(canWrite("admin")).toBe(true);
    expect(canWrite(null)).toBe(true);
  });
});

describe("canEditGuide", () => {
  it("lets a known writer or admin write a namespace's guide", () => {
    expect(canEditGuide("writer", "team")).toBe(true);
    expect(canEditGuide("admin", "personal-alice")).toBe(true);
  });

  it("keeps a viewer, an unknown role and a personal collection reading", () => {
    expect(canEditGuide("viewer", "team")).toBe(false);
    expect(canEditGuide(null, "team")).toBe(false);
    expect(canEditGuide("admin", "col-me-alice")).toBe(false);
  });
});
