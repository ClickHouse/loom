import { cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { EMPTY_ONBOARDING, saveOnboarding } from "../lib/onboarding";
import { axeViolations } from "../test/axe";
import { Recent } from "./Recent";

// An empty Memories page (no first-run checklist) says what appears there and offers what
// fills it: a writer adds a memory or connects an agent in place; a viewer is told who can
// write and opens the members list to find an admin.

const auth = vi.hoisted(() => ({ off: false }));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    isAuthDisabled: () => auth.off,
    listMine: vi.fn(async (): Promise<MyNamespaceEntry[]> => []),
    listMemories: vi.fn(async () => ({ entries: [], truncated: false, total: 0, today: 0 })),
    countMemories: vi.fn(async () => 0),
    bulkGetMemories: vi.fn(async () => ({ results: {}, missing: [] })),
    conflictKeys: vi.fn(async (): Promise<string[]> => []),
    listTokens: vi.fn(async () => []),
    listGrants: vi.fn(async () => ({
      rows: [
        { subject: "boss@x", role: "admin", status: "active", granted_at: "2026-09-16T00:00:00Z", granted_by: "" },
        { subject: "n@x", role: "viewer", status: "active", granted_at: "2026-09-16T00:00:00Z", granted_by: "" },
      ],
    })),
  };
});
vi.mock("./Tokens", () => ({
  QuickConnect: () => <div>Token form</div>,
}));

const NS = "team";
const row = (role: MyNamespaceEntry["role"]): MyNamespaceEntry => ({
  namespace: NS,
  role,
  status: "active",
  visibility: "private",
  description: "",
  joined_at: "2026-09-16T00:00:00Z",
  is_new: false,
  kind: "memory",
});
const personal: MyNamespaceEntry = { ...row("admin"), namespace: "personal-n" };

function seed(role: MyNamespaceEntry["role"], roles: string[]) {
  const who: WhoAmI = { authenticated: true, subject: "n@x", org: "dev", roles, namespaces: [NS] };
  useStore.setState({
    authed: true,
    whoami: who,
    // Two namespaces: not a new account, so no checklist.
    myNamespaces: [personal, row(role)],
    identity: { org: "dev", namespace: NS, agent: "loom-ui" },
    view: "memories",
    memoriesVersion: 0,
  });
}

const WAIT = { timeout: 5000 };
const empty = async () => (await screen.findByRole("heading", { name: "No memories yet" }, WAIT)).parentElement!;

beforeEach(() => {
  resetNamespaceRoleCache();
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  auth.off = false;
});

describe("Memories empty state", () => {
  it("offers a writer Add memory and Connect an agent, and passes axe", async () => {
    seed("writer", ["WRITER"]);
    const { container } = render(<Recent />);
    const state = await empty();
    expect(
      within(state).getByText("Memories appear here when your agent saves one. Connect an agent, or add one yourself."),
    ).toBeInTheDocument();
    expect(within(state).getByRole("button", { name: "Add memory" })).toBeInTheDocument();
    await userEvent.click(within(state).getByRole("button", { name: "Connect an agent" }));
    const dialog = await screen.findByRole("dialog", { name: "Connect an agent" }, WAIT);
    expect(await within(dialog).findByText("Token form")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(await axeViolations(container)).toEqual([]);
  });

  it("keeps Add memory live for a `*` writer token that list_mine holds no row for", async () => {
    seed("writer", ["WRITER"]);
    useStore.setState({
      whoami: { authenticated: true, subject: "n@x", org: "dev", roles: ["WRITER"], namespaces: ["*"] },
      myNamespaces: [personal, { ...row("writer"), namespace: "elsewhere" }],
    });
    render(<Recent />);
    const state = await empty();
    const add = within(state).getByRole("button", { name: "Add memory" });
    expect(add).not.toHaveAttribute("aria-disabled");
  });

  it("tells a viewer who writes here and opens the members list, with no Add memory or connect", async () => {
    seed("viewer", ["VIEWER"]);
    const { container } = render(<Recent />);
    const state = await empty();
    expect(await axeViolations(container)).toEqual([]);
    expect(
      within(state).getByText("Memories appear here when someone with writer access saves one."),
    ).toBeInTheDocument();
    expect(
      within(state).getByText(`You're a viewer in ${NS}. Ask an admin for writer to add memories.`),
    ).toBeInTheDocument();
    expect(within(state).queryByRole("button", { name: "Add memory" })).toBeNull();
    expect(within(state).queryByRole("button", { name: "Connect an agent" })).toBeNull();
    await userEvent.click(within(state).getByRole("button", { name: "See who the admins are" }));
    const dialog = await screen.findByRole("dialog", undefined, WAIT);
    expect(await within(dialog).findByText("boss@x", undefined, WAIT)).toBeInTheDocument();
    // A viewer sees Add member unavailable, with the role it needs.
    const add = within(dialog).getByRole("button", { name: "Add member" });
    expect(add).toHaveAttribute("aria-disabled", "true");
    expect(add).toHaveAccessibleDescription(`Needs admin on ${NS}. An admin of it can grant that.`);
  });

  it("with auth off reads as a writer's, and the connect dialog shows the plugin lines", async () => {
    auth.off = true;
    seed("admin", ["ADMIN"]);
    // Auth off shows the checklist on an empty namespace until it is hidden.
    saveOnboarding("n@x", `dev/${NS}`, { ...EMPTY_ONBOARDING, dismissed: true });
    render(<Recent />);
    const state = await empty();
    expect(within(state).getByText(/Memories appear here when your agent saves one/)).toBeInTheDocument();
    await userEvent.click(within(state).getByRole("button", { name: "Connect an agent" }));
    const dialog = await screen.findByRole("dialog", { name: "Connect an agent" }, WAIT);
    expect(within(dialog).getByText(/no token is needed/)).toBeInTheDocument();
    expect(within(dialog).queryByText("Token form")).toBeNull();
  });
});
