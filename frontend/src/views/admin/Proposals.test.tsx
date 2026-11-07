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

/** Renders the panel over `rows` (of `total`), as its read on open answers them. */
async function show(rows: MemoryProposal[], total = rows.length) {
  vi.mocked(api.listProposals).mockResolvedValueOnce({ proposals: rows, total });
  render(<ProposalsPanel />);
  await screen.findAllByText(rows[0].memory_key);
}

describe("ProposalsPanel", () => {
  it("reads the proposals when it opens, and accepts one", async () => {
    await show([P]);
    vi.mocked(api.listProposals).mockResolvedValue({ proposals: [], total: 0 });
    expect(screen.getByText("Deploys run on Tuesdays")).toBeInTheDocument();
    expect(screen.getByText("ready to accept")).toBeInTheDocument();
    expect(api.listProposals).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "Accept deploy_window" }));
    await waitFor(() =>
      expect(api.decideProposals).toHaveBeenCalledWith({ org: "acme", namespace: "team" }, ["deploy_window"], "accept"),
    );
    expect(await screen.findByText("No proposals")).toBeInTheDocument();
  });

  it("offers a viewer the proposals and no decision", async () => {
    signIn("viewer");
    await show([P], 1);
    expect(screen.getByText("Deploys run on Tuesdays")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Accept deploy_window" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Actions for deploy_window" })).toBeNull();
  });

  it("says a decision did not happen, and reads the list again", async () => {
    vi.mocked(api.decideProposals).mockResolvedValueOnce({
      decided: 0,
      results: [{ memory_key: "deploy_window", decided: false, status: "active", reason: "already accepted" }],
    });
    vi.mocked(api.listProposals).mockResolvedValue({ proposals: [], total: 0 });
    await show([P], 1);
    await userEvent.click(screen.getByRole("button", { name: "Actions for deploy_window" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Reject" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Not decided: already accepted", "info"));
    await waitFor(() => expect(api.listProposals).toHaveBeenCalledTimes(2));
  });

  it("offers no decision on a key two agents proposed, shows a high one as sensitive, and says the list is cut", async () => {
    const other = { ...P, memory_id: "m2", agent: "cursor", value_preview: "", sensitivity: "high" as const };
    const lone = { ...P, memory_id: "m3", memory_key: "standup_time" };
    await show([P, other, lone], 140);
    expect(screen.getAllByText("deploy_window")).toHaveLength(2);
    // A decision names only the key, so neither of the two can take one; each says why.
    for (const accept of screen.getAllByRole("button", { name: "Accept deploy_window" })) {
      expect(accept).toHaveAttribute("aria-disabled", "true");
      expect(accept).toHaveAccessibleDescription(/More than one proposal holds this key/);
    }
    expect(screen.queryByRole("button", { name: "Actions for deploy_window" })).toBeNull();
    expect(screen.getByRole("button", { name: "Accept standup_time" })).toBeInTheDocument();
    expect(screen.getByText("sensitive")).toBeInTheDocument();
    expect(screen.getByText("The newest 3 proposals are listed; decide some to see the rest.")).toBeInTheDocument();
  });

  it("says an accepted fact that contradicts another waits in Conflicts", async () => {
    vi.mocked(api.decideProposals).mockResolvedValueOnce({
      decided: 1,
      results: [{ memory_key: "deploy_window", decided: true, status: "conflicted", reason: "" }],
    });
    vi.mocked(api.listProposals).mockResolvedValue({ proposals: [], total: 0 });
    await show([P], 1);
    await userEvent.click(screen.getByRole("button", { name: "Accept deploy_window" }));
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith("Accepted; it contradicts another fact, so it waits in Conflicts", "ok"),
    );
  });

  it("holds every row until the list is read again after a decision", async () => {
    let answer: (v: { proposals: MemoryProposal[]; total: number }) => void = () => {};
    const lone = { ...P, memory_id: "m3", memory_key: "standup_time" };
    await show([P, lone], 2);
    vi.mocked(api.listProposals).mockImplementationOnce(() => new Promise((r) => (answer = r)));
    await userEvent.click(screen.getByRole("button", { name: "Accept deploy_window" }));
    await waitFor(() => expect(api.listProposals).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("button", { name: "Accept standup_time" })).toBeDisabled();
    answer({ proposals: [lone], total: 1 });
    await waitFor(() => expect(screen.getByRole("button", { name: "Accept standup_time" })).toBeEnabled());
  });
});
