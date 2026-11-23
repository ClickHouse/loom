import { describe, expect, it } from "vitest";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import {
  ADMIN_BASE,
  ADMIN_GROUPS,
  MAIN,
  RAIL_GROUPS,
  SETTINGS_ITEM,
  adminItems,
  groupAdminItems,
  isDrawerView,
  operatorItems,
  railGroups,
  reviewNavItem,
  scopeHint,
  type NavItem,
} from "./Sidebar";
import { mobileMoreItems } from "./MobileMore";
import { ALL_VIEWS, type AdminBadges } from "../lib/state";
import { must } from "../test/must";

// Flow (route id "sharing") is an operator's view of the whole namespace — it lives in
// the admin drawer, not the end-user rail. The rail is three labelled groups (Memory,
// Activity, Sources) under Profile, Settings at the foot; Recall is a tab of Memories,
// Namespaces in your org is reached from the namespace picker; the drawer reads as Review /
// Observe / Access and gains Guardrails.

const admin = { authenticated: true, subject: "op@example.com", org: "dev", roles: ["PLATFORM_ADMIN"] };
const writer = { authenticated: true, subject: "w@example.com", org: "dev", roles: ["WRITER"] };
const NS = "team-a";
const row = (role: MyNamespaceEntry["role"]): MyNamespaceEntry => ({
  namespace: NS,
  role,
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
});

describe("Flow in the admin drawer", () => {
  it("is an admin drawer item, not a rail item, under the same label and id", () => {
    expect(MAIN.map((m) => m.id)).not.toContain("sharing");
    const flow = ADMIN_BASE.find((m) => m.id === "sharing");
    expect(flow?.label).toBe("Flow");
  });

  it("opens the drawer on a deep link, like an admin: view", () => {
    expect(isDrawerView("sharing")).toBe(true);
    expect(isDrawerView("admin:activity")).toBe(true);
    expect(isDrawerView("runs")).toBe(false);
  });

  it("sits in the phone sheet's Admin group for admins and is absent for a writer", () => {
    const forAdmin = mobileMoreItems(admin, [row("admin")], NS, null);
    const adminGroup = forAdmin.find((g) => g.group === "Admin");
    expect(adminGroup?.items.map((i) => i.id)).toContain("sharing");
    expect(forAdmin.find((g) => g.group === "Main")?.items.map((i) => i.id) ?? []).not.toContain("sharing");
    const forWriter = mobileMoreItems(writer, [row("writer")], NS, null).flatMap((g) => g.items.map((i) => i.id));
    expect(forWriter).not.toContain("sharing");
  });
});

describe("the rail reads as groups", () => {
  it("is Profile, then Memory / Activity / Sources, with Settings at the foot", () => {
    expect(RAIL_GROUPS.map((g) => g.label)).toEqual([null, "Memory", "Activity", "Sources"]);
    expect(RAIL_GROUPS[0].items.map((i) => i.id)).toEqual(["profile"]);
    expect(RAIL_GROUPS[1].items.map((i) => i.id)).toEqual(["memories", "knowledge", "sessions"]);
    expect(RAIL_GROUPS[2].items.map((i) => i.id)).toEqual(["runs", "tasks"]);
    expect(RAIL_GROUPS[3].items.map((i) => i.id)).toEqual(["code"]);
    expect(SETTINGS_ITEM).toMatchObject({ id: "setup", label: "Settings" });
  });

  it("Recall, Namespaces in your org, Graph and Flow are not rail destinations but stay resolvable views", () => {
    const rail = MAIN.map((m) => m.id);
    for (const v of ["recall", "browse", "graph", "sharing"] as const) {
      expect(rail, v).not.toContain(v);
      expect(ALL_VIEWS, v).toContain(v);
    }
  });

  it("every rail item says what scopes it", () => {
    for (const m of [...MAIN, SETTINGS_ITEM]) expect(scopeHint(m), m.id).not.toBe("");
    const tasks = must(
      MAIN.find((m) => m.id === "tasks"),
      "tasks nav item",
    );
    const memories = must(
      MAIN.find((m) => m.id === "memories"),
      "memories nav item",
    );
    expect(scopeHint(tasks)).toMatch(/every namespace/);
    expect(scopeHint(memories)).toMatch(/picked above/);
  });

  it("the phone sheet inherits the groups and closes with Settings", () => {
    const groups = mobileMoreItems(writer, [row("writer")], NS, null).map((g) => g.group);
    expect(groups).toEqual(["Memory", "Activity", "Sources", "Settings"]);
    const forAdmin = mobileMoreItems(admin, [row("admin")], NS, null).map((g) => g.group);
    expect(forAdmin).toEqual(["Memory", "Activity", "Sources", "Settings", "Admin"]);
  });
});

