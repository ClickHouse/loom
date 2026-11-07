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
vi.mock("../../components/Toast", () => ({ toast: vi.fn(), toastError: vi.fn(), ToastViewport: () => null }));
import { toast } from "../../components/Toast";
import { ApiError } from "../../lib/errors";

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
  vi.restoreAllMocks();
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
        expect.anything(),
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
        expect.anything(),
      ),
    );
  });

  it("exports the scope as a JSON file and says which sections were cut", async () => {
    signIn(["admin"]);
    const createUrl = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:x");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    const section = (truncated = false) => ({ rows: [], count: 0, truncated });
    vi.mocked(api.exportErasureScope).mockResolvedValueOnce({
      exported_at: "",
      counts: {},
      memory: section(true),
      knowledge: section(),
      conversations: section(),
      conversation_threads: section(),
      agent_events: section(true),
      audit_log: section(),
    });
    render(<ErasuresPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Export data" }));
    const dialog = await screen.findByRole("dialog", { name: "Export data" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Download" }));
    await waitFor(() =>
      expect(api.exportErasureScope).toHaveBeenCalledWith(
        "dev",
        { scope_type: "namespace", scope_value: "ns1" },
        "alice",
        expect.anything(),
      ),
    );
    expect(createUrl).toHaveBeenCalledTimes(1);
    expect(toast).toHaveBeenCalledWith("The export holds the first 10,000 rows of memory, agent events", "info");
  });

  it("keeps a refused erase in its dialog, and closes on no answer, the saga perhaps still running", async () => {
    signIn(["writer"], "admin");
    vi.mocked(api.requestErasure).mockRejectedValueOnce(new ApiError({ status: 403, message: "not admin" }));
    render(<ErasuresPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Erase data" }));
    let dialog = await screen.findByRole("dialog", { name: "Erase data" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Erase" }));
    expect(await within(dialog).findByRole("alert")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Erase" })).toBeEnabled();
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    vi.mocked(api.requestErasure).mockRejectedValueOnce(new DOMException("timed out", "TimeoutError"));
    await userEvent.click(screen.getByRole("button", { name: "Erase data" }));
    dialog = await screen.findByRole("dialog", { name: "Erase data" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Erase" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Erase data" })).toBeNull());
    expect(toast).toHaveBeenCalledWith("The erasure may still be running; its row shows here once it ends", "info");
  });

  it("says a running saga was not checked yet", async () => {
    vi.mocked(api.listErasures).mockResolvedValueOnce({ rows: [ROW] });
    vi.mocked(api.getErasureStatus).mockResolvedValueOnce({
      found: true,
      erasure_id: "e1",
      scope_type: "namespace",
      scope_value: "ns1",
      requester: "alice",
      status: "in_progress",
      current_step: 2,
      requested_at: "2000-01-01 00:00:00",
      completed_at: null,
      failure_reason: null,
      residue_count: 0,
      residue: {},
      verified_at: null,
    });
    render(<ErasuresPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Details of the erasure requested/ }));
    const dialog = await screen.findByRole("dialog", { name: "Erasure details" });
    expect(await within(dialog).findByText("Not checked yet")).toBeInTheDocument();
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
      verified_at: "2000-01-01 00:00:09",
    });
    render(<ErasuresPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Details of the erasure requested/ }));
    const dialog = await screen.findByRole("dialog", { name: "Erasure details" });
    expect(await within(dialog).findByText("residue in loom.memory")).toBeInTheDocument();
    expect(within(dialog).getByText("loom.memory: 2")).toBeInTheDocument();
    expect(within(dialog).queryByText(/loom.chunks/)).toBeNull();
    expect(api.getErasureStatus).toHaveBeenCalledWith("e1", expect.anything());
  });
});
