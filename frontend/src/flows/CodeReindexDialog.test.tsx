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
  it("moves the branch's watermark to the commit typed", async () => {
    const onClose = vi.fn();
    const branch = { org: "acme", namespace: "code-x", repo: "acme/x", branch: "main" } as CodeRepoSummary;
    render(<CodeReindexDialog branch={branch} onClose={onClose} />);
    const go = screen.getByRole("button", { name: "Re-walk" });
    expect(go).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Commit"), "aaa111");
    await userEvent.click(go);
    await waitFor(() => expect(api.codeReindex).toHaveBeenCalledWith({ org: "acme", namespace: "code-x" }, "aaa111"));
    expect(onClose).toHaveBeenCalled();
  });
});
