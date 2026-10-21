import { cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { invalidateShared } from "../lib/sharedReads";
import { NamespacePicker } from "./NamespacePicker";

// The picker lists memory namespaces by role, then code indexes and tracker records in
// their own folded groups. A row is a code index only when the server says kind=code —
// never from its name. Search opens every group with a match; Up and Down rove over the
// rows and the group buttons in the order they are drawn.

const row = (namespace: string, extra: Partial<MyNamespaceEntry> = {}): MyNamespaceEntry => ({
  namespace,
  role: "writer",
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
  kind: "memory",
  ...extra,
});

const ROWS: MyNamespaceEntry[] = [
  row("team", { role: "admin" }),
  row("ops"),
  // Named like a code index, but the server says memory: it stays a memory namespace.
  row("code-notes"),
  row("idx-ch", { kind: "code", role: "jwt", description: "ClickHouse/ClickHouse @ master" }),
  row("idx-loom", { kind: "code", role: "jwt", description: "acme/loom @ main" }),
  row("tracker-ch", { kind: "qa", role: "viewer", description: "ClickHouse issues" }),
];

let mine = ROWS;
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, isAuthDisabled: () => false, listMine: vi.fn(async () => mine), setSavedNamespace: vi.fn() };
});

const whoami: WhoAmI = { authenticated: true, subject: "a@example.com", org: "dev", roles: ["WRITER"], namespaces: [] };

function seed(current: string, rows: MyNamespaceEntry[] = ROWS) {
  mine = rows;
  useStore.setState({
    authed: true,
    whoami,
    myNamespaces: rows,
    identity: { org: "dev", namespace: current, agent: "loom-ui" },
  });
}

async function openPicker() {
  fireEvent.click(screen.getByRole("button", { name: /^Namespace: / }));
  return screen.findByRole("dialog", { name: "Namespaces" });
}

const group = (dialog: HTMLElement, name: RegExp) => within(dialog).getByRole("button", { name });

