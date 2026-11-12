import { describe, expect, it } from "vitest";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import { hasAdminClaim, hasOrgAdminClaim, hasPlatformAdminClaim, holdsAdminGrant, isNamespaceAdmin } from "./role";
import { canUpdateNamespace, restrictedMisses } from "./namespaceUpdate";

// Who sees the Admin drawer and who may call /v1/admin/*.

const viewerJwt: WhoAmI = {
  authenticated: true,
  subject: "v@example.com",
  org: "dev",
  roles: ["VIEWER"],
  namespaces: ["team-a"],
};
const writerJwt: WhoAmI = { ...viewerJwt, roles: ["WRITER"] };
const adminJwt: WhoAmI = { ...viewerJwt, roles: ["ADMIN"] };
const platform: WhoAmI = { ...viewerJwt, roles: ["PLATFORM_ADMIN"] };
const authOff: WhoAmI = { authenticated: false };

const row = (namespace: string, role: MyNamespaceEntry["role"], status?: "active" | "pending"): MyNamespaceEntry => ({
  namespace,
  role,
  status,
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
});

describe("hasAdminClaim (the /v1/admin/* gate)", () => {
  it("is the JWT roles claim reaching ADMIN, case-insensitively", () => {
    expect(hasAdminClaim(adminJwt)).toBe(true);
    expect(hasAdminClaim(platform)).toBe(true);
    expect(hasAdminClaim({ ...viewerJwt, roles: ["org_admin"] })).toBe(true);
    expect(hasAdminClaim(writerJwt)).toBe(false);
    expect(hasAdminClaim(viewerJwt)).toBe(false);
    expect(hasAdminClaim(null)).toBe(false);
  });

  it("treats LOOM_AUTH_REQUIRED=false as the operator", () => {
    expect(hasAdminClaim(authOff)).toBe(true);
  });
});

describe("hasOrgAdminClaim (admin.users, ops.health, claim_release, org-wide audit / cost)", () => {
  it("is the JWT roles claim reaching ORG_ADMIN, case-insensitively", () => {
    expect(hasOrgAdminClaim({ ...viewerJwt, roles: ["ORG_ADMIN"] })).toBe(true);
    expect(hasOrgAdminClaim({ ...viewerJwt, roles: ["org_admin"] })).toBe(true);
    expect(hasOrgAdminClaim(platform)).toBe(true);
    expect(hasOrgAdminClaim({ ...viewerJwt, roles: ["platform_admin"] })).toBe(true);
  });

  it("an ADMIN claim, a lower claim or an unknown whoami does not reach it", () => {
    expect(hasOrgAdminClaim(adminJwt)).toBe(false);
    expect(hasOrgAdminClaim(writerJwt)).toBe(false);
    expect(hasOrgAdminClaim({ ...viewerJwt, roles: ["tenant_admin_viewer"] })).toBe(false);
    expect(hasOrgAdminClaim(null)).toBe(false);
  });

  it("treats LOOM_AUTH_REQUIRED=false as the operator", () => {
    expect(hasOrgAdminClaim(authOff)).toBe(true);
  });
});

describe("hasPlatformAdminClaim (Settings → Models & keys)", () => {
  it("is the JWT roles claim naming PLATFORM_ADMIN, case-insensitively", () => {
    expect(hasPlatformAdminClaim(platform)).toBe(true);
    expect(hasPlatformAdminClaim({ ...viewerJwt, roles: ["platform_admin"] })).toBe(true);
  });

  it("an ORG_ADMIN claim, a lower claim or an unknown whoami does not reach it", () => {
    expect(hasPlatformAdminClaim({ ...viewerJwt, roles: ["ORG_ADMIN"] })).toBe(false);
    expect(hasPlatformAdminClaim(adminJwt)).toBe(false);
    expect(hasPlatformAdminClaim(null)).toBe(false);
  });

  it("treats LOOM_AUTH_REQUIRED=false as the operator", () => {
    expect(hasPlatformAdminClaim(authOff)).toBe(true);
  });
});

