import { cleanup, render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import type { CodeRepoSummary } from "../api";
import { must } from "../test/must";
import { CodeReindexDialog } from "./CodeReindexDialog";

vi.mock("../components/Toast", () => ({ toast: vi.fn(), toastError: vi.fn(), ToastViewport: () => null }));
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, codeRecentCommits: vi.fn(), codeReindex: vi.fn() };
});

const watermark = "c".repeat(40);
const branch = {
  org: "acme",
  namespace: "code-x",
  repo: "acme/x",
  branch: "main",
  git_head: watermark,
} as CodeRepoSummary;
const sha = "a".repeat(40);
const row = (commit: string) => ({
  commit,
  subject: "fix the parser",
  committed_at: "",
  ingested_at: "",
  files: 3,
  pr_number: 0,
});

beforeEach(() => {
  vi.mocked(api.codeRecentCommits).mockResolvedValue({ rows: [row(watermark), row(sha)] });
  vi.mocked(api.codeReindex).mockResolvedValue({ previous_head: "b".repeat(40), new_head: sha });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

async function pick() {
  await userEvent.click(await screen.findByRole("button", { name: /Commit/ }));
  await userEvent.click(must(document.querySelector<HTMLElement>(`[cui-select-item][data-value="${sha}"]`), "commit"));
}

describe("CodeReindexDialog", () => {
  it("re-indexes after a commit the branch indexed, then re-reads the card", async () => {
    const onClose = vi.fn();
    const onDone = vi.fn();
    render(<CodeReindexDialog branch={branch} onDone={onDone} onClose={onClose} />);
    expect(screen.getByRole("button", { name: "Re-index" })).toBeDisabled();
    await pick();
    await userEvent.click(screen.getByRole("button", { name: "Re-index" }));
    await waitFor(() => expect(api.codeReindex).toHaveBeenCalledWith({ org: "acme", namespace: "code-x" }, sha));
    expect(api.codeRecentCommits).toHaveBeenCalledWith({ org: "acme", namespace: "code-x" }, 100, expect.anything());
    expect(onDone).toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it("does not offer the watermark, where a move changes nothing", async () => {
    render(<CodeReindexDialog branch={branch} onDone={vi.fn()} onClose={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: /Commit/ }));
    expect(document.querySelector(`[cui-select-item][data-value="${sha}"]`)).not.toBeNull();
    expect(document.querySelector(`[cui-select-item][data-value="${watermark}"]`)).toBeNull();
  });

  it("keeps the dialog open with the reason when the server refuses", async () => {
    vi.mocked(api.codeReindex).mockRejectedValueOnce(new Error("not an admin"));
    const onClose = vi.fn();
    render(<CodeReindexDialog branch={branch} onDone={vi.fn()} onClose={onClose} />);
    await pick();
    await userEvent.click(screen.getByRole("button", { name: "Re-index" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("not an admin");
    expect(onClose).not.toHaveBeenCalled();
  });

  it("says so when the branch has indexed nothing before its last commit", async () => {
    vi.mocked(api.codeRecentCommits).mockResolvedValueOnce({ rows: [row(watermark)] });
    render(<CodeReindexDialog branch={branch} onDone={vi.fn()} onClose={vi.fn()} />);
    expect(await screen.findByText("The branch has indexed no commit before its last one.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Re-index" })).toBeDisabled();
  });
});
