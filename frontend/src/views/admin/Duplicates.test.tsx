import { cleanup, render, screen, waitFor } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../api";
import { confirmAction } from "../../components/ConfirmDialog";
import { useStore } from "../../lib/state";
import { DuplicatesPanel } from "./Duplicates";

vi.mock("../../components/ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));
vi.mock("../../components/Toast", () => ({ toast: vi.fn(), toastError: vi.fn(), ToastViewport: () => null }));
vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    listMemoryDuplicates: vi.fn(async () => ({
      clusters: [
        {
          canonical_id: "c1",
          canonical_key: "home_city",
          canonical_agent: "claude-code",
          dup_count: 1,
          duplicates: [{ memory_id: "d1", memory_key: "user_city", agent: "cursor", created_at: null }],
        },
      ],
    })),
    listDocumentDuplicates: vi.fn(async () => ({
      doc_clusters: [
        {
          content_sha256: "h",
          doc_count: 2,
          docs: [
            { doc_id: "1", source_uri: "a.md", title: "Runbook", ingested_at: "" },
            { doc_id: "2", source_uri: "b.md", title: "", ingested_at: "" },
          ],
        },
      ],
    })),
    listWritingAgents: vi.fn(async () => ["claude-code", "-"]),
    resolveMemoryDuplicate: vi.fn(async () => ({})),
    retireDocumentDuplicate: vi.fn(async () => ({})),
    collapseMemoryDuplicates: vi.fn(async (_id: unknown, dry: boolean) => ({
      pairs: 3,
      restamped: dry ? 0 : 3,
      truncated: false,
      sample: [],
    })),
    repairOrphanDuplicates: vi.fn(async () => ({ candidates: 0, reactivated: 0 })),
    consolidateMemories: vi.fn(async () => ({ consolidated: [], examined: 10, clusters_found: 2 })),
  };
});

const scope = { org: "acme", namespace: "team" };

beforeEach(() => useStore.setState({ identity: { ...scope, agent: "loom-ui" } }));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("DuplicatesPanel", () => {
  it("settles a memory pair with the canonical named again", async () => {
    render(<DuplicatesPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Keep both: user_city" }));
    await waitFor(() =>
      expect(api.resolveMemoryDuplicate).toHaveBeenCalledWith(
        scope,
        expect.objectContaining({ canonical_key: "home_city" }),
        "d1",
        "keep_both",
      ),
    );
  });

  it("deletes a memory of the pair only after a confirmation that names it", async () => {
    vi.mocked(confirmAction).mockResolvedValueOnce(false);
    render(<DuplicatesPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Keep this one: user_city" }));
    expect(confirmAction).toHaveBeenCalledWith(expect.objectContaining({ title: "Delete home_city?" }));
    expect(api.resolveMemoryDuplicate).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Drop this one: user_city" }));
    expect(confirmAction).toHaveBeenLastCalledWith(expect.objectContaining({ title: "Delete user_city?" }));
    await waitFor(() =>
      expect(api.resolveMemoryDuplicate).toHaveBeenCalledWith(scope, expect.anything(), "d1", "keep_canonical"),
    );
  });

  it("retires a document copy in favour of the first, after a confirmation", async () => {
    render(<DuplicatesPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Retire copy: b.md" }));
    expect(confirmAction).toHaveBeenCalledWith(expect.objectContaining({ title: "Retire b.md?" }));
    await waitFor(() => expect(api.retireDocumentDuplicate).toHaveBeenCalledWith(scope, "a.md", "b.md"));
  });

  it("collapses every pair only after a dry run and a confirmation", async () => {
    render(<DuplicatesPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Collapse all pairs" }));
    await waitFor(() => expect(api.collapseMemoryDuplicates).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.collapseMemoryDuplicates).mock.calls.map((c) => c[1])).toEqual([true, false]);
    expect(confirmAction).toHaveBeenCalledWith(
      expect.objectContaining({ title: expect.stringContaining("3 duplicate pairs") }),
    );
  });

  it("asks nothing when the dry run finds nothing to repair", async () => {
    render(<DuplicatesPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Repair orphaned duplicates" }));
    await waitFor(() => expect(api.repairOrphanDuplicates).toHaveBeenCalledTimes(1));
    expect(confirmAction).not.toHaveBeenCalled();
  });

  it("merges one agent's near-duplicates once an agent is picked", async () => {
    render(<DuplicatesPanel />);
    const merge = await screen.findByRole("button", { name: "Merge near-duplicates" });
    expect(merge).toBeDisabled();
    await userEvent.click(screen.getByLabelText("Agent"));
    // Click UI's Select items carry no option role.
    const item = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('[cui-select-item][data-value="claude-code"]');
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });
    expect(document.querySelector('[cui-select-item][data-value="-"]')).toBeNull();
    await userEvent.click(item);
    await userEvent.click(merge);
    await waitFor(() => expect(api.consolidateMemories).toHaveBeenCalledWith(scope, "claude-code", false));
    expect(api.consolidateMemories).toHaveBeenCalledWith(scope, "claude-code", true);
  });
});