describe("isNamespaceAdmin (the Admin drawer)", () => {
  it("is false for a viewer or writer grant", () => {
    expect(isNamespaceAdmin(viewerJwt, [row("team-a", "viewer")], "team-a")).toBe(false);
    expect(isNamespaceAdmin(writerJwt, [row("team-a", "writer")], "team-a")).toBe(false);
  });

  it("is true for an admin grant on the current namespace only", () => {
    const mine = [row("team-a", "admin"), row("team-b", "viewer")];
    expect(isNamespaceAdmin(writerJwt, mine, "team-a")).toBe(true);
    expect(isNamespaceAdmin(writerJwt, mine, "team-b")).toBe(false);
    expect(isNamespaceAdmin(writerJwt, mine, "team-c")).toBe(false);
  });

  it("ignores a pending admin request and a JWT-claim-only namespace", () => {
    expect(isNamespaceAdmin(viewerJwt, [row("team-a", "admin", "pending")], "team-a")).toBe(false);
    expect(isNamespaceAdmin(viewerJwt, [row("team-a", "jwt")], "team-a")).toBe(false);
  });

  it("is true on an admin claim regardless of grants", () => {
    expect(isNamespaceAdmin(adminJwt, [row("team-a", "viewer")], "team-a")).toBe(true);
    expect(isNamespaceAdmin(platform, [], "anything")).toBe(true);
    expect(isNamespaceAdmin(authOff, null, "local")).toBe(true);
  });

  it("treats an unknown grant list as unknown, not as no access", () => {
    expect(isNamespaceAdmin(writerJwt, null, "team-a")).toBe(true);
    expect(isNamespaceAdmin(null, null, "team-a")).toBe(false);
  });
});

describe("holdsAdminGrant (grants.* from the namespace's own grant list)", () => {
  it("is an active admin grant or an admin claim, case-insensitively", () => {
    expect(holdsAdminGrant(writerJwt, { role: "admin", status: "active" })).toBe(true);
    expect(holdsAdminGrant(writerJwt, { role: "admin", status: "pending" })).toBe(false);
    expect(holdsAdminGrant(writerJwt, { role: "writer", status: "active" })).toBe(false);
    expect(holdsAdminGrant(writerJwt, undefined)).toBe(false);
    expect(holdsAdminGrant({ ...viewerJwt, roles: ["org_admin"] }, undefined)).toBe(true);
    expect(holdsAdminGrant(authOff, undefined)).toBe(true);
  });
});

describe("canUpdateNamespace (namespace.register's update path)", () => {
  const autoRow = { owner_email: "v@example.com", tags: ["auto-registered"] };

  it("admits the owner of an auto-registered row, who holds no grant", () => {
    expect(canUpdateNamespace(writerJwt, [], { org: "dev", namespace: "team-a" }, autoRow)).toBe(true);
    expect(isNamespaceAdmin(writerJwt, [], "team-a")).toBe(false);
  });

  it("needs both the tag and the owner match", () => {
    expect(canUpdateNamespace(writerJwt, [], { org: "dev", namespace: "team-a" }, { ...autoRow, tags: [] })).toBe(
      false,
    );
    expect(
      canUpdateNamespace(
        writerJwt,
        [],
        { org: "dev", namespace: "team-a" },
        { ...autoRow, owner_email: "other@example.com" },
      ),
    ).toBe(false);
    expect(canUpdateNamespace(writerJwt, [], { org: "dev", namespace: "team-a" }, null)).toBe(false);
    expect(
      canUpdateNamespace(
        { ...writerJwt, subject: undefined },
        [],
        { org: "dev", namespace: "team-a" },
        { ...autoRow, owner_email: "" },
      ),
    ).toBe(false);
  });

  it("is true for a namespace admin whatever the row says", () => {
    expect(canUpdateNamespace(writerJwt, [row("team-a", "admin")], { org: "dev", namespace: "team-a" }, null)).toBe(
      true,
    );
    expect(canUpdateNamespace(authOff, null, { org: "dev", namespace: "local" }, null)).toBe(true);
  });
});

