import { cleanup, render, screen, waitFor } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../api";
import type { MemoryProposal } from "../../api";
import { useStore } from "../../lib/state";
import { resetNamespaceRoleCache } from "../../lib/namespaceRole";
import { ProposalsPanel } from "./Proposals";

vi.mock("../../components/Toast", () => ({ toast: vi.fn(), toastError: vi.fn(), ToastViewport: () => null }));
vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, listProposals: vi.fn(), decideProposals: vi.fn(async () => ({ decided: 1 })) };
});

const P: MemoryProposal = {
  memory_key: "deploy_window",
  agent: "claude-code",
  value_preview: "Deploys run on Tuesdays",
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
    render(<ProposalsPanel initial={[P]} />);
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
    render(<ProposalsPanel initial={[P]} />);
    expect(screen.getByText("Deploys run on Tuesdays")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Accept deploy_window" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reject deploy_window" })).toBeNull();
  });
});
