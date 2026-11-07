import { cleanup, render, screen, waitFor } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../api";
import { confirmAction } from "../../components/ConfirmDialog";
import { toast } from "../../components/Toast";
import { useStore } from "../../lib/state";
import { DuplicatesPanel, keptAndCopies } from "./Duplicates";

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
          duplicates: [{ memory_id: "d1", memory_key: "user_city", agent: "cursor" }],
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
    listMemoryAgents: vi.fn(async () => ["claude-code", "-"]),
    resolveMemoryDuplicate: vi.fn(async () => ({})),
    retireDocumentDuplicate: vi.fn(async () => ({})),
    collapseMemoryDuplicates: vi.fn(async (_id: unknown, dry: boolean) => ({
      mirror_stale: 2,
      restamped: dry ? 0 : 2,
      truncated: false,
    })),
    repairOrphanDuplicates: vi.fn(async () => ({ candidates: 0, reactivated: 0 })),
    consolidateMemories: vi.fn(async () => ({
      consolidated: [{ canonical_key: "k", canonical_value: "v", merged_keys: ["a", "b"], cluster_size: 2 }],
      examined: 10,
      clusters_found: 2,
      clusters_failed: 1,
      clusters_refused_lossy: 1,
    })),
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
    await userEvent.click(await screen.findByRole("button", { name: "Actions for user_city" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Keep this one, delete home_city" }));
    expect(confirmAction).toHaveBeenCalledWith(expect.objectContaining({ title: "Delete home_city?" }));
    expect(api.resolveMemoryDuplicate).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Actions for user_city" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Keep home_city, delete this one" }));
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

  it("marks the stale search rows of duplicates only after a dry run and a confirmation", async () => {
    render(<DuplicatesPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Hide duplicates from search" }));
    await waitFor(() => expect(api.collapseMemoryDuplicates).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.collapseMemoryDuplicates).mock.calls.map((c) => c[1])).toEqual([true, false]);
    expect(confirmAction).toHaveBeenCalledWith(
      expect.objectContaining({ title: expect.stringContaining("Mark 2 search rows") }),
    );
    expect(toast).toHaveBeenCalledWith("Marked 2 search rows.", "ok");
  });

  it("asks nothing when search already skips the pairs, and says a cut read covers only the first pairs", async () => {
    vi.mocked(api.collapseMemoryDuplicates).mockResolvedValueOnce({
      mirror_stale: 0,
      restamped: 0,
      truncated: false,
    });
    render(<DuplicatesPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Hide duplicates from search" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Search already skips every duplicate.", "info"));
    expect(confirmAction).not.toHaveBeenCalled();

    vi.mocked(api.collapseMemoryDuplicates).mockResolvedValueOnce({ mirror_stale: 0, restamped: 0, truncated: true });
    await userEvent.click(screen.getByRole("button", { name: "Hide duplicates from search" }));
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith("No search row needs marking among the first 5,000 duplicate pairs.", "info"),
    );

    vi.mocked(api.collapseMemoryDuplicates)
      .mockResolvedValueOnce({ mirror_stale: 1, restamped: 0, truncated: true })
      .mockResolvedValueOnce({ mirror_stale: 1, restamped: 1, truncated: true });
    await userEvent.click(screen.getByRole("button", { name: "Hide duplicates from search" }));
    await waitFor(() =>
      expect(confirmAction).toHaveBeenCalledWith(
        expect.objectContaining({ title: expect.stringContaining("among the first 5,000 duplicate pairs") }),
      ),
    );
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Marked 1 search row.", "ok"));
  });

  it("offers no decision on a cluster whose canonical is gone, and warns of the siblings a keep orphans", async () => {
    vi.mocked(api.listMemoryDuplicates).mockResolvedValueOnce({
      clusters: [
        {
          canonical_id: "gone",
          canonical_key: "",
          canonical_agent: "",
          dup_count: 1,
          duplicates: [{ memory_id: "o1", memory_key: "old_city", agent: "a" }],
        },
        {
          canonical_id: "c2",
          canonical_key: "home",
          canonical_agent: "a",
          dup_count: 2,
          duplicates: [
            { memory_id: "x1", memory_key: "home_1", agent: "a" },
            { memory_id: "x2", memory_key: "home_2", agent: "a" },
          ],
        },
      ],
    });
    vi.mocked(confirmAction).mockResolvedValueOnce(false);
    render(<DuplicatesPanel />);
    expect(await screen.findByText(/1 duplicate of a deleted memory/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Keep both: old_city" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Actions for home_1" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Keep this one, delete home" }));
    expect(confirmAction).toHaveBeenCalledWith(
      expect.objectContaining({ message: expect.stringContaining("The other duplicate of it loses its canonical") }),
    );
  });

  it("keeps the oldest document and lists each other address once", () => {
    const d = (doc_id: string, source_uri: string, ingested_at: string) => ({
      doc_id,
      source_uri,
      title: "",
      ingested_at,
    });
    const { keep, copies } = keptAndCopies([
      d("3", "c.md", "2026-03"),
      d("1", "a.md", "2026-01"),
      d("2", "a.md", "2026-02"),
      d("4", "c.md", "2026-04"),
    ]);
    expect(keep.doc_id).toBe("1");
    expect(copies.map((c) => c.doc_id)).toEqual(["3"]);
  });

  it("says when the agents could not be read", async () => {
    vi.mocked(api.listMemoryAgents).mockRejectedValueOnce(new Error("boom"));
    render(<DuplicatesPanel />);
    expect(await screen.findByText("Couldn't load the agents")).toBeInTheDocument();
  });

  it("holds a decided pair's buttons until the list is read again", async () => {
    render(<DuplicatesPanel />);
    const keep = await screen.findByRole("button", { name: "Keep both: user_city" });
    let answer: (v: { clusters: never[] }) => void = () => {};
    vi.mocked(api.listMemoryDuplicates).mockImplementationOnce(() => new Promise((r) => (answer = r)));
    await userEvent.click(keep);
    await waitFor(() => expect(api.resolveMemoryDuplicate).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: "Keep both: user_city" })).toBeDisabled();
    answer({ clusters: [] });
    expect(await screen.findByText("No pair waits for a decision.")).toBeInTheDocument();
  });

  it("shows the memory pairs when the document read fails", async () => {
    vi.mocked(api.listDocumentDuplicates).mockRejectedValueOnce(new Error("boom"));
    render(<DuplicatesPanel />);
    expect(await screen.findByRole("button", { name: "Keep both: user_city" })).toBeInTheDocument();
    expect(await screen.findByText("Couldn't load duplicate documents")).toBeInTheDocument();
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
    // One call, after the confirmation: a dry run would pay for the same model calls.
    await waitFor(() => expect(api.consolidateMemories).toHaveBeenCalledWith(scope, "claude-code"));
    expect(api.consolidateMemories).toHaveBeenCalledTimes(1);
    expect(toast).toHaveBeenCalledWith(
      "Merged 1 of 2 clusters; 1 left as it was (1 because the merge lost a detail).",
      "ok",
    );
    // It deletes the merged memories, so it asks as a deletion does.
    expect(confirmAction).toHaveBeenCalledWith(expect.objectContaining({ danger: true, confirmLabel: "Merge" }));
  });
});
