/** A pattern's sweep deletes what it already matches: a namespace pattern for a writer of
 *  the namespace, an org pattern for an org admin only, as the sweep handler decides. */
import { cleanup, render, screen, waitFor } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../api";
import * as guardrails from "../../api/guardrails";
import type { BlacklistRow, MyNamespaceEntry, WhoAmI } from "../../api";
import { useStore } from "../../lib/state";
import { resetNamespaceRoleCache } from "../../lib/namespaceRole";
import { confirmAction } from "../../components/ConfirmDialog";
import { toast } from "../../components/Toast";
import { Blacklist, PrivacyFilters } from "./Blacklist";

vi.mock("../../components/ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));
vi.mock("../../components/Toast", () => ({ toast: vi.fn(), toastError: vi.fn(), ToastViewport: () => null }));
vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    listBlacklist: vi.fn(),
  };
});
vi.mock("../../api/guardrails", () => ({
  sweepBlacklistPattern: vi.fn(async () => ({ matched: 3, tombstoned: 3, truncated: false, error: "" })),
  getOrgModelCalls: vi.fn(() => new Promise(() => {})),
  getExtractionPolicy: vi.fn(() => new Promise(() => {})),
}));

const row = (pattern_id: string, scope: BlacklistRow["scope"]): BlacklistRow =>
  ({
    pattern_id,
    pattern: `secret-${pattern_id}`,
    pattern_type: "substring",
    scope,
    reason: "",
    created_by: "a@x",
    hit_count: 0,
    last_hit_at: null,
    created_at: "2000-01-01 00:00:00",
  }) as BlacklistRow;

function signIn(roles: string[]) {
  const whoami: WhoAmI = { authenticated: true, subject: "a@x", org: "acme", roles, namespaces: ["team"] };
  const grant = { namespace: "team", role: "writer", status: "active" } as MyNamespaceEntry;
  useStore.setState({ whoami, myNamespaces: [grant], identity: { org: "acme", namespace: "team", agent: "loom-ui" } });
}

beforeEach(() => {
  resetNamespaceRoleCache();
  vi.mocked(api.listBlacklist).mockResolvedValue({ rows: [row("p1", "namespace"), row("p2", "org")] });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  useStore.setState({ whoami: null, myNamespaces: null, identity: null });
});

/** Opens a pattern's row menu and picks Delete matches. */
async function sweepOf(pattern: string) {
  await userEvent.click(await screen.findByRole("button", { name: `Actions for ${pattern}` }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Delete matches" }));
}

describe("PrivacyFilters sweep", () => {
  it("says when a sweep failed, when nothing matched and when more matches remain", async () => {
    signIn(["writer"]);
    vi.mocked(guardrails.sweepBlacklistPattern)
      .mockResolvedValueOnce({ matched: 0, tombstoned: 0, truncated: false, error: "the pattern no longer compiles" })
      .mockResolvedValueOnce({ matched: 0, tombstoned: 0, truncated: false, error: "" })
      .mockResolvedValueOnce({ matched: 5000, tombstoned: 5000, truncated: true, error: "" });
    render(<PrivacyFilters />);
    await sweepOf("secret-p1");
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Sweep failed: the pattern no longer compiles", "error"));
    await sweepOf("secret-p1");
    await waitFor(() => expect(toast).toHaveBeenCalledWith("No stored memory in team matches this pattern", "info"));
    await sweepOf("secret-p1");
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(
        "Deleted 5000 of 5000 matching memories in team; more remain, sweep again",
        "ok",
      ),
    );
  });

  it("deletes nothing when the confirmation is declined", async () => {
    signIn(["writer"]);
    vi.mocked(confirmAction).mockResolvedValueOnce(false);
    render(<PrivacyFilters />);
    await sweepOf("secret-p1");
    await waitFor(() => expect(confirmAction).toHaveBeenCalled());
    expect(guardrails.sweepBlacklistPattern).not.toHaveBeenCalled();
  });

  it("shows the org's guardrails on the page to an org admin claim only", async () => {
    signIn(["writer"]);
    render(<Blacklist />);
    await screen.findByRole("button", { name: "Actions for secret-p1" });
    expect(screen.queryByRole("heading", { name: "Model calls" })).toBeNull();
    cleanup();

    signIn(["org_admin"]);
    render(<Blacklist />);
    expect(await screen.findByRole("heading", { name: "Model calls" })).toBeInTheDocument();
  });

  it("offers a writer the sweep of a namespace pattern, not of an org or global one", async () => {
    vi.mocked(api.listBlacklist).mockResolvedValue({
      rows: [row("p1", "namespace"), row("p2", "org"), row("p3", "global")],
    });
    signIn(["writer"]);
    render(<PrivacyFilters />);
    await sweepOf("secret-p1");
    expect(screen.queryByRole("button", { name: "Actions for secret-p2" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Actions for secret-p3" })).toBeNull();
    await waitFor(() =>
      expect(guardrails.sweepBlacklistPattern).toHaveBeenCalledWith(
        expect.objectContaining({ namespace: "team" }),
        "p1",
      ),
    );
  });

  it("offers an org admin the sweep of an org and a global pattern", async () => {
    vi.mocked(api.listBlacklist).mockResolvedValue({ rows: [row("p2", "org"), row("p3", "global")] });
    signIn(["org_admin"]);
    render(<PrivacyFilters />);
    expect(await screen.findByRole("button", { name: "Actions for secret-p3" })).toBeInTheDocument();
    await sweepOf("secret-p2");
    expect(confirmAction).toHaveBeenCalledWith(
      expect.objectContaining({ title: expect.stringContaining("across acme") }),
    );
    await waitFor(() => expect(guardrails.sweepBlacklistPattern).toHaveBeenCalledWith(expect.anything(), "p2"));
  });
});
