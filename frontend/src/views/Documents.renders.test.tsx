import { act, cleanup, fireEvent, render, screen } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { KnowledgeEntry } from "../api";
import { useStore } from "../lib/state";
import { Documents } from "./Documents";

// The rows are memoised and filter by the text a render behind the box: a keystroke that
// leaves the rows as they were renders none of them again. Each row renders its upload
// time, so ListTime counts the rows rendered.

const renders = { count: 0 };
vi.mock("../components/ui/ListTime", () => ({
  ListTime: ({ iso }: { iso: string }) => {
    renders.count += 1;
    return <time>{iso}</time>;
  },
}));

const doc = (i: number): KnowledgeEntry => ({
  source_uri: `docs/handbook-${i}.md`,
  title: `Handbook ${i}`,
  original_filename: `handbook-${i}.md`,
  description: "",
  tags: ["ops", "runbook"],
  ingested_at: "2026-09-19T00:00:00Z",
  chunk_count: 3,
  has_blob: false,
});
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  listKnowledge: vi.fn(async () => ({ rows: [doc(1), doc(2), doc(3)], next_cursor: null })),
}));

describe("Library row renders", () => {
  beforeEach(() => {
    renders.count = 0;
    useStore.setState({ authed: true, identity: { org: "dev", namespace: "ns", agent: "loom-ui" }, view: "knowledge" });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ identity: null });
  });

  it("a keystroke whose search every row still matches renders no row again", async () => {
    render(<Documents />);
    await screen.findByText("Handbook 3");
    renders.count = 0;
    await act(async () => {
      fireEvent.change(screen.getByLabelText("Search library"), { target: { value: "handbook" } });
    });
    expect(screen.getByText("Handbook 3")).toBeInTheDocument();
    expect(renders.count).toBe(0);
  });
});
