/** The guide dialog reads a namespace's guide, edits it from the revision it read, and on a
 *  save someone else got in first shows their text and saves again on top of it. */
import { describe, it, expect, vi, beforeEach } from "vitest";
import userEvent from "@testing-library/user-event";
import { render, screen, fireEvent, waitFor } from "../test/rtl";
import { GuideDialog } from "./GuideDialog";
import * as api from "../api";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, getNamespaceGuide: vi.fn(), saveNamespaceGuide: vi.fn(), guideHistory: vi.fn() };
});

const GUIDE: api.NamespaceGuide = {
  found: true,
  revision: 3,
  content: "Keys: acct/<id>/plan",
  summary: "Per-account plans",
  note: "",
  updated_by: "process:sa:billing-bot",
  updated_at: "2026-10-21T10:00:00Z",
};

const open = (canEdit: boolean, onSaved = () => {}) =>
  render(<GuideDialog open onOpenChange={() => {}} org="o" namespace="billing" canEdit={canEdit} onSaved={onSaved} />);

beforeEach(() => {
  vi.mocked(api.getNamespaceGuide).mockReset().mockResolvedValue(GUIDE);
  vi.mocked(api.saveNamespaceGuide).mockReset();
  vi.mocked(api.guideHistory).mockReset().mockResolvedValue([]);
});

describe("GuideDialog", () => {
  it("shows the guide with its revision and writer, and no Edit to a reader", async () => {
    open(false);
    expect(await screen.findByText("Keys: acct/<id>/plan")).toBeInTheDocument();
    expect(screen.getByText(/Revision 3 by process:sa:billing-bot/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
  });

  it("offers a writer to write the first guide", async () => {
    vi.mocked(api.getNamespaceGuide).mockResolvedValue({ ...GUIDE, found: false, revision: 0, content: "" });
    open(true);
    expect(await screen.findByRole("button", { name: "Write the guide" })).toBeInTheDocument();
  });

  it("saves from the revision it read", async () => {
    vi.mocked(api.saveNamespaceGuide).mockResolvedValue({ written: true, conflict: false, revision: 4, current: null });
    const onSaved = vi.fn();
    open(true, onSaved);
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    await userEvent.type(screen.getByLabelText("Guide (Markdown)"), "{Enter}Tag stale");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    // The summary was not edited, so it is not sent: the server keeps the current one.
    expect(api.saveNamespaceGuide).toHaveBeenCalledWith("o", "billing", {
      content: "Keys: acct/<id>/plan\nTag stale",
      note: "",
      expected_revision: 3,
    });
  });

  it("on a conflict keeps the draft, shows the newer text and saves on top of it", async () => {
    const newer = {
      ...GUIDE,
      revision: 4,
      content: "Keys: acct/<id>/plan\nTag tier:N",
      summary: "Plans and tiers",
      updated_by: "human:bob",
    };
    vi.mocked(api.saveNamespaceGuide)
      .mockResolvedValueOnce({ written: false, conflict: true, revision: 4, current: newer })
      .mockResolvedValueOnce({ written: true, conflict: false, revision: 5, current: null });
    const onSaved = vi.fn();
    open(true, onSaved);
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    await userEvent.clear(screen.getByLabelText("Guide (Markdown)"));
    await userEvent.type(screen.getByLabelText("Guide (Markdown)"), "mine");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/Revision 4 by human:bob.* was saved while you edited/)).toBeInTheDocument();
    expect(screen.getByText(/Tag tier:N/)).toBeInTheDocument();
    expect(screen.getByText("Their summary: Plans and tiers")).toBeInTheDocument();
    expect(screen.getByLabelText("Guide (Markdown)")).toHaveValue("mine");
    expect(onSaved).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    const second = vi.mocked(api.saveNamespaceGuide).mock.calls[1]?.[2];
    expect(second?.expected_revision).toBe(4);
    // The untouched summary field took theirs, and is not sent back over it.
    expect(screen.getByLabelText(/Summary/)).toHaveValue("Plans and tiers");
    expect(second).not.toHaveProperty("summary");
  });

  it("sends an edited summary, and keeps it over a newer one after a conflict", async () => {
    const newer = { ...GUIDE, revision: 4, summary: "Plans and tiers", updated_by: "human:bob" };
    vi.mocked(api.saveNamespaceGuide)
      .mockResolvedValueOnce({ written: false, conflict: true, revision: 4, current: newer })
      .mockResolvedValueOnce({ written: true, conflict: false, revision: 5, current: null });
    const onSaved = vi.fn();
    open(true, onSaved);
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    await userEvent.clear(screen.getByLabelText(/Summary/));
    await userEvent.type(screen.getByLabelText(/Summary/), "Mine");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/was saved while you edited/)).toBeInTheDocument();
    expect(vi.mocked(api.saveNamespaceGuide).mock.calls[0]?.[2]).toMatchObject({ summary: "Mine" });
    expect(screen.getByLabelText(/Summary/)).toHaveValue("Mine");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(vi.mocked(api.saveNamespaceGuide).mock.calls[1]?.[2]).toMatchObject({
      summary: "Mine",
      expected_revision: 4,
    });
  });

  it("lists the earlier revisions under History, without the one shown", async () => {
    const revision = (n: number, note = "") => ({
      revision: n,
      summary: "Plans",
      note,
      updated_by: "a@x",
      updated_at: "",
    });
    vi.mocked(api.guideHistory).mockResolvedValueOnce([revision(3), revision(2, "split the tiers"), revision(1)]);
    open(false);
    await userEvent.click(await screen.findByRole("button", { name: "History" }));
    expect(await screen.findByText("Revision 2 by a@x")).toBeInTheDocument();
    expect(screen.getByText("— split the tiers")).toBeInTheDocument();
    expect(screen.getByText("Revision 1 by a@x")).toBeInTheDocument();
    expect(screen.queryByText("Revision 3 by a@x")).toBeNull();
    // The summary says what the namespace holds, not why a revision changed.
    expect(screen.queryByText(/— Plans/)).toBeNull();
  });

  it("offers Retry when the history fails to read", async () => {
    vi.mocked(api.guideHistory).mockRejectedValueOnce(new Error("down")).mockResolvedValueOnce([]);
    open(false);
    await userEvent.click(await screen.findByRole("button", { name: "History" }));
    await userEvent.click(await screen.findByRole("button", { name: "Retry" }));
    await waitFor(() => expect(api.guideHistory).toHaveBeenCalledTimes(2));
  });

  it("offers no History on a guide's first revision", async () => {
    vi.mocked(api.getNamespaceGuide).mockResolvedValue({ ...GUIDE, revision: 1 });
    open(false);
    expect(await screen.findByText(/Revision 1/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "History" })).toBeNull();
  });
});
