import { cleanup, render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import type { CodeRepoSummary } from "../api";
import { CodeReindexDialog } from "./CodeReindexDialog";

vi.mock("../components/Toast", () => ({ toast: vi.fn(), toastError: vi.fn(), ToastViewport: () => null }));
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, codeReindex: vi.fn(async () => ({ previous_head: "bbb", new_head: "aaa111" })) };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("CodeReindexDialog", () => {
  it("moves the branch's watermark only to a full commit id, then re-reads the card", async () => {
    const onClose = vi.fn();
    const onDone = vi.fn();
    const branch = { org: "acme", namespace: "code-x", repo: "acme/x", branch: "main" } as CodeRepoSummary;
    render(<CodeReindexDialog branch={branch} onDone={onDone} onClose={onClose} />);
    const go = screen.getByRole("button", { name: "Re-walk" });
    const field = screen.getByLabelText("Commit");
    await userEvent.type(field, "aaa111");
    expect(go).toBeDisabled();
    expect(screen.getByText("A full 40-character commit id")).toBeInTheDocument();
    const sha = "a".repeat(40);
    await userEvent.clear(field);
    await userEvent.type(field, sha);
    await userEvent.click(go);
    await waitFor(() => expect(api.codeReindex).toHaveBeenCalledWith({ org: "acme", namespace: "code-x" }, sha));
    expect(onDone).toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });
});
