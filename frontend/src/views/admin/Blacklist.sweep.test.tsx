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
import { PrivacyFilters } from "./Blacklist";

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

describe("PrivacyFilters sweep", () => {
  it("offers a writer the sweep of a namespace pattern, not of an org one", async () => {
    signIn(["writer"]);
    render(<PrivacyFilters />);
    const sweep = await screen.findByRole("button", { name: "Delete stored memories matching secret-p1" });
    expect(screen.queryByRole("button", { name: "Delete stored memories matching secret-p2" })).toBeNull();
    await userEvent.click(sweep);
    await waitFor(() =>
      expect(guardrails.sweepBlacklistPattern).toHaveBeenCalledWith(
        expect.objectContaining({ namespace: "team" }),
        "p1",
      ),
    );
  });

  it("offers an org admin the sweep of an org pattern", async () => {
    signIn(["org_admin"]);
    render(<PrivacyFilters />);
    expect(
      await screen.findByRole("button", { name: "Delete stored memories matching secret-p2" }),
    ).toBeInTheDocument();
  });
});
