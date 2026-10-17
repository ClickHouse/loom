import { cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CodeContextPackRow } from "../api";
import { PacksTab } from "./Code";

// The PR packs list pages with Click UI's Pagination bar: code.context_packs counts the
// packs and reads them by offset, so the bar has a total and a page field.

const listCodeContextPacks = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, listCodeContextPacks: (...a: unknown[]) => listCodeContextPacks(...a) };
});

const pack = (pr: number): CodeContextPackRow => ({
  pr_number: pr,
  merge_commit_sha: "abcdef1234",
  pack_tokens: 1200,
  created_at: "2026-09-20 07:00:00",
  title: `PR ${pr}`,
  author: "dev",
  merged_at: "2026-09-20 07:00:00",
});

const ID = { org: "dev", namespace: "code-acme" };

beforeEach(() => {
  listCodeContextPacks.mockReset();
  localStorage.clear();
});
afterEach(cleanup);

describe("PR packs pages", () => {
  it("reads by offset behind Click UI's Pagination bar and brings the list into focus", async () => {
    listCodeContextPacks.mockImplementation(async (_id, opts: { offset: number; limit: number }) => ({
      rows: Array.from({ length: Math.min(opts.limit, 120 - opts.offset) }, (_, i) => pack(1000 - opts.offset - i)),
      total: 120,
    }));
    render(<PacksTab id={ID} repo="acme/app" />);
    await screen.findByText("PR 1000");
    expect(listCodeContextPacks.mock.calls[0][1]).toMatchObject({ limit: 50, offset: 0 });
    const nav = screen.getByRole("navigation", { name: "PR pack pages" });
    expect(nav).toHaveTextContent("1–50 of 120");
    expect(screen.queryByRole("button", { name: "Older" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(await screen.findByText("PR 950")).toBeInTheDocument();
    expect(listCodeContextPacks.mock.calls.at(-1)?.[1]).toMatchObject({ limit: 50, offset: 50 });
    await waitFor(() => expect(nav).toHaveTextContent("51–100 of 120"));
    expect(document.getElementById("packs-list")).toHaveFocus();
  });

  it("draws no pager when one page holds every pack", async () => {
    listCodeContextPacks.mockResolvedValue({ rows: [pack(7)], total: 1 });
    render(<PacksTab id={ID} repo="acme/app" />);
    await screen.findByText("PR 7");
    expect(screen.queryByRole("navigation", { name: "PR pack pages" })).toBeNull();
  });

  it("starts another branch at its first page", async () => {
    listCodeContextPacks.mockImplementation(async (_id, opts: { offset: number; limit: number }) => ({
      rows: Array.from({ length: Math.min(opts.limit, 120 - opts.offset) }, (_, i) => pack(1000 - opts.offset - i)),
      total: 120,
    }));
    const { rerender } = render(<PacksTab id={ID} repo="acme/app" />);
    await screen.findByText("PR 1000");
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(await screen.findByText("PR 950")).toBeInTheDocument();

    const other = { org: "dev", namespace: "code-acme-release" };
    rerender(<PacksTab id={other} repo="acme/app" />);
    expect(await screen.findByText("PR 1000")).toBeInTheDocument();
    const reads = listCodeContextPacks.mock.calls.filter(([id]) => id.namespace === other.namespace);
    expect(reads.map(([, opts]) => opts.offset)).toEqual([0]);
  });
});
