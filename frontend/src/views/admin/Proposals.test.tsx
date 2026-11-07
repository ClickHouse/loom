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

  it("offers no decision on a key two agents proposed, shows a high one as sensitive, and says the list is cut", async () => {
    const other = { ...P, memory_id: "m2", agent: "cursor", value_preview: "", sensitivity: "high" as const };
    const lone = { ...P, memory_id: "m3", memory_key: "standup_time" };
    render(<ProposalsPanel initial={{ rows: [P, other, lone], total: 140 }} onSeedUsed={() => {}} />);
    expect(screen.getAllByText("deploy_window")).toHaveLength(2);
    // A decision names only the key, so neither of the two is offered one.
    expect(screen.queryByRole("button", { name: "Accept deploy_window" })).toBeNull();
    expect(screen.getAllByText("Decide over the API")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Accept standup_time" })).toBeInTheDocument();
    expect(screen.getByText("sensitive")).toBeInTheDocument();
    expect(screen.getByText("The newest 3 proposals are listed; decide some to see the rest.")).toBeInTheDocument();
  });

  it("holds every row until the list is read again after a decision", async () => {
    let answer: (v: { proposals: MemoryProposal[]; total: number }) => void = () => {};
    vi.mocked(api.listProposals).mockImplementationOnce(() => new Promise((r) => (answer = r)));
    const lone = { ...P, memory_id: "m3", memory_key: "standup_time" };
    render(<ProposalsPanel initial={{ rows: [P, lone], total: 2 }} onSeedUsed={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Accept deploy_window" }));
    await waitFor(() => expect(api.listProposals).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: "Accept standup_time" })).toBeDisabled();
    answer({ proposals: [lone], total: 1 });
    await waitFor(() => expect(screen.getByRole("button", { name: "Accept standup_time" })).toBeEnabled());
  });
});
