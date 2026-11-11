import { createRef, type RefObject } from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { invalidateShared } from "../lib/sharedReads";
import { NamespacePicker } from "./NamespacePicker";
import { NamespacePickerList } from "./NamespacePickerList";

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
// The grouping, counted: the names decide the groups, so a keystroke reworks none.
const { grouped } = vi.hoisted(() => ({ grouped: { calls: 0 } }));
vi.mock("../lib/nameFamilies", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/nameFamilies")>();
  return {
    nameFamilies: (...args: Parameters<typeof actual.nameFamilies>) => {
      grouped.calls += 1;
      return actual.nameFamilies(...args);
    },
  };
});
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
  const dialog = await screen.findByRole("dialog", { name: "Namespaces" });
  // The open panel loads with its first open.
  await within(dialog).findByRole("textbox", { name: "Search namespaces" });
  return dialog;
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
    const code = group(dialog, /^Repositories/);
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
    expect(ch.closest("section")).toHaveAccessibleName(/Repositories/);
  });

  it("puts a code index with no description on one line, its name as code", async () => {
    seed("team", [row("team", { role: "admin" }), row("idx-bare", { kind: "code", role: "jwt" })]);
    render(<NamespacePicker />);
    const dialog = await openPicker();
    fireEvent.click(group(dialog, /^Repositories/));
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
    const code = group(dialog, /^Repositories/);
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
    const trigger = screen.getByRole("button", { name: "Namespace: idx-ch, repository" });
    expect(within(trigger).getByText("code")).toBeInTheDocument();
    const dialog = await openPicker();
    expect(group(dialog, /^Repositories/)).toHaveAttribute("aria-expanded", "true");
    const current = within(dialog).getByRole("button", { name: /idx-ch/ });
    expect(current).toHaveAttribute("aria-current", "true");
    // Current first, then by name.
    const names = within(document.getElementById(group(dialog, /^Repositories/).getAttribute("aria-controls") ?? "")!)
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
    const code = group(dialog, /^Repositories/);
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
    fireEvent.click(group(dialog, /^Repositories/));
    expect(within(dialog).getAllByRole("button", { name: /acme\/repo/ })).toHaveLength(8);
    fireEvent.click(within(dialog).getByRole("button", { name: "Show all 11" }));
    expect(within(dialog).getAllByRole("button", { name: /acme\/repo/ })).toHaveLength(11);
    // The button it replaced took the focus with it: the first row it showed has it now.
    expect(within(dialog).getByRole("button", { name: /acme\/repo8 @ main/ })).toHaveFocus();
  });

  it("folds memory namespaces sharing a name prefix, three or more, into a group per prefix", async () => {
    seed("team", [
      row("team", { role: "admin" }),
      row("group-a-shared", { description: "Workspace A" }),
      row("group-b-shared", { description: "Workspace B" }),
      row("group-c-shared"),
      row("agent-1-a", { description: "Support agent" }),
      row("agent-1-b", { description: "Sales agent" }),
      row("agent-2-c"),
      row("ops-one"),
      row("ops-two"),
      row("idx-ch", { kind: "code", role: "jwt" }),
    ]);
    render(<NamespacePicker />);
    const dialog = await openPicker();
    // Titled by the prefix the group shares.
    const shared = group(dialog, /^group-…/);
    const agents = group(dialog, /^agent-…/);
    expect(shared).toHaveAttribute("aria-expanded", "false");
    expect(shared).toHaveTextContent("3");
    expect(agents).toHaveTextContent("3");
    expect(within(dialog).queryByRole("button", { name: /group-a-shared/ })).toBeNull();
    // Two of a prefix stay with the rest, by role.
    expect(within(dialog).getByRole("button", { name: /ops-one/ })).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: /^ops-…/ })).toBeNull();
    // The name families come first, by prefix, then the code indexes.
    const order = [...dialog.querySelectorAll("[data-nav^='group:']")].map((b) => b.getAttribute("data-nav"));
    expect(order).toEqual(["group:agent-", "group:group-", "group:code"]);
    fireEvent.click(agents);
    const body = document.getElementById(agents.getAttribute("aria-controls") ?? "");
    // A member reads like a code index: its description, then its name as code.
    const support = within(body as HTMLElement).getByRole("button", { name: /agent-1-a/ });
    expect(support.textContent).toMatch(/^Support agent.*agent-1-a/);
    expect(within(support).getByText("agent-1-a").tagName).toBe("CODE");
  });

  it("folds names that carry an owner's uuid per owner, the uuid one part of the prefix", async () => {
    const OWNER_1 = "1b4e28ba-2fa1-41d2-883f-0016d3cca427";
    const OWNER_2 = "6f1c2b7e-0d4a-4c1b-9e8f-1a2b3c4d5e6f";
    const agent = (owner: string, n: number) => row(`agent-${owner}-00000000-0000-4000-8000-00000000000${n}`);
    seed("team", [
      row("team", { role: "admin" }),
      ...[1, 2, 3].map((n) => agent(OWNER_1, n)),
      ...[4, 5, 6].map((n) => agent(OWNER_2, n)),
    ]);
    render(<NamespacePicker />);
    const dialog = await openPicker();
    expect(group(dialog, new RegExp(`^agent-${OWNER_1}-…`))).toHaveTextContent("3");
    expect(group(dialog, new RegExp(`^agent-${OWNER_2}-…`))).toHaveTextContent("3");
    expect(within(dialog).queryByRole("button", { name: /^agent-… / })).toBeNull();
  });

  it("opens the name family the current namespace is in, and the one a search matches", async () => {
    const rows = [
      row("team", { role: "admin" }),
      row("agent-1-a", { description: "Support agent" }),
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
    expect(within(dialog).getByRole("button", { name: /Support agent/ })).toBeInTheDocument();
  });

  it("keeps the footer's Spaces link for someone whose joined namespaces all sit in a family", async () => {
    seed("team", [row("team", { role: "admin" }), row("agent-1-a"), row("agent-1-b"), row("agent-2-c")]);
    render(<NamespacePicker />);
    const dialog = await openPicker();
    expect(within(dialog).getByRole("button", { name: "Browse and join spaces" })).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Find public namespaces to join in Spaces" })).toBeNull();
  });

  it("groups the names once per list, not again for each keystroke of a search", async () => {
    seed("team", [row("team", { role: "admin" }), row("agent-1-a"), row("agent-1-b"), row("agent-2-c")]);
    const user = userEvent.setup();
    render(<NamespacePicker />);
    const dialog = await openPicker();
    await waitFor(() => expect(group(dialog, /^agent-…/)).toBeInTheDocument());
    const before = grouped.calls;
    await user.type(within(dialog).getByRole("textbox", { name: "Search namespaces" }), "agent-1");
    expect(group(dialog, /^agent-…/)).toHaveTextContent("2 of 3");
    expect(grouped.calls).toBe(before);
  });

  it("opens the current namespace's group once the panel's own read places it, when boot's rows did not", async () => {
    seed("agent-1-b", [row("team", { role: "admin" }), row("agent-1-a"), row("agent-1-b"), row("agent-2-c")]);
    // Boot's rows predate the namespace: only the panel's own read lists it.
    useStore.setState({ myNamespaces: [row("team", { role: "admin" })] });
    render(<NamespacePicker />);
    const dialog = await openPicker();
    await waitFor(() => expect(group(dialog, /^agent-…/)).toHaveAttribute("aria-expanded", "true"));
  });

  it("opens the group the panel's own read puts the current namespace in, over boot's", async () => {
    const before = [row("team", { role: "admin" }), row("agent-1-a"), row("agent-1-b"), row("agent-2-c")];
    seed("agent-1-b", [...before, row("agent-1-c")]);
    // Boot's rows predate agent-1-c, so they fold the current namespace under agent-.
    useStore.setState({ myNamespaces: before });
    render(<NamespacePicker />);
    const dialog = await openPicker();
    await waitFor(() => expect(group(dialog, /^agent-1-…/)).toHaveAttribute("aria-expanded", "true"));
  });

  it("takes the focus as it loads only from where the open left it", async () => {
    seed("team");
    function Harness(props: {
      list: boolean;
      trigger: RefObject<HTMLButtonElement>;
      input: RefObject<HTMLInputElement>;
    }) {
      return (
        <>
          <button type="button">elsewhere</button>
          <button type="button" ref={props.trigger}>
            trigger
          </button>
          {props.list && (
            <NamespacePickerList
              inputRef={props.input}
              triggerRef={props.trigger}
              onPick={() => {}}
              onClose={() => {}}
              onCreate={() => {}}
            />
          )}
        </>
      );
    }
    for (const [from, takes] of [
      ["elsewhere", false],
      ["trigger", true],
    ] as const) {
      const trigger = createRef<HTMLButtonElement>();
      const input = createRef<HTMLInputElement>();
      const { rerender, unmount } = render(<Harness list={false} trigger={trigger} input={input} />);
      screen.getByRole("button", { name: from }).focus();
      rerender(<Harness list trigger={trigger} input={input} />);
      const search = await screen.findByRole("textbox", { name: "Search namespaces" });
      if (takes) expect(search).toHaveFocus();
      else expect(screen.getByRole("button", { name: from })).toHaveFocus();
      unmount();
    }
  });

  it("folds a group a search opened when its button is pressed, and opens it again", async () => {
    seed("team");
    const user = userEvent.setup();
    render(<NamespacePicker />);
    const dialog = await openPicker();
    await user.type(within(dialog).getByRole("textbox", { name: "Search namespaces" }), "loom");
    const code = group(dialog, /^Repositories/);
    expect(code).toHaveAttribute("aria-expanded", "true");
    await user.click(code);
    expect(code).toHaveAttribute("aria-expanded", "false");
    expect(within(dialog).queryByRole("button", { name: /acme\/loom @ main/ })).toBeNull();
    await user.click(code);
    expect(code).toHaveAttribute("aria-expanded", "true");
    expect(within(dialog).getByRole("button", { name: /acme\/loom @ main/ })).toBeInTheDocument();
  });

  it("lists no collection, whatever its role or kind, and links to the Library instead", async () => {
    seed("team", [
      row("team", { role: "admin" }),
      row("ops"),
      row("col-research", { role: "admin", kind: "collection", title: "Research" }),
      row("col-handbook", { role: "viewer", kind: "collection" }),
      // A server a release behind, or a create cut short: the reserved name still decides.
      row("col-unfinished", { role: "writer", kind: "memory" }),
      row("col-claimed", { role: "jwt", kind: "collection" }),
    ]);
    render(<NamespacePicker />);
    const dialog = await openPicker();
    expect(within(dialog).getByRole("button", { name: /\bteam\b/ })).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: /col-|Research/ })).toBeNull();
    expect(within(dialog).queryByText(/col-/)).toBeNull();
    expect(within(dialog).getByText("Your namespaces")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Open collections in the Library" }));
    expect(useStore.getState().view).toBe("knowledge");
  });

  it("says once what a namespace and a repository are", async () => {
    seed("team");
    render(<NamespacePicker />);
    const dialog = await openPicker();
    expect(within(dialog).getByText(/^A namespace is a project's or an agent's memory\./)).toBeInTheDocument();
    fireEvent.click(group(dialog, /^Repositories/));
    expect(within(dialog).getByText("Code that Loom indexes, read-only.")).toBeInTheDocument();
  });
});
