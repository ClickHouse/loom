import { cleanup, fireEvent, render, screen, waitFor, within } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../api";
import type { ErasureListRow, WhoAmI } from "../../api";
import { useStore } from "../../lib/state";
import { ErasuresPanel } from "./Erasures";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    listErasures: vi.fn(async () => ({ rows: [] })),
    requestErasure: vi.fn(async () => ({
      erasure_id: "e2",
      status: "completed",
      steps: [],
      residue_count: 0,
      failure_reason: "",
    })),
    getErasureStatus: vi.fn(),
    exportErasureScope: vi.fn(async () => ({ counts: { memory: 1 } })),
  };
});
vi.mock("../../components/ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));

const ROW: ErasureListRow = {
  erasure_id: "e1",
  scope_type: "namespace",
  scope_value: "ns1",
  requester: "alice",
  status: "completed",
  requested_at: "2000-01-01 00:00:00",
  completed_at: "2000-01-01 00:00:05",
  failure_reason: "",
  steps_completed_count: 9,
  reversals_count: 0,
};

function signIn(roles: string[], grant: "admin" | "writer" = "writer") {
  const whoami: WhoAmI = { authenticated: true, subject: "alice", org: "dev", roles };
  useStore.setState({
    identity: { org: "dev", namespace: "ns1", agent: "loom-ui" },
    whoami,
    myNamespaces: [{ namespace: "ns1", role: grant, status: "active" } as never],
  });
}

beforeEach(() => signIn(["writer"]));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ErasuresPanel", () => {
  it("offers no erase or export to a caller who is not admin", async () => {
    render(<ErasuresPanel />);
    expect(await screen.findByText("No erasures yet")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Erase data" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Export data" })).toBeNull();
  });

  it("erases the current namespace for its admin, after a confirmation, and reloads the list", async () => {
    signIn(["writer"], "admin");
    render(<ErasuresPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Erase data" }));
    const dialog = await screen.findByRole("dialog", { name: "Erase data" });
    // A namespace admin is not an org admin: the namespace is the only scope.
    expect(within(dialog).queryByRole("radiogroup")).toBeNull();
    await userEvent.click(within(dialog).getByRole("button", { name: "Erase" }));
    await waitFor(() =>
      expect(api.requestErasure).toHaveBeenCalledWith(
        "dev",
        { scope_type: "namespace", scope_value: "ns1" },
        "alice",
        false,
      ),
    );
    await waitFor(() => expect(api.listErasures).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("dialog", { name: "Erase data" })).toBeNull();
  });

  it("lets an org admin erase one user, and refuses a user id that names no one", async () => {
    signIn(["org_admin"]);
    render(<ErasuresPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Erase data" }));
    const dialog = await screen.findByRole("dialog", { name: "Erase data" });
    await userEvent.click(within(dialog).getByRole("radio", { name: "One user, across the org" }));
    const field = within(dialog).getByLabelText("User id");
    await userEvent.type(field, "-");
    expect(within(dialog).getByText("Name one user")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Erase" })).toBeDisabled();
    await userEvent.clear(field);
    await userEvent.type(field, "bob");
    await userEvent.click(within(dialog).getByRole("checkbox", { name: /Purge/ }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Erase" }));
    await waitFor(() =>
      expect(api.requestErasure).toHaveBeenCalledWith(
        "dev",
        { scope_type: "user_id", scope_value: "bob" },
        "alice",
        true,
      ),
    );
  });

  it("exports the scope as a JSON file", async () => {
    signIn(["admin"]);
    const createUrl = vi.fn(() => "blob:x");
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: createUrl, revokeObjectURL: vi.fn() }));
    render(<ErasuresPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Export data" }));
    const dialog = await screen.findByRole("dialog", { name: "Export data" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Download" }));
    await waitFor(() =>
      expect(api.exportErasureScope).toHaveBeenCalledWith(
        "dev",
        { scope_type: "namespace", scope_value: "ns1" },
        "alice",
      ),
    );
    expect(createUrl).toHaveBeenCalledTimes(1);
    vi.unstubAllGlobals();
  });

  it("shows a saga's status and what it left behind", async () => {
    vi.mocked(api.listErasures).mockResolvedValueOnce({ rows: [ROW] });
    vi.mocked(api.getErasureStatus).mockResolvedValueOnce({
      found: true,
      erasure_id: "e1",
      scope_type: "namespace",
      scope_value: "ns1",
      requester: "alice",
      status: "failed",
      current_step: 3,
      requested_at: "2000-01-01 00:00:00",
      completed_at: null,
      failure_reason: "residue in loom.memory",
      residue_count: 2,
      residue: { "loom.memory": 2, "loom.chunks": 0 },
      verified_at: null,
    });
    render(<ErasuresPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Details of erasure ns1" }));
    const dialog = await screen.findByRole("dialog", { name: "Erasure details" });
    expect(await within(dialog).findByText("residue in loom.memory")).toBeInTheDocument();
    expect(within(dialog).getByText("loom.memory: 2")).toBeInTheDocument();
    expect(within(dialog).queryByText(/loom.chunks/)).toBeNull();
    expect(api.getErasureStatus).toHaveBeenCalledWith("e1", expect.anything());
  });
});