describe("canUpdateNamespace on a restricted row (the two-factor gate)", () => {
  const restricted = { owner_email: "v@example.com", tags: ["auto-registered"], classification: "restricted" as const };
  const namedAdmin: WhoAmI = { ...adminJwt, namespaces: ["team-a"] };

  it("admits a token that names the namespace, carries ADMIN and holds a direct admin grant", () => {
    expect(
      canUpdateNamespace(namedAdmin, [row("team-a", "admin")], { org: "dev", namespace: "team-a" }, restricted),
    ).toBe(true);
    expect(
      canUpdateNamespace(authOff, null, { org: "dev", namespace: "local" }, { ...restricted, owner_email: "" }),
    ).toBe(true);
  });

  it("refuses the flat admin claim on a wildcard token, whatever the grants", () => {
    const wildcard: WhoAmI = { ...adminJwt, namespaces: ["*"] };
    expect(
      canUpdateNamespace(wildcard, [row("team-a", "admin")], { org: "dev", namespace: "team-a" }, restricted),
    ).toBe(false);
    expect(
      canUpdateNamespace(
        { ...namedAdmin, org: "*" },
        [row("team-a", "admin")],
        { org: "dev", namespace: "team-a" },
        restricted,
      ),
    ).toBe(false);
    expect(canUpdateNamespace(platform, [], { org: "dev", namespace: "team-a" }, restricted)).toBe(false);
  });

  it("refuses the auto-registered owner and an admin grant under a lower token role", () => {
    expect(canUpdateNamespace(writerJwt, [], { org: "dev", namespace: "team-a" }, restricted)).toBe(false);
    expect(
      canUpdateNamespace(writerJwt, [row("team-a", "admin")], { org: "dev", namespace: "team-a" }, restricted),
    ).toBe(false);
    expect(
      canUpdateNamespace(namedAdmin, [row("team-a", "viewer")], { org: "dev", namespace: "team-a" }, restricted),
    ).toBe(false);
  });

  it("refuses a token of another org, and names each part the gate misses", () => {
    expect(
      canUpdateNamespace(
        { ...namedAdmin, org: "acme" },
        [row("team-a", "admin")],
        { org: "dev", namespace: "team-a" },
        restricted,
      ),
    ).toBe(false);
    expect(
      restrictedMisses({ ...namedAdmin, org: "acme" }, [row("team-a", "admin")], { org: "dev", namespace: "team-a" }),
    ).toEqual(["org"]);
    expect(restrictedMisses(writerJwt, [], { org: "dev", namespace: "team-b" })).toEqual(["name", "claim", "grant"]);
    expect(restrictedMisses(namedAdmin, [row("team-a", "admin")], { org: "dev", namespace: "team-a" })).toEqual([]);
  });

  it("refuses an inherited, pending or unknown grant", () => {
    expect(
      canUpdateNamespace(
        namedAdmin,
        [{ ...row("team-a", "admin"), inherited: true }],
        { org: "dev", namespace: "team-a" },
        restricted,
      ),
    ).toBe(false);
    expect(
      canUpdateNamespace(
        namedAdmin,
        [row("team-a", "admin", "pending")],
        { org: "dev", namespace: "team-a" },
        restricted,
      ),
    ).toBe(false);
    expect(canUpdateNamespace(namedAdmin, null, { org: "dev", namespace: "team-a" }, restricted)).toBe(false);
    expect(canUpdateNamespace(null, null, { org: "dev", namespace: "team-a" }, restricted)).toBe(false);
  });

  it("leaves a standard or unclassified row on the ordinary rule", () => {
    const wildcard: WhoAmI = { ...adminJwt, namespaces: ["*"] };
    expect(
      canUpdateNamespace(
        wildcard,
        [],
        { org: "dev", namespace: "team-a" },
        { ...restricted, classification: "standard" },
      ),
    ).toBe(true);
    expect(
      canUpdateNamespace(
        wildcard,
        [],
        { org: "dev", namespace: "team-a" },
        { ...restricted, classification: undefined },
      ),
    ).toBe(true);
  });
});
