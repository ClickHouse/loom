/** A namespace admin can forget every memory one agent holds there; the agent's name is
 *  typed again first, and nobody else sees the action. */
import { cleanup, render, screen, waitFor, within } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../../lib/state";
import { Agents } from "./Agents";
import { toast } from "../../components/Toast";

const clearAgentMemories = vi.fn(async (_id: unknown, _agent: string, _signal?: AbortSignal) => ({
  rows_affected: { "loom.memory": 7, "loom.memory_embeddings": 7 },
  total: 14,
  locked_skipped: 1,
}));
vi.mock("../../components/Toast", () => ({ toast: vi.fn(), toastError: vi.fn(), ToastViewport: () => null }));
vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    listConnectedAgents: vi.fn(async () => ({
      rows: [
        {
          agent: "claude-code",
          ops: 3,
          namespaces_touched: 1,
          primary_namespace: "team",
          cost_usd: 0,
          last_seen: new Date().toISOString(),
        },
        { agent: "-", ops: 1, namespaces_touched: 1, primary_namespace: "team", cost_usd: 0, last_seen: "" },
      ],
    })),
    clearAgentMemories: (id: unknown, agent: string, signal?: AbortSignal) => clearAgentMemories(id, agent, signal),
  };
});

function signIn(role: "admin" | "writer") {
  useStore.setState({
    identity: { org: "acme", namespace: "team", agent: "loom-ui" },
    whoami: { authenticated: true, subject: "alice", org: "acme", roles: ["writer"] },
    myNamespaces: [{ namespace: "team", role, status: "active" } as never],
  });
}

beforeEach(() => clearAgentMemories.mockClear());
afterEach(() => {
  cleanup();
  useStore.setState({ identity: null, whoami: null, myNamespaces: null });
});

describe("Agents: forget an agent's memories", () => {
  it("shows no row menu to a writer", async () => {
    signIn("writer");
    render(<Agents />);
    expect(await screen.findByText("claude-code")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Actions for claude-code" })).toBeNull();
  });

  it("clears the agent's memories once its name is typed", async () => {
    signIn("admin");
    render(<Agents />);
    await userEvent.click(await screen.findByRole("button", { name: "Actions for claude-code" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: /Forget its memories in team/ }));
    const dialog = await screen.findByRole("dialog", { name: "Forget memories" });
    const submit = within(dialog).getByRole("button", { name: "Forget memories" });
    expect(submit).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText("Type claude-code to confirm"), "claude-code");
    await userEvent.click(submit);
    await waitFor(() =>
      expect(clearAgentMemories).toHaveBeenCalledWith(
        expect.objectContaining({ org: "acme", namespace: "team" }),
        "claude-code",
        expect.any(AbortSignal),
      ),
    );
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Forget memories" })).toBeNull());
    expect(toast).toHaveBeenCalledWith("Forgot 7 memories of claude-code; 1 locked kept", "ok");
  });

  it("keeps the dialog open with the reason when the clear is refused", async () => {
    signIn("admin");
    clearAgentMemories.mockRejectedValueOnce(new Error("not admin"));
    render(<Agents />);
    await userEvent.click(await screen.findByRole("button", { name: "Actions for claude-code" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: /Forget its memories in team/ }));
    const dialog = await screen.findByRole("dialog", { name: "Forget memories" });
    await userEvent.type(within(dialog).getByLabelText("Type claude-code to confirm"), "claude-code");
    await userEvent.click(within(dialog).getByRole("button", { name: "Forget memories" }));
    expect(await within(dialog).findByRole("alert")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Forget memories" })).toBeEnabled();
  });

  it("keeps the confirm off for a name typed wrong, and offers no forget for the `-` agent", async () => {
    signIn("admin");
    render(<Agents />);
    await userEvent.click(await screen.findByRole("button", { name: "Actions for claude-code" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: /Forget its memories in team/ }));
    const dialog = await screen.findByRole("dialog", { name: "Forget memories" });
    await userEvent.type(within(dialog).getByLabelText("Type claude-code to confirm"), "claude-cod");
    expect(within(dialog).getByRole("button", { name: "Forget memories" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Actions for -" })).toBeNull();
  });
});
