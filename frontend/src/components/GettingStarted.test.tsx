import { act, cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry, TokenEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { onboardingKey } from "../lib/onboarding";
import { axeViolations } from "../test/axe";
import { Setup } from "../views/Setup";

// A first visit to Setup is a four-step checklist. It shows for an account with only its
// personal namespace and no memories, hides for one with memories, each step opens the
// EXISTING flow, ticks persist across a remount, and the whole thing passes axe.

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listTokens: vi.fn(async (): Promise<TokenEntry[]> => []),
    listMine: vi.fn(async (): Promise<MyNamespaceEntry[]> => []),
    listMemories: vi.fn(async () => ({ entries: [], truncated: false, total: 0, today: 0 })),
    countMemories: vi.fn(async () => 0),
    listBlacklist: vi.fn(async () => ({ rows: [] })),
    listGrants: vi.fn(async () => ({ rows: [] })),
    createToken: vi.fn(async () => ({
      token: "tok-123",
      name: "token-2026-01-01-abcdef",
      sa_subject: "sa:t",
      org: "dev",
      namespaces: ["personal-n", "*"],
      ns_roles: {},
      roles: ["writer"],
      reads_everything: true,
      saves_to: "personal-n",
      created_at: "2026-01-01T00:00:00",
      expires_at: null,
      mcp_url: "http://loom.test/mcp",
    })),
  };
});
import { countMemories, createToken, listGrants, listMemories } from "../api";

const SUBJECT = "n@x";
const NS = "personal-n";
const whoami: WhoAmI = { authenticated: true, subject: SUBJECT, org: "dev", roles: ["WRITER"], namespaces: [NS] };
const personal: MyNamespaceEntry = {
  namespace: NS,
  role: "admin",
  visibility: "private",
  description: "",
  joined_at: "2026-09-16T00:00:00Z",
  is_new: true,
  kind: "memory",
};

function seed(mine: MyNamespaceEntry[]) {
  useStore.setState({
    authed: true,
    whoami,
    myNamespaces: mine,
    identity: { org: "dev", namespace: NS, agent: "loom-ui" },
    view: "setup",
    memoriesVersion: 0,
    paletteRequest: null,
    paletteOpens: 0,
  });
}

const checklist = () => screen.queryByRole("region", { name: "Getting started" });
// One budget for every wait: CI runners are slower than the 1 s default assumes.
const WAIT = { timeout: 5000 };