describe("NamespacePicker groups", () => {
  beforeEach(() => {
    invalidateShared();
    localStorage.clear();
  });
  afterEach(() => cleanup());

  it("keeps code indexes and tracker records out of the memory groups, folded", async () => {
    seed("team");
    render(<NamespacePicker />);
    const dialog = await openPicker();
    expect(within(dialog).getByRole("button", { name: /\bteam\b/ })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: /code-notes/ })).toBeInTheDocument();
    const code = group(dialog, /^Code indexes/);
    const tracker = group(dialog, /^Tracker records/);
    expect(code).toHaveAttribute("aria-expanded", "false");
    expect(code).toHaveTextContent("2");
    expect(tracker).toHaveAttribute("aria-expanded", "false");
    expect(within(dialog).queryByRole("button", { name: /idx-ch/ })).toBeNull();

    fireEvent.click(code);
    expect(code).toHaveAttribute("aria-expanded", "true");
    const body = document.getElementById(code.getAttribute("aria-controls") ?? "");
    expect(body).not.toBeNull();
    const ch = within(body as HTMLElement).getByRole("button", { name: /ClickHouse\/ClickHouse @ master/ });
    // The repository is the row's first line; the namespace its second, as code. No per-row code badge.
    expect(ch.textContent).toMatch(/^ClickHouse\/ClickHouse @ master.*idx-ch/);
    expect(within(ch).getByText("idx-ch").tagName).toBe("CODE");
    expect(within(ch).queryByText("code")).toBeNull();
    // The section is a labelled group.
    expect(ch.closest("section")).toHaveAccessibleName(/Code indexes/);
  });

  it("puts a code index with no description on one line, its name as code", async () => {
    seed("team", [row("team", { role: "admin" }), row("idx-bare", { kind: "code", role: "jwt" })]);
    render(<NamespacePicker />);
    const dialog = await openPicker();
    fireEvent.click(group(dialog, /^Code indexes/));
    const name = within(within(dialog).getByRole("button", { name: /idx-bare/ })).getByText("idx-bare");
    expect(name.tagName).toBe("CODE");
    // The first line, and no empty second line under it.
    expect(name.parentElement?.previousElementSibling).toBeNull();
    expect(name.parentElement?.nextElementSibling).toBeNull();
  });

  it("opens every group with a match while searching and hides the rest", async () => {
    seed("team");
    const user = userEvent.setup();
    render(<NamespacePicker />);
    const dialog = await openPicker();
    await user.type(within(dialog).getByRole("textbox", { name: "Search namespaces" }), "loom");
    const code = group(dialog, /^Code indexes/);
    expect(code).toHaveAttribute("aria-expanded", "true");
    expect(code).toHaveTextContent("1 of 2");
    expect(within(dialog).getByRole("button", { name: /acme\/loom @ main/ })).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: /^Tracker records/ })).toBeNull();
    expect(within(dialog).queryByRole("button", { name: /\bteam\b/ })).toBeNull();

    await user.clear(within(dialog).getByRole("textbox", { name: "Search namespaces" }));
    await user.type(within(dialog).getByRole("textbox", { name: "Search namespaces" }), "zzz");
    expect(within(dialog).getByText("No namespaces match ‘zzz’")).toBeInTheDocument();
  });

  it("names a current code index on the trigger and opens its group", async () => {
    seed("idx-ch");
    render(<NamespacePicker />);
    const trigger = screen.getByRole("button", { name: "Namespace: idx-ch, code index" });
    expect(within(trigger).getByText("code")).toBeInTheDocument();
    const dialog = await openPicker();
    expect(group(dialog, /^Code indexes/)).toHaveAttribute("aria-expanded", "true");
    const current = within(dialog).getByRole("button", { name: /idx-ch/ });
    expect(current).toHaveAttribute("aria-current", "true");
    // Current first, then by name.
    const names = within(document.getElementById(group(dialog, /^Code indexes/).getAttribute("aria-controls") ?? "")!)
      .getAllByRole("button")
      .map((b) => b.textContent);
    expect(names[0]).toContain("idx-ch");
  });

  it("roves with Up, Down, Home and End over rows and group buttons in drawn order", async () => {
    seed("team");
    const user = userEvent.setup();
    render(<NamespacePicker />);
    const dialog = await openPicker();
    const search = within(dialog).getByRole("textbox", { name: "Search namespaces" });
    // The search field takes the focus on open; Down moves into the list.
    await waitFor(() => expect(search).toHaveFocus());
    await user.keyboard("{ArrowDown}");
    // The current namespace is the tab stop the list is entered at.
    expect(within(dialog).getByRole("button", { name: /\bteam\b/ })).toHaveFocus();
    await user.keyboard("{End}");
    expect(group(dialog, /^Tracker records/)).toHaveFocus();
    await user.keyboard("{ArrowUp}");
    const code = group(dialog, /^Code indexes/);
    expect(code).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(code).toHaveAttribute("aria-expanded", "true");
    await user.keyboard("{ArrowDown}");
    expect(within(dialog).getByRole("button", { name: /ClickHouse\/ClickHouse/ })).toHaveFocus();
    await user.keyboard("{Home}");
    expect(within(dialog).getByRole("button", { name: /\bteam\b/ })).toHaveFocus();
    // One tab stop: every other option is out of the tab order.
    const stops = [...dialog.querySelectorAll("[data-nav]")].filter((el) => el.getAttribute("tabindex") === "0");
    expect(stops).toHaveLength(1);
  });

  it("caps an open group at eight rows with a Show all", async () => {
    const many = Array.from({ length: 11 }, (_, i) =>
      row(`idx-${String(i).padStart(2, "0")}`, { kind: "code", role: "jwt", description: `acme/repo${i} @ main` }),
    );
    seed("team", [row("team", { role: "admin" }), ...many]);
    render(<NamespacePicker />);
    const dialog = await openPicker();
    fireEvent.click(group(dialog, /^Code indexes/));
    expect(within(dialog).getAllByRole("button", { name: /acme\/repo/ })).toHaveLength(8);
    fireEvent.click(within(dialog).getByRole("button", { name: "Show all 11" }));
    expect(within(dialog).getAllByRole("button", { name: /acme\/repo/ })).toHaveLength(11);
    // The button it replaced took the focus with it: the first row it showed has it now.
    expect(within(dialog).getByRole("button", { name: /acme\/repo8 @ main/ })).toHaveFocus();
  });

  it("folds memory namespaces sharing a name prefix, three or more, into a group per prefix", async () => {
    seed("team", [
      row("team", { role: "admin" }),
      row("t-1-shared", { description: "Acme" }),
      row("t-2-shared", { description: "Globex" }),
      row("t-3-shared"),
      row("agent-1-a", { description: "Support bot" }),
      row("agent-1-b", { description: "Sales bot" }),
      row("agent-2-c"),
      row("col-one"),
      row("col-two"),
      row("idx-ch", { kind: "code", role: "jwt" }),
    ]);
    render(<NamespacePicker />);
    const dialog = await openPicker();
    // Named by the prefix, with the last segment every member shares.
    const shared = group(dialog, /^t-…-shared/);
    const agents = group(dialog, /^agent-…/);
    expect(shared).toHaveAttribute("aria-expanded", "false");
    expect(shared).toHaveTextContent("3");
    expect(agents).toHaveTextContent("3");
    expect(within(dialog).queryByRole("button", { name: /t-1-shared/ })).toBeNull();
    // Two of a prefix stay with the rest, by role.
    expect(within(dialog).getByRole("button", { name: /col-one/ })).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: /^col-…/ })).toBeNull();
    // The name families come first, by prefix, then the code indexes.
    const order = [...dialog.querySelectorAll("[data-nav^='group:']")].map((b) => b.getAttribute("data-nav"));
    expect(order).toEqual(["group:agent-", "group:t-", "group:code"]);
    fireEvent.click(agents);
    const body = document.getElementById(agents.getAttribute("aria-controls") ?? "");
    // A member reads like a code index: its description, then its name as code.
    const bot = within(body as HTMLElement).getByRole("button", { name: /agent-1-a/ });
    expect(bot.textContent).toMatch(/^Support bot.*agent-1-a/);
    expect(within(bot).getByText("agent-1-a").tagName).toBe("CODE");
  });

  it("opens the name family the current namespace is in, and the one a search matches", async () => {
    const rows = [
      row("team", { role: "admin" }),
      row("agent-1-a", { description: "Support bot" }),
      row("agent-1-b"),
      row("agent-2-c"),
    ];
    seed("agent-1-b", rows);
    const user = userEvent.setup();
    const { unmount } = render(<NamespacePicker />);
    let dialog = await openPicker();
    expect(group(dialog, /^agent-…/)).toHaveAttribute("aria-expanded", "true");
    expect(within(dialog).getByRole("button", { name: /agent-1-b/ })).toHaveAttribute("aria-current", "true");
    unmount();
    seed("team", rows);
    render(<NamespacePicker />);
    dialog = await openPicker();
    expect(group(dialog, /^agent-…/)).toHaveAttribute("aria-expanded", "false");
    await user.type(within(dialog).getByRole("textbox", { name: "Search namespaces" }), "support");
    expect(group(dialog, /^agent-…/)).toHaveAttribute("aria-expanded", "true");
    expect(group(dialog, /^agent-…/)).toHaveTextContent("1 of 3");
    expect(within(dialog).getByRole("button", { name: /Support bot/ })).toBeInTheDocument();
  });

  it("keeps the footer's Spaces link for someone whose joined namespaces all sit in a family", async () => {
    seed("team", [row("team", { role: "admin" }), row("agent-1-a"), row("agent-1-b"), row("agent-2-c")]);
    render(<NamespacePicker />);
    const dialog = await openPicker();
    expect(within(dialog).getByRole("button", { name: "Browse and join spaces" })).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Find public namespaces to join in Spaces" })).toBeNull();
  });

  it("folds a group a search opened when its button is pressed, and opens it again", async () => {
    seed("team");
    const user = userEvent.setup();
    render(<NamespacePicker />);
    const dialog = await openPicker();
    await user.type(within(dialog).getByRole("textbox", { name: "Search namespaces" }), "loom");
    const code = group(dialog, /^Code indexes/);
    expect(code).toHaveAttribute("aria-expanded", "true");
    await user.click(code);
    expect(code).toHaveAttribute("aria-expanded", "false");
    expect(within(dialog).queryByRole("button", { name: /acme\/loom @ main/ })).toBeNull();
    await user.click(code);
    expect(code).toHaveAttribute("aria-expanded", "true");
    expect(within(dialog).getByRole("button", { name: /acme\/loom @ main/ })).toBeInTheDocument();
  });
});