describe("the admin drawer reads as workflows", () => {
  it("is Review / Observe, with Guardrails now in the drawer", () => {
    expect(ADMIN_GROUPS.map((g) => g.label)).toEqual(["Review", "Observe"]);
    expect(ADMIN_GROUPS[0].items.map((i) => i.id)).toEqual(["admin:guardrails", "admin:policies"]);
    expect(ADMIN_GROUPS[1].items.map((i) => i.id)).toEqual([
      "admin:health",
      "admin:activity",
      "sharing",
      "admin:usage",
      "admin:agents",
    ]);
    expect(ADMIN_BASE.map((i) => i.id)).toContain("admin:guardrails");
  });

  it("groups what the caller may see: Review queue leads Review while there is one; Access holds Users only when visible", () => {
    const visible: NavItem[] = [
      { id: "admin:review_queue", label: "Review queue", icon: ADMIN_BASE[0].icon, badge: 3 },
      ...ADMIN_BASE,
    ];
    const groups = groupAdminItems(visible);
    expect(groups.map((g) => g.label)).toEqual(["Review", "Observe"]);
    expect(groups[0].items.map((i) => i.id)).toEqual(["admin:review_queue", "admin:guardrails", "admin:policies"]);
    const withUsers = groupAdminItems([...ADMIN_BASE, { id: "admin:users", label: "Users", icon: ADMIN_BASE[0].icon }]);
    expect(withUsers.map((g) => g.label)).toEqual(["Review", "Observe", "Access"]);
    expect(withUsers[2].items.map((i) => i.id)).toEqual(["admin:users"]);
    expect(groupAdminItems([])).toEqual([]);
  });
});

describe("operator entries", () => {
  const who = (roles: string[]) => ({ authenticated: true, roles }) as never;
  it("open Users to an org admin and Deployment settings to a platform admin alone", () => {
    expect(operatorItems(who(["org_admin"])).map((i) => i.id)).toEqual(["admin:users"]);
    expect(operatorItems(who(["platform_admin"])).map((i) => i.id)).toEqual(["admin:users", "admin:settings"]);
    expect(operatorItems(who(["admin"]))).toEqual([]);
  });
  it("file Deployment settings under Deployment, for every org", () => {
    const groups = groupAdminItems(operatorItems(who(["platform_admin"])));
    expect(groups.map((g) => g.label)).toEqual(["Access", "Deployment"]);
    expect(scopeHint(must(groups[1]?.items[0], "the settings entry"))).toBe("for every org on this Loom");
    expect(isDrawerView("admin:settings")).toBe(true);
  });
});

