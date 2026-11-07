import { cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import { ThreadActions } from "./ThreadActions";

vi.mock("../components/ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));
vi.mock("../components/Toast", () => ({ toast: vi.fn(), toastError: vi.fn(), ToastViewport: () => null }));
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    renameThread: vi.fn(async () => ({ updated: true })),
    deleteThread: vi.fn(async () => ({ total: 3 })),
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
});
