import { cleanup, render, screen, waitFor } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../api";
import { toast } from "../../components/Toast";
import type { MemoryProposal } from "../../api";
import { useStore } from "../../lib/state";
import { resetNamespaceRoleCache } from "../../lib/namespaceRole";
import { ProposalsPanel } from "./Proposals";

vi.mock("../../components/Toast", () => ({ toast: vi.fn(), toastError: vi.fn(), ToastViewport: () => null }));
vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, listProposals: vi.fn(), decideProposals: vi.fn(async () => ({ decided: 1, results: [] })) };
});

const P: MemoryProposal = {
  memory_key: "deploy_window",
  memory_id: "m1",
  agent: "claude-code",
  value_preview: "Deploys run on Tuesdays",
  sensitivity: "",
  created_at: "2000-01-01 00:00:00",
  graded_passes: 4,
  graded_fails: 0,
  used_count: 4,
  harm_count: 0,
  ready: "accept",
};

function signIn(role: "viewer" | "writer") {
  useStore.setState({
    identity: { org: "acme", namespace: "team", agent: "loom-ui" },
    whoami: { authenticated: true, subject: "alice", org: "acme", roles: ["writer"] },
    myNamespaces: [{ namespace: "team", role, status: "active" } as never],
  });
}

beforeEach(() => {
  resetNamespaceRoleCache();
  signIn("writer");
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ProposalsPanel", () => {
  it("shows the probe's rows without reading again, and accepts one", async () => {
    vi.mocked(api.listProposals).mockResolvedValue({ proposals: [], total: 0 });
    render(<ProposalsPanel initial={{ rows: [P], total: 1 }} onSeedUsed={() => {}} />);
    expect(screen.getByText("Deploys run on Tuesdays")).toBeInTheDocument();
    expect(screen.getByText("ready to accept")).toBeInTheDocument();
    expect(api.listProposals).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Accept deploy_window" }));
    await waitFor(() =>
      expect(api.decideProposals).toHaveBeenCalledWith({ org: "acme", namespace: "team" }, ["deploy_window"], "accept"),
    );
    expect(await screen.findByText("No proposals")).toBeInTheDocument();
  });

  it("offers a viewer the proposals and no decision", async () => {
    signIn("viewer");
    render(<ProposalsPanel initial={{ rows: [P], total: 1 }} onSeedUsed={() => {}} />);
    expect(screen.getByText("Deploys run on Tuesdays")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Accept deploy_window" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Actions for deploy_window" })).toBeNull();
  });

  it("says a decision did not happen, and reads the list again", async () => {
    vi.mocked(api.decideProposals).mockResolvedValueOnce({
      decided: 0,
      results: [{ memory_key: "deploy_window", decided: false, reason: "already accepted" }],
    });
    vi.mocked(api.listProposals).mockResolvedValue({ proposals: [], total: 0 });
    render(<ProposalsPanel initial={{ rows: [P], total: 1 }} onSeedUsed={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Actions for deploy_window" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Reject" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Not decided: already accepted", "info"));
    await waitFor(() => expect(api.listProposals).toHaveBeenCalled());
  });

  it("keeps two agents' proposals of one key apart, shows a high one as sensitive, and says the list is cut", async () => {
    const other = { ...P, memory_id: "m2", agent: "cursor", value_preview: "", sensitivity: "high" as const };
    render(<ProposalsPanel initial={{ rows: [P, other], total: 140 }} onSeedUsed={() => {}} />);
    expect(screen.getAllByText("deploy_window")).toHaveLength(2);
    expect(screen.getByText("sensitive")).toBeInTheDocument();
    expect(screen.getByText("The newest 2 of 140 proposals; decide some to see the rest.")).toBeInTheDocument();
  });
});
