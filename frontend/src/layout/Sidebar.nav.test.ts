import { describe, expect, it } from "vitest";
import type { MyNamespaceEntry } from "../api";
import {
  ADMIN_BASE,
  ADMIN_GROUPS,
  MAIN,
  RAIL_GROUPS,
  SETTINGS_ITEM,
  groupAdminItems,
  isDrawerView,
  operatorItems,
  scopeHint,
  type NavItem,
} from "./Sidebar";
import { mobileMoreItems } from "./MobileNav";
import { ALL_VIEWS } from "../lib/state";
import { must } from "../test/must";

// Flow (route id "sharing") is an operator's view of the whole namespace — it lives in
// the admin drawer, not the end-user rail. The rail is three labelled groups (Memory,
// Activity, Sources) under Profile, Settings at the foot; Recall is a tab of Memories,
// Spaces lives in the namespace picker; the drawer reads as Review / Observe / Access and
// gains Guardrails.

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
    const forAdmin = mobileMoreItems(admin, [row("admin")], NS);
    const adminGroup = forAdmin.find((g) => g.group === "Admin");
    expect(adminGroup?.items.map((i) => i.id)).toContain("sharing");
    expect(forAdmin.find((g) => g.group === "Main")?.items.map((i) => i.id) ?? []).not.toContain("sharing");
    const forWriter = mobileMoreItems(writer, [row("writer")], NS).flatMap((g) => g.items.map((i) => i.id));
    expect(forWriter).not.toContain("sharing");
  });
});

describe("the rail reads as groups", () => {
  it("is Profile, then Memory / Activity / Sources, with Settings at the foot", () => {
    expect(RAIL_GROUPS.map((g) => g.label)).toEqual([null, "Memory", "Activity", "Sources"]);
    expect(RAIL_GROUPS[0].items.map((i) => i.id)).toEqual(["profile"]);
    expect(RAIL_GROUPS[1].items.map((i) => i.id)).toEqual(["memories", "knowledge", "sessions", "assistant"]);
    expect(RAIL_GROUPS[2].items.map((i) => i.id)).toEqual(["runs", "tasks"]);
    expect(RAIL_GROUPS[3].items.map((i) => i.id)).toEqual(["code"]);
    expect(SETTINGS_ITEM).toMatchObject({ id: "setup", label: "Settings" });
  });

  it("Recall, Spaces, Graph and Flow are not rail destinations but stay resolvable views", () => {
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
    const groups = mobileMoreItems(writer, [row("writer")], NS).map((g) => g.group);
    expect(groups).toEqual(["Memory", "Activity", "Sources", "Settings"]);
    const forAdmin = mobileMoreItems(admin, [row("admin")], NS).map((g) => g.group);
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