describe("the Review queue's stable rail entry", () => {
  const badges = (conflicts: number | null, pendingProposals: number | null): AdminBadges => ({
    conflicts,
    pendingProposals,
    autoResolved: false,
    overCap: false,
    newAgents: 0,
  });

  it("sits in the Memory group for any role that may view review work, whatever the counts", () => {
    for (const who of [admin, writer, { ...writer, roles: ["VIEWER"] } as WhoAmI]) {
      for (const role of ["admin", "writer", "viewer"] as const) {
        const groups = railGroups(who, [row(role)], NS, null);
        expect(groups.map((g) => g.label)).toEqual([null, "Memory", "Activity", "Sources"]);
        expect(groups[1].items.map((i) => i.id)).toEqual(["memories", "knowledge", "sessions", "admin:review_queue"]);
      }
    }
    // The static arrays the phone's primary tabs read are unchanged.
    expect(MAIN.map((m) => m.id)).not.toContain("admin:review_queue");
  });

  it("is absent when the role is unknown, and on a non-memory namespace", () => {
    expect(railGroups(writer, null, NS, null).flatMap((g) => g.items.map((i) => i.id))).not.toContain(
      "admin:review_queue",
    );
    expect(railGroups(writer, [row("writer")], "", null).flatMap((g) => g.items.map((i) => i.id))).not.toContain(
      "admin:review_queue",
    );
    // A collection name is not a memory namespace.
    expect(
      railGroups(writer, [row("writer")], "collection:docs", null).flatMap((g) => g.items.map((i) => i.id)),
    ).not.toContain("admin:review_queue");
  });

  it("the phone's More sheet gets the same entry, in its Memory group, badge and all", () => {
    const sheet = mobileMoreItems(writer, [row("viewer")], NS, badges(1, 2));
    const memory = sheet.find((g) => g.group === "Memory");
    const review = memory?.items.find((i) => i.id === "admin:review_queue");
    expect(review).toMatchObject({ badge: 3, badgeLabel: "3 items to review: 1 conflict, 2 pending proposals" });
    expect(sheet.find((g) => g.group === "Admin")).toBeUndefined();
  });

  it("a proposal-only count links straight to the proposals tab", () => {
    const item = reviewNavItem(writer, [row("writer")], NS, badges(0, 3));
    expect(item).toMatchObject({
      id: "admin:review_queue",
      badge: 3,
      badgeLabel: "3 items to review: 3 pending proposals",
      query: { tab: "proposals" },
    });
    // Conflicts present: the plain queue link, no tab override.
    expect(reviewNavItem(writer, [row("writer")], NS, badges(2, 3))?.query).toBeUndefined();
    // A failed count is unknown: no badge number, no tab override, entry still there.
    const partial = reviewNavItem(writer, [row("writer")], NS, badges(null, 3));
    expect(partial?.badge).toBe(3);
    expect(partial?.badgeLabel).toBe("at least 3 items to review: 3 pending proposals");
    expect(partial?.query).toBeUndefined();
    expect(reviewNavItem(writer, [row("writer")], NS, null)).toMatchObject({ id: "admin:review_queue" });
  });
});

describe("adminItems counts pending proposals toward the Review queue", () => {
  const badges = (conflicts: number | null, pendingProposals: number | null, autoResolved = false): AdminBadges => ({
    conflicts,
    pendingProposals,
    autoResolved,
    overCap: false,
    newAgents: 0,
  });

  it("leads with the Review entry for pending proposals alone, badge and label per kind", () => {
    const items = adminItems(admin, [row("admin")], NS, badges(0, 3));
    expect(items[0]).toMatchObject({
      id: "admin:review_queue",
      badge: 3,
      badgeLabel: "3 items to review: 3 pending proposals",
      query: { tab: "proposals" },
    });
  });

  it("adds conflicts and proposals into one labeled count", () => {
    const items = adminItems(admin, [row("admin")], NS, badges(2, 3));
    expect(items[0]?.badge).toBe(5);
    expect(items[0]?.badgeLabel).toBe("5 items to review: 2 conflicts, 3 pending proposals");
    expect(items[0]?.query).toBeUndefined();
  });

  it("a failed count makes the sum partial, labeled 'at least', never a false zero", () => {
    const items = adminItems(admin, [row("admin")], NS, badges(2, null));
    expect(items[0]?.badge).toBe(2);
    expect(items[0]?.badgeLabel).toBe("at least 2 items to review: 2 conflicts");
    // Both failed: no badge, but the entry still appears only via auto-resolution; with
    // nothing known and nothing auto-resolved, no entry.
    expect(adminItems(admin, [row("admin")], NS, badges(null, null))).not.toContainEqual(
      expect.objectContaining({ id: "admin:review_queue" }),
    );
  });

  it("auto-resolved history alone still surfaces the entry, without a number", () => {
    const items = adminItems(admin, [row("admin")], NS, badges(0, 0, true));
    expect(items[0]).toMatchObject({ id: "admin:review_queue", badge: undefined, badgeLabel: undefined });
  });
});
