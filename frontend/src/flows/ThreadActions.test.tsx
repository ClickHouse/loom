import { cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import { confirmAction } from "../components/ConfirmDialog";
import { toast } from "../components/Toast";
import { ThreadActions } from "./ThreadActions";

vi.mock("../components/ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));
vi.mock("../components/Toast", () => ({ toast: vi.fn(), toastError: vi.fn(), ToastViewport: () => null }));
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    renameThread: vi.fn(async () => ({ updated: true })),
    createThreadRow: vi.fn(async () => ({ thread_id: "t1" })),
    deleteThread: vi.fn(async () => ({ total: 3 })),
    getConversationThread: vi.fn(async () => ({ found: false, msg_count: 0, agent: "" })),
  };
});

const identity = { org: "acme", namespace: "team" };
const thread = { thread_id: "t1", agent: "claude-code", title: "Deploy plan" };

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ThreadActions", () => {
  it("offers nothing to a role that reaches neither op", () => {
    render(
      <ThreadActions identity={identity} thread={thread} canRename={false} canDelete={false} onChanged={vi.fn()} />,
    );
    expect(screen.queryByRole("button", { name: "Actions for Deploy plan" })).toBeNull();
  });

  it("renames a thread and deletes one with the thread id confirmed", async () => {
    vi.mocked(api.getConversationThread).mockResolvedValueOnce({
      found: true,
      msg_count: 3,
      agent: "claude-code",
      created_at: "2000-01-01 00:00:00",
    } as never);
    const onChanged = vi.fn();
    render(<ThreadActions identity={identity} thread={thread} canRename canDelete onChanged={onChanged} />);
    await userEvent.click(screen.getByRole("button", { name: "Actions for Deploy plan" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Rename" }));
    const dialog = await screen.findByRole("dialog", { name: "Rename conversation" });
    const field = within(dialog).getByLabelText("Title");
    await userEvent.clear(field);
    await userEvent.type(field, "Release plan");
    await userEvent.click(within(dialog).getByRole("button", { name: "Rename" }));
    await waitFor(() => expect(api.renameThread).toHaveBeenCalledWith(identity, "claude-code", "t1", "Release plan"));
    await userEvent.click(screen.getByRole("button", { name: "Actions for Deploy plan" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
    await waitFor(() => expect(api.deleteThread).toHaveBeenCalledWith(identity, "claude-code", "t1"));
    expect(onChanged).toHaveBeenCalledTimes(2);
  });

  it("titles a thread built by appends, which has no row to update, by writing one", async () => {
    vi.mocked(api.getConversationThread).mockResolvedValueOnce({
      found: true,
      msg_count: 1,
      agent: "claude-code",
      created_at: null,
    } as never);
    render(<ThreadActions identity={identity} thread={thread} canRename canDelete={false} onChanged={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Actions for Deploy plan" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Rename" }));
    const dialog = await screen.findByRole("dialog", { name: "Rename conversation" });
    await userEvent.clear(within(dialog).getByLabelText("Title"));
    await userEvent.type(within(dialog).getByLabelText("Title"), "Release plan");
    await userEvent.click(within(dialog).getByRole("button", { name: "Rename" }));
    await waitFor(() =>
      expect(api.createThreadRow).toHaveBeenCalledWith(identity, "claude-code", "t1", "Release plan"),
    );
    expect(api.renameThread).not.toHaveBeenCalled();
  });

  it("writes the row when it was deleted after the read but turns remain", async () => {
    vi.mocked(api.getConversationThread)
      .mockResolvedValueOnce({ found: true, msg_count: 1, agent: "codex", created_at: "2000-01-01 00:00:00" } as never)
      .mockResolvedValueOnce({ found: true, msg_count: 1, agent: "cursor", created_at: null } as never);
    vi.mocked(api.renameThread).mockResolvedValueOnce({ updated: false });
    render(<ThreadActions identity={identity} thread={thread} canRename canDelete={false} onChanged={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Actions for Deploy plan" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Rename" }));
    const dialog = await screen.findByRole("dialog", { name: "Rename conversation" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Rename" }));
    await waitFor(() => expect(api.createThreadRow).toHaveBeenCalledWith(identity, "claude-code", "t1", "Deploy plan"));
  });

  it("renames as the agent that owns the thread's row, not the last writer", async () => {
    vi.mocked(api.getConversationThread).mockResolvedValueOnce({
      found: true,
      msg_count: 3,
      agent: "codex",
      created_at: "2000-01-01 00:00:00",
    } as never);
    render(<ThreadActions identity={identity} thread={thread} canRename canDelete={false} onChanged={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Actions for Deploy plan" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Rename" }));
    const dialog = await screen.findByRole("dialog", { name: "Rename conversation" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Rename" }));
    await waitFor(() => expect(api.renameThread).toHaveBeenCalledWith(identity, "codex", "t1", "Deploy plan"));
    expect(api.createThreadRow).not.toHaveBeenCalled();
  });

  it("deletes the row an agent without turns owns", async () => {
    vi.mocked(api.getConversationThread)
      .mockResolvedValueOnce({ found: true, msg_count: 0, agent: "codex" } as never)
      .mockResolvedValueOnce({ found: false, msg_count: 0, agent: "" } as never);
    render(<ThreadActions identity={identity} thread={thread} canRename={false} canDelete onChanged={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Actions for Deploy plan" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith('Deleted "Deploy plan"', "ok"));
    expect(vi.mocked(api.deleteThread).mock.calls.map((c) => c[1])).toEqual(["claude-code", "codex"]);
  });

  it("says a delete failed after a part of the thread was deleted", async () => {
    vi.mocked(api.getConversationThread)
      .mockResolvedValueOnce({ found: true, msg_count: 2, agent: "codex" } as never)
      .mockRejectedValueOnce(new Error("down"));
    render(<ThreadActions identity={identity} thread={thread} canRename={false} canDelete onChanged={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Actions for Deploy plan" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith('Deleted part of "Deploy plan", then failed: down', "error"),
    );
  });

  it("deletes each agent's part of a thread, and says no restore is offered", async () => {
    vi.mocked(api.getConversationThread)
      .mockResolvedValueOnce({ found: true, msg_count: 2, agent: "codex" } as never)
      .mockResolvedValueOnce({ found: false, msg_count: 0, agent: "" } as never);
    render(<ThreadActions identity={identity} thread={thread} canRename={false} canDelete onChanged={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Actions for Deploy plan" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith('Deleted "Deploy plan"', "ok"));
    expect(vi.mocked(api.deleteThread).mock.calls.map((c) => c[1])).toEqual(["claude-code", "codex"]);
    expect(confirmAction).toHaveBeenCalledWith(
      expect.objectContaining({ message: expect.stringContaining("cannot bring them back") }),
    );
  });

  it("says what is left when a delete leaves messages behind", async () => {
    vi.mocked(api.getConversationThread).mockResolvedValueOnce({
      found: true,
      msg_count: 4,
      agent: "claude-code",
    } as never);
    render(<ThreadActions identity={identity} thread={thread} canRename={false} canDelete onChanged={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Actions for Deploy plan" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith('Deleted part of "Deploy plan"; 4 messages are left', "info"),
    );
  });

  it("keeps the count it read when the next agent's delete fails", async () => {
    vi.mocked(api.getConversationThread).mockResolvedValueOnce({ found: true, msg_count: 2, agent: "codex" } as never);
    vi.mocked(api.deleteThread).mockResolvedValueOnce({ total: 1 }).mockRejectedValueOnce(new Error("down"));
    render(<ThreadActions identity={identity} thread={thread} canRename={false} canDelete onChanged={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Actions for Deploy plan" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(
        'Deleted part of "Deploy plan", then failed: down; 2 messages are left',
        "error",
      ),
    );
  });

  it("writes no row for a conversation deleted while the rename was open, and reads the list again", async () => {
    vi.mocked(api.getConversationThread).mockResolvedValueOnce({ found: false, msg_count: 0, agent: "" } as never);
    const onChanged = vi.fn();
    render(<ThreadActions identity={identity} thread={thread} canRename canDelete={false} onChanged={onChanged} />);
    await userEvent.click(screen.getByRole("button", { name: "Actions for Deploy plan" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Rename" }));
    const dialog = await screen.findByRole("dialog", { name: "Rename conversation" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Rename" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("This conversation was deleted", "info"));
    expect(onChanged).toHaveBeenCalled();
    expect(api.renameThread).not.toHaveBeenCalled();
    expect(api.createThreadRow).not.toHaveBeenCalled();
  });

  it("writes no row when the conversation went between the read and the update", async () => {
    vi.mocked(api.getConversationThread)
      .mockResolvedValueOnce({ found: true, msg_count: 1, agent: "codex", created_at: "2000-01-01 00:00:00" } as never)
      .mockResolvedValueOnce({ found: false, msg_count: 0, agent: "" } as never);
    vi.mocked(api.renameThread).mockResolvedValueOnce({ updated: false });
    const onChanged = vi.fn();
    render(<ThreadActions identity={identity} thread={thread} canRename canDelete={false} onChanged={onChanged} />);
    await userEvent.click(screen.getByRole("button", { name: "Actions for Deploy plan" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Rename" }));
    const dialog = await screen.findByRole("dialog", { name: "Rename conversation" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Rename" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("This conversation was deleted", "info"));
    expect(api.renameThread).toHaveBeenCalledWith(identity, "codex", "t1", "Deploy plan");
    expect(api.createThreadRow).not.toHaveBeenCalled();
    expect(onChanged).toHaveBeenCalled();
  });
});
