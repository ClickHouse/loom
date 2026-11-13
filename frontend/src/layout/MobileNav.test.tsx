import { act, render, screen, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import type { MyNamespaceEntry } from "../api";
import { useStore, viewHref } from "../lib/state";
import { useDock } from "../lib/assistantDock";
import { MAIN, ADMIN_BASE, ADMIN_OPERATOR_ONLY } from "./Sidebar";
import { MobileNav } from "./MobileNav";
import { mobileMoreItems } from "./MobileMore";

// The phone's "More" sheet is generated from the sidebar's nav arrays, so every route the
// sidebar offers is reachable on a phone under the same label.

const admin = { authenticated: true, subject: "op@example.com", org: "dev", roles: ["PLATFORM_ADMIN"] };
const writer = { authenticated: true, subject: "w@example.com", org: "dev", roles: ["WRITER"] };
const NS = "team-a";
const identity = { org: "dev", namespace: NS, agent: "loom-ui" };
const row = (role: MyNamespaceEntry["role"]): MyNamespaceEntry => ({
  namespace: NS,
  role,
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
});

beforeEach(() => {
  useStore.setState({ view: "memories", whoami: null, identity, myNamespaces: null });
});

describe("mobileMoreItems", () => {
  it("lists every sidebar route: MAIN, the admin drawer and Setup", () => {
    const ids = mobileMoreItems(admin, null, NS, null).flatMap((g) => g.items.map((i) => i.id));
    const primary = ["memories", "knowledge"];
    const expected = [
      ...MAIN.map((m) => m.id),
      ...ADMIN_BASE.map((m) => m.id),
      ...ADMIN_OPERATOR_ONLY.map((m) => m.id),
      "setup",
    ];
    for (const id of expected) {
      if (primary.includes(id)) continue;
      expect(ids, `sidebar route ${id} is missing from the phone nav`).toContain(id);
    }
  });

  it("uses the sidebar's labels verbatim", () => {
    const labels = new Map(
      mobileMoreItems(admin, null, NS, null).flatMap((g) => g.items.map((i) => [i.id, i.label] as const)),
    );
    for (const m of [...MAIN, ...ADMIN_BASE]) {
      if (["memories", "knowledge"].includes(m.id)) continue;
      expect(labels.get(m.id)).toBe(m.label);
    }
    expect(labels.get("code")).toBe("Repositories");
  });

  it("shows operator-only entries to platform admins only", () => {
    const ids = (w: typeof writer) =>
      mobileMoreItems(w, [row("admin")], NS, null).flatMap((g) => g.items.map((i) => i.id));
    expect(ids(admin)).toContain("admin:users");
    expect(ids(writer)).toContain("admin:health");
    expect(ids(writer)).not.toContain("admin:users");
  });

  it("a viewer without an admin claim sees no ADMIN group (jwt-only namespace, the audit's case)", async () => {
    const viewer = { authenticated: true, subject: "v@example.com", org: "dev", roles: ["VIEWER"] };
    const jwtOnly = [row("jwt")];
    expect(mobileMoreItems(viewer, jwtOnly, NS, null).map((g) => g.group)).not.toContain("Admin");
    useStore.setState({ whoami: viewer, myNamespaces: jwtOnly });
    render(<MobileNav />);
    await userEvent.setup().click(screen.getByRole("button", { name: "More" }));
    const sheet = await screen.findByRole("dialog", { name: "More" });
    expect(within(sheet).queryByText("Admin")).not.toBeInTheDocument();
    for (const m of ADMIN_BASE) expect(within(sheet).queryByRole("link", { name: m.label })).not.toBeInTheDocument();
    expect(within(sheet).getByRole("link", { name: "Profile" })).toBeInTheDocument();
  });

  it("follows the sidebar's 8540 gate: no Admin group for a writer or viewer of the namespace", () => {
    const groups = (role: MyNamespaceEntry["role"]) =>
      mobileMoreItems(writer, [row(role)], NS, null).map((g) => g.group);
    // The sheet carries the rail's groups, then Settings, then Admin for an admin
    expect(groups("admin")).toEqual(["Memory", "Activity", "Sources", "Settings", "Admin"]);
    expect(groups("writer")).toEqual(["Memory", "Activity", "Sources", "Settings"]);
    expect(groups("viewer")).not.toContain("Admin");
  });
});

describe("MobileNav", () => {
  it("opens a More sheet with a link for every sidebar route", async () => {
    useStore.setState({ whoami: admin });
    render(<MobileNav />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "More" }));
    const sheet = await screen.findByRole("dialog", { name: "More" });
    for (const m of [...MAIN, ...ADMIN_BASE, ...ADMIN_OPERATOR_ONLY]) {
      if (["memories", "knowledge"].includes(m.id)) continue;
      const link = within(sheet).getByRole("link", { name: m.label });
      expect(link).toHaveAttribute("href", viewHref(m.id));
    }
    expect(within(sheet).getByRole("link", { name: "Settings" })).toHaveAttribute("href", viewHref("setup"));
  });

  it("navigates and closes when a sheet link is tapped", async () => {
    useStore.setState({ whoami: writer, myNamespaces: [row("writer")] });
    render(<MobileNav />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "More" }));
    await user.click(await screen.findByRole("link", { name: "Repositories" }));
    expect(useStore.getState().view).toBe("code");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("marks the current primary tab", () => {
    useStore.setState({ view: "knowledge" });
    render(<MobileNav />);
    expect(screen.getByRole("link", { name: "Library" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Memories" })).not.toHaveAttribute("aria-current");
  });
});

describe("the Assistant item", () => {
  it("sits between Library and More, a button with the launcher's marks, never the current tab", () => {
    render(<MobileNav />);
    const nav = screen.getByRole("navigation", { name: "Sections" });
    expect([...nav.children].map((e) => e.textContent?.trim())).toEqual(["Memories", "Library", "Assistant", "More"]);
    const item = within(nav).getByRole("button", { name: "Assistant" });
    expect(item).toHaveAttribute("data-focus-key", "assistant-launcher");
    expect(item).toHaveAttribute("aria-expanded", "false");
    expect(item).not.toHaveAttribute("aria-current");
    expect(item).not.toHaveAttribute("aria-haspopup");
    expect(item).not.toHaveAttribute("aria-controls");
    act(() => useDock.setState({ working: true, mounted: true, open: true }));
    expect(item).toHaveAccessibleName("Assistant, answering");
    expect(item).toHaveAttribute("aria-expanded", "true");
    expect(item).toHaveAttribute("aria-controls", "assistant-panel");
    act(() => useDock.setState({ working: false, reason: { kind: "failing" } }));
    expect(item).toHaveAccessibleName("Assistant, unavailable");
    act(() => useDock.setState({ reason: null, open: false, mounted: false }));
  });
});