describe("GettingStarted on Setup", () => {
  beforeEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    localStorage.clear();
    // Stray fetches answer an empty object; nothing here is about them.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 200 })),
    );
    Element.prototype.scrollIntoView = vi.fn();
    vi.mocked(listMemories).mockResolvedValue({ entries: [], truncated: false, total: 0, today: 0 });
    vi.mocked(countMemories).mockResolvedValue(0);
  });
  afterEach(() => {
    // Unmount before the store loses its identity: afterEach hooks run
    // newest-first, so the shared cleanup would otherwise come second.
    cleanup();
    vi.unstubAllGlobals();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null });
  });

  it("renders four steps at 0 of 4 for a new account", async () => {
    seed([personal]);
    render(<Setup />);
    const region = await screen.findByRole("region", { name: "Getting started" });
    expect(within(region).getAllByRole("listitem")).toHaveLength(4);
    expect(within(region).getByRole("heading", { name: /Connect a client/ })).toBeInTheDocument();
    expect(within(region).getByRole("heading", { name: /Write your first memory/ })).toBeInTheDocument();
    expect(within(region).getByRole("heading", { name: /Find it again/ })).toBeInTheDocument();
    expect(within(region).getByRole("heading", { name: /Invite a teammate/ })).toBeInTheDocument();
    expect(within(region).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "0");
    // The intro names the space in plain language, with the technical word once.
    expect(region).toHaveTextContent(/your space \(a namespace, in the docs\)/);
    // The rest of the page is a tab away; the token form is open because there is no
    // token yet.
    expect(screen.getByRole("tab", { name: "Memory" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /Create token/ })).toBeInTheDocument();
    // One count for the total, never a page.
    expect(countMemories).toHaveBeenCalledTimes(1);
    expect(listMemories).not.toHaveBeenCalled();
  });

  it("is hidden for an account that already has memories, and the page reads as before", async () => {
    vi.mocked(countMemories).mockResolvedValue(12);
    seed([personal]);
    render(<Setup />);
    await waitFor(() => expect(countMemories).toHaveBeenCalled(), WAIT);
    await screen.findByRole("tab", { name: "Tokens" });
    expect(checklist()).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Memory" })).toBeInTheDocument();
  });

  it("is hidden for an account that joined another space", async () => {
    seed([personal, { ...personal, namespace: "team-x", role: "writer", is_new: false }]);
    render(<Setup />);
    await waitFor(() => expect(countMemories).toHaveBeenCalled(), WAIT);
    await screen.findByRole("tab", { name: "Tokens" });
    expect(checklist()).not.toBeInTheDocument();
  });

  it("each step's action opens its existing flow", async () => {
    const user = userEvent.setup();
    seed([personal]);
    render(<Setup />);
    const region = await screen.findByRole("region", { name: "Getting started" });

    // 1. Connect → scrolls to and focuses the token form's Create button.
    await user.click(within(region).getByRole("button", { name: "Mint a token" }));
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /Create token/ })).toHaveFocus();
    // A mint ticks the step and renders the one-line Claude Code command.
    await user.click(screen.getByRole("button", { name: /Create token/ }));
    await waitFor(() => expect(createToken).toHaveBeenCalled(), WAIT);
    // The existing post-mint dialog opens as before; close it to read the
    // checklist (a modal hides the rest of the page from assistive tech).
    expect(await screen.findByRole("dialog", { name: /Token created/ })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument(), WAIT);
    const cmd = await within(region).findByText(/claude mcp add --transport http loom/);
    expect(cmd).toHaveTextContent('"http://loom.test/mcp?toolset=coding" -H "Authorization: Bearer tok-123" -s user');
    expect(within(region).getByRole("tab", { name: "Cursor" })).toBeInTheDocument();
    expect(within(region).getByRole("tab", { name: "Other clients" })).toBeInTheDocument();
    // A refused clipboard write says so instead of claiming "Copied".
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValueOnce(new Error("denied"));
    const copy = within(region).getByRole("button", { name: "Copy command" });
    await user.click(copy);
    await waitFor(() => expect(copy).toHaveTextContent("Copy failed"), WAIT);

    // 2. Write → the AddMemoryDialog.
    await user.click(within(region).getByRole("button", { name: "Write a memory" }));
    expect(await screen.findByRole("dialog", { name: /Add memory/ })).toBeInTheDocument();
    await user.keyboard("{Escape}");

    // 3. Find → the ⌘K palette, asked to open with a hint; ticks at once.
    await user.click(within(region).getByRole("button", { name: "Search your memories" }));
    expect(useStore.getState().paletteRequest).toMatchObject({ hint: "deploy window" });

    // 4. Invite → the MembersDialog for this namespace.
    await user.click(within(region).getByRole("button", { name: "Add a teammate" }));
    expect(await screen.findByRole("dialog", { name: /Members of/ })).toBeInTheDocument();
    expect(listGrants).toHaveBeenCalledWith({ org: "dev", namespace: NS });
  });

  it("ticks persist across a remount, and Skip the tour hides it for good", async () => {
    const user = userEvent.setup();
    seed([personal]);
    const first = render(<Setup />);
    let region = await screen.findByRole("region", { name: "Getting started" });
    await user.click(within(region).getByRole("button", { name: "Search your memories" }));
    expect(within(region).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "1");
    first.unmount();

    // Fresh store fields, same browser: the tick is read back from storage.
    seed([personal]);
    const second = render(<Setup />);
    region = await screen.findByRole("region", { name: "Getting started" });
    expect(within(region).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "1");
    expect(JSON.parse(localStorage.getItem(onboardingKey(SUBJECT)) ?? "{}")).toMatchObject({
      done: { find: true },
      started: true,
    });

    await user.click(within(region).getByRole("button", { name: "Skip the tour" }));
    expect(checklist()).not.toBeInTheDocument();
    second.unmount();

    seed([personal]);
    render(<Setup />);
    await screen.findByRole("tab", { name: "Tokens" });
    await waitFor(() => expect(countMemories).toHaveBeenCalled(), WAIT);
    expect(checklist()).not.toBeInTheDocument();
  });

  it("stays through the first memory, ticking step two instead of disappearing", async () => {
    seed([personal]);
    render(<Setup />);
    const region = await screen.findByRole("region", { name: "Getting started" });
    expect(within(region).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "0");
    const readsBefore = vi.mocked(countMemories).mock.calls.length;
    // A write elsewhere bumps memoriesVersion; Setup re-reads the total. The
    // read resolves only when this test says so, inside act, so the tick is
    // asserted after the state has flushed — not raced against a timer.
    let resolveTotal!: (n: number) => void;
    vi.mocked(countMemories).mockImplementationOnce(
      () =>
        new Promise<number>((res) => {
          resolveTotal = res;
        }),
    );
    await act(async () => {
      useStore.getState().bumpMemoriesVersion();
    });
    await waitFor(() => expect(vi.mocked(countMemories).mock.calls.length).toBeGreaterThan(readsBefore), WAIT);
    await act(async () => {
      resolveTotal(1);
    });
    await waitFor(() => expect(within(region).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "1"), WAIT);
    expect(checklist()).toBeInTheDocument();
  });

  it("passes axe", async () => {
    seed([personal]);
    render(<Setup />);
    const region = await screen.findByRole("region", { name: "Getting started" });
    expect(await axeViolations(region)).toEqual([]);
  });
});
