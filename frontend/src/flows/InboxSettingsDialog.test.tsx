/** The posters list is picked, never typed: rows come from
 *  `inbox.principals`; listed subjects start checked, writers on the owner
 *  namespace are checked and disabled (they post regardless), the filter
 *  narrows the rows, and saving sends the checked subjects as `posters`. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { InboxSettingsDialog } from "./InboxSettingsDialog";
import * as tasks from "../api/tasks";
import type { Inbox, Principal } from "../api/tasks";
import { must } from "../test/must";

// Relative to now: `relativeTime` renders "Nd ago" only inside a week.
const hoursAgo = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString();

const PRINCIPALS: Principal[] = [
  {
    subject: "alice@example.com",
    label: "alice@example.com",
    kind: "human",
    namespaces: ["team", "other", "qa", "code-x", "code-y"],
    roles: ["viewer"],
    last_seen: hoursAgo(26),
    can_already_post: false,
    listed: true,
  },
  {
    subject: "sa:team-bot-9f8e7d",
    label: "team-bot",
    kind: "service_account",
    namespaces: ["team"],
    roles: ["writer"],
    last_seen: null,
    can_already_post: true,
    listed: false,
  },
  {
    subject: "carol@example.com",
    label: "carol@example.com",
    kind: "human",
    namespaces: ["other"],
    roles: ["viewer"],
    last_seen: hoursAgo(24 * 3),
    can_already_post: false,
    listed: false,
  },
  // Listed but no longer in the directory: the server keeps it so it can be unchecked.
  {
    subject: "sa:retired-1a2b3c",
    label: "sa:retired-1a2b3c",
    kind: "other",
    namespaces: [],
    roles: [],
    last_seen: null,
    can_already_post: false,
    listed: true,
  },
];

vi.mock("../api/tasks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/tasks")>();
  return {
    ...actual,
    listPrincipals: vi.fn(async () => ({ principals: PRINCIPALS, truncated: false })),
    listPolicies: vi.fn(async () => []),
    registerInbox: vi.fn(async (_id: unknown, input: { posters?: string[] }) => ({
      inbox: { ...INBOX, posters: input.posters ?? [], version: 2 },
      created: false,
    })),
  };
});

const identity = { org: "dev", namespace: "team" };
const INBOX: Inbox = {
  org: "dev",
  inbox: "private-bot",
  owner_agent: "reporter-private",
  owner_namespace: "team",
  description: "",
  posters: ["alice@example.com", "sa:retired-1a2b3c"],
  accept_disclosure: ["internal"],
  default_lease_seconds: 1800,
  max_attempts: 3,
  max_defers: 5,
  triage_escalate_after_seconds: 14400,
  webhook_url: "",
  created_by: "",
  created_at: "2026-09-20T00:00:00",
  updated_at: "2026-09-20T00:00:00",
  version: 1,
};
const noop = () => undefined;

beforeEach(() => {
  vi.mocked(tasks.listPrincipals).mockClear();
  vi.mocked(tasks.registerInbox).mockClear();
});

async function openEditing() {
  render(<InboxSettingsDialog open onOpenChange={noop} identity={identity} inbox={INBOX} onSaved={noop} />);
  const list = await screen.findByRole("list", { name: "Posters" });
  return list;
}

describe("InboxSettingsDialog poster picker", () => {
  it("asks the directory for the inbox and renders one row per principal — no free-text entry", async () => {
    const list = await openEditing();
    await waitFor(() =>
      expect(tasks.listPrincipals).toHaveBeenCalledWith(identity, { inbox: "private-bot", query: "" }),
    );
    expect(within(list).getAllByRole("checkbox")).toHaveLength(4);
    expect(screen.queryByRole("textbox", { name: /^Posters/ })).toBeNull();
    // Label, subject when it differs, kind pill, namespaces cut with +N, last seen.
    const bot = must(
      within(list)
        .getByRole("checkbox", { name: /team-bot/ })
        .closest("li"),
      "team-bot row",
    );
    expect(bot).toHaveTextContent("sa:team-bot-9f8e7d");
    expect(bot).toHaveTextContent("Service account");
    const alice = must(
      within(list)
        .getByRole("checkbox", { name: /^alice@example\.com/ })
        .closest("li"),
      "alice row",
    );
    expect(alice).toHaveTextContent("Person");
    expect(alice).toHaveTextContent("team, other, qa +2");
    expect(alice).toHaveTextContent(/last seen .*ago/);
  });

  it("listed principals start checked; a writer on the owner namespace is checked, disabled and explained", async () => {
    const list = await openEditing();
    expect(within(list).getByRole("checkbox", { name: /^alice@example\.com/ })).toBeChecked();
    expect(within(list).getByRole("checkbox", { name: /sa:retired-1a2b3c/ })).toBeChecked();
    expect(within(list).getByRole("checkbox", { name: /carol@example\.com/ })).not.toBeChecked();
    const bot = within(list).getByRole("checkbox", { name: /team-bot/ });
    expect(bot).toBeChecked();
    expect(bot).toBeDisabled();
    expect(bot.closest("li")).toHaveTextContent("already a writer here");
    expect(screen.getByText(/2 listed as posters/)).toBeInTheDocument();
  });

  it("the filter narrows the rows over label, subject and namespaces", async () => {
    const list = await openEditing();
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "Filter posters" }), "carol");
    expect(within(list).getAllByRole("checkbox")).toHaveLength(1);
    expect(within(list).getByRole("checkbox", { name: /carol@example\.com/ })).toBeInTheDocument();
    await user.clear(screen.getByRole("textbox", { name: "Filter posters" }));
    await user.type(screen.getByRole("textbox", { name: "Filter posters" }), "code-y");
    expect(within(list).getAllByRole("checkbox")).toHaveLength(1);
    expect(within(list).getByRole("checkbox", { name: /^alice@example\.com/ })).toBeInTheDocument();
    await user.clear(screen.getByRole("textbox", { name: "Filter posters" }));
    await user.type(screen.getByRole("textbox", { name: "Filter posters" }), "nobody-here");
    expect(screen.queryByRole("list", { name: "Posters" })).toBeNull();
    expect(screen.getByText("No identity matches the filter.")).toBeInTheDocument();
    // The directory was not re-queried: the list fit, so the filter is client-side.
    expect(tasks.listPrincipals).toHaveBeenCalledTimes(1);
  });

  it("saving sends the checked subjects as posters, without the disabled writer", async () => {
    const list = await openEditing();
    const user = userEvent.setup();
    await user.click(within(list).getByRole("checkbox", { name: /carol@example\.com/ }));
    await user.click(within(list).getByRole("checkbox", { name: /sa:retired-1a2b3c/ }));
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(tasks.registerInbox).toHaveBeenCalledTimes(1));
    const sent = vi.mocked(tasks.registerInbox).mock.calls[0][1];
    expect(sent.inbox).toBe("private-bot");
    expect(sent.if_match_version).toBe(1);
    expect([...(sent.posters ?? [])].sort()).toEqual(["alice@example.com", "carol@example.com"]);
  });

  it("a new inbox needs its name first; then the directory is marked against that name", async () => {
    render(<InboxSettingsDialog open onOpenChange={noop} identity={identity} inbox={null} onSaved={noop} />);
    await screen.findByRole("dialog");
    expect(screen.getByText("Name the inbox to choose posters.")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Filter posters" })).toBeDisabled();
    expect(tasks.listPrincipals).not.toHaveBeenCalled();
    await userEvent.setup().type(screen.getByRole("textbox", { name: /^Inbox/ }), "new-bot");
    await waitFor(() => expect(tasks.listPrincipals).toHaveBeenCalledWith(identity, { inbox: "new-bot", query: "" }));
    // Typing debounced: one call for the finished name, not one per keystroke.
    expect(tasks.listPrincipals).toHaveBeenCalledTimes(1);
    expect(await screen.findByRole("checkbox", { name: /carol@example\.com/ })).not.toBeChecked();
  });

  it("a directory error is shown and the current selection stays", async () => {
    vi.mocked(tasks.listPrincipals).mockRejectedValueOnce(new Error("directory down"));
    render(<InboxSettingsDialog open onOpenChange={noop} identity={identity} inbox={INBOX} onSaved={noop} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/directory down/);
    // The inbox's own posters still render (as rows the admin can uncheck) and would be sent as-is.
    const list = screen.getByRole("list", { name: "Posters" });
    expect(within(list).getAllByRole("checkbox")).toHaveLength(2);
    expect(within(list).getByRole("checkbox", { name: /^alice@example\.com/ })).toBeChecked();
    await userEvent.setup().click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(tasks.registerInbox).toHaveBeenCalledTimes(1));
    expect(vi.mocked(tasks.registerInbox).mock.calls[0][1].posters).toEqual(["alice@example.com", "sa:retired-1a2b3c"]);
  });
});
