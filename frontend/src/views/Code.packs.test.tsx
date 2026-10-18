import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
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

describe("PR packs rows", () => {
  // A row list is one tab stop (lib/useRowNav); the arrows move between packs.
  it("is one tab stop, moves by arrow, and draws each pack's time with ListTime", async () => {
    listCodeContextPacks.mockResolvedValue({ rows: [pack(3), pack(2), pack(1)], total: 3 });
    render(<PacksTab id={ID} repo="acme/app" />);
    const first = (await screen.findByText("PR 3")).closest("button") as HTMLElement;
    const opens = [...document.querySelectorAll<HTMLElement>("#packs-list [data-row-open]")];
    expect(opens.map((b) => b.tabIndex)).toEqual([0, -1, -1]);
    expect(first.querySelector("time")).toHaveAttribute("dateTime", "2026-09-20T07:00:00.000Z");
    first.focus();
    await userEvent.keyboard("{ArrowDown}");
    expect(opens[1]).toHaveFocus();
  });
});

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

  it("steps back to the last page when the next page's read finds its packs gone", async () => {
    let total = 60;
    listCodeContextPacks.mockImplementation(async (_id, opts: { offset: number; limit: number }) => ({
      rows: Array.from({ length: Math.max(0, Math.min(opts.limit, total - opts.offset)) }, (_, i) =>
        pack(1000 - opts.offset - i),
      ),
      total,
    }));
    render(<PacksTab id={ID} repo="acme/app" />);
    await screen.findByText("PR 1000");
    total = 45;
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() => expect(listCodeContextPacks.mock.calls.at(-1)?.[1]).toMatchObject({ offset: 0 }));
    expect(await screen.findByText("PR 1000")).toBeInTheDocument();
    expect(screen.queryByText("No packs yet")).toBeNull();
    expect(document.getElementById("packs-list")).toHaveFocus();
    expect(within(screen.getByRole("navigation", { name: "PR pack pages" })).getByRole("status")).toHaveTextContent(
      "Page 1 of 1",
    );
  });

  it("starts again from the first page when the page size changes on a later page", async () => {
    listCodeContextPacks.mockImplementation(async (_id, opts: { offset: number; limit: number }) => ({
      rows: Array.from({ length: Math.max(0, Math.min(opts.limit, 120 - opts.offset)) }, (_, i) =>
        pack(1000 - opts.offset - i),
      ),
      total: 120,
    }));
    render(<PacksTab id={ID} repo="acme/app" />);
    await screen.findByText("PR 1000");
    const pager = () => screen.getByRole("navigation", { name: "PR pack pages" });
    fireEvent.click(within(pager()).getByRole("button", { name: "Next page" }));
    await screen.findByText("PR 950");
    fireEvent.click(within(pager()).getByRole("button", { name: "Next page" }));
    await screen.findByText("PR 900");
    await userEvent.click(within(pager()).getByRole("button", { name: "Rows per page" }));
    await userEvent.click(await screen.findByText(/^100/));
    await waitFor(() => expect(listCodeContextPacks.mock.calls.at(-1)?.[1]).toMatchObject({ limit: 100, offset: 0 }));
    expect(await screen.findByText("PR 1000")).toBeInTheDocument();
    expect(screen.queryByText("PR 900")).toBeNull();
  });

  it("keeps focus on the list when a page steps back to an empty first page", async () => {
    let total = 60;
    listCodeContextPacks.mockImplementation(async (_id, opts: { offset: number; limit: number }) => ({
      rows: Array.from({ length: Math.max(0, Math.min(opts.limit, total - opts.offset)) }, (_, i) =>
        pack(1000 - opts.offset - i),
      ),
      total,
    }));
    render(<PacksTab id={ID} repo="acme/app" />);
    await screen.findByText("PR 1000");
    total = 0;
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(await screen.findByText("No packs yet")).toBeInTheDocument();
    expect(document.getElementById("packs-list")).toHaveFocus();
  });

  it("says nothing of a PR number until it has settled and been read", async () => {
    let answer: (v: unknown) => void = () => {};
    listCodeContextPacks.mockImplementation(async (_id, opts: { pr_number: number }) =>
      opts.pr_number === 123 ? new Promise((r) => (answer = r)) : { rows: [], total: 0 },
    );
    render(<PacksTab id={ID} repo="acme/app" />);
    expect(await screen.findByText("Packs appear as merges are ingested.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Filter by PR number"), { target: { value: "123" } });
    expect(screen.queryByText(/No precomputed pack for PR #123/)).toBeNull();
    await waitFor(() =>
      expect(listCodeContextPacks.mock.calls.at(-1)?.[1]).toMatchObject({ pr_number: 123, offset: 0 }),
    );
    // Read, not yet answered: the empty state says nothing of either filter.
    expect(screen.queryByText("No packs yet")).toBeNull();
    await act(async () => answer({ rows: [], total: 0 }));
    expect(await screen.findByText(/No precomputed pack for PR #123/)).toBeInTheDocument();
  });

  it("says a failed read failed, keeps the filter, and reads again on Retry", async () => {
    listCodeContextPacks.mockRejectedValueOnce(new Error("boom"));
    listCodeContextPacks.mockResolvedValue({ rows: [pack(7)], total: 1 });
    render(<PacksTab id={ID} repo="acme/app" />);
    expect(await screen.findByText("Couldn't load PR packs")).toBeInTheDocument();
    expect(screen.getByLabelText("Filter by PR number")).toBeInTheDocument();
    let answer: (v: unknown) => void = () => {};
    listCodeContextPacks.mockReturnValueOnce(new Promise((r) => (answer = r)));
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    // The read is in flight: the error gives way to it.
    await waitFor(() => expect(screen.queryByText("Couldn't load PR packs")).toBeNull());
    await act(async () => answer({ rows: [pack(7)], total: 1 }));
    expect(await screen.findByText("PR 7")).toBeInTheDocument();
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
    // RepoDetail keys the tab by branch, as here.
    const { rerender } = render(<PacksTab key={ID.namespace} id={ID} repo="acme/app" />);
    await screen.findByText("PR 1000");
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(await screen.findByText("PR 950")).toBeInTheDocument();

    const other = { org: "dev", namespace: "code-acme-release" };
    rerender(<PacksTab key={other.namespace} id={other} repo="acme/app" />);
    expect(await screen.findByText("PR 1000")).toBeInTheDocument();
    const reads = listCodeContextPacks.mock.calls.filter(([id]) => id.namespace === other.namespace);
    expect(reads.map(([, opts]) => opts.offset)).toEqual([0]);
  });
});
