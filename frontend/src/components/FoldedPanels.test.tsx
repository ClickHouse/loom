import { cleanup, fireEvent, render, screen } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { setFoldOpen } from "../lib/folds";
import { MentorPanel } from "./MentorPanel";
import { NamespaceAuditPanel } from "./NamespaceAuditPanel";

// The memories page's usage audit and mentor start folded to their title row, and each
// remembers, in this browser, that someone opened it.

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, mentorBrief: vi.fn(), auditNamespace: vi.fn() };
});

const viewer: WhoAmI = {
  authenticated: true,
  subject: "v@example.com",
  org: "dev",
  roles: ["VIEWER"],
  namespaces: ["a"],
};
const row: MyNamespaceEntry = {
  namespace: "a",
  role: "viewer",
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
};
const inA = { org: "dev", namespace: "a", agent: "loom-ui" };

const PANELS = [
  { name: "Usage audit", fold: "usage-audit", action: "Run audit", Panel: NamespaceAuditPanel },
  { name: "Mentor", fold: "mentor", action: "Ask the mentor", Panel: MentorPanel },
];

describe.each(PANELS)("$name panel", ({ name, fold, action, Panel }) => {
  beforeEach(() => {
    resetNamespaceRoleCache();
    useStore.setState({ authed: true, whoami: viewer, myNamespaces: [row], identity: inA });
    setFoldOpen(fold, false);
  });
  afterEach(() => cleanup());

  it("starts folded to its title", () => {
    render(<Panel identity={inA} />);
    const toggle = screen.getByRole("button", { name });
    expect(screen.getByRole("heading", { name })).toContainElement(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("button", { name: action })).toBeNull();
  });

  it("opens on a click, and stays open the next time", () => {
    const first = render(<Panel identity={inA} />);
    fireEvent.click(screen.getByRole("button", { name }));
    expect(screen.getByRole("button", { name })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: action })).toBeVisible();
    first.unmount();

    render(<Panel identity={inA} />);
    expect(screen.getByRole("button", { name })).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(screen.getByRole("button", { name }));
    expect(screen.queryByRole("button", { name: action })).toBeNull();
  });
});
