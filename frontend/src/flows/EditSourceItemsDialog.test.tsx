import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import { axeViolations, unlabeledControls } from "../test/axe";
import { listKnowledgeSources, parseKnowledgeSourceLinks, updateKnowledgeSource } from "../api/knowledgeSources";
import type { KnowledgeSource } from "../lib/knowledgeSources";
import { useStore } from "../lib/state";
import { EditSourceItemsDialog } from "./EditSourceItemsDialog";

vi.mock("../api/knowledgeSources", async (load) => ({
  ...(await load<typeof import("../api/knowledgeSources")>()),
  listKnowledgeSources: vi.fn(),
  parseKnowledgeSourceLinks: vi.fn(),
  updateKnowledgeSource: vi.fn(),
}));

const NS = { org: "acme", namespace: "col-team" };
const LINK = "https://drive.google.com/file/d/new-file/view";
const ITEM = { type: "folder", id: "existing-folder", recursive: true };
const SOURCE: KnowledgeSource = {
  name: "notes",
  kind: "gdrive",
  credential_mode: "connected",
  owned_by_caller: true,
  owner_subject: "canonical-reader",
  access: "mirrored",
  items: [ITEM, { type: "file", id: "keep-file" }],
  include: ["docs", "pdf"],
  config: {},
  created_by: "",
  created_at: "",
  sync: {
    requested_at: "",
    last_synced_at: "",
    status: "",
    documents_seen: 0,
    changed: 0,
    removed: 0,
    unconverted: 0,
    unconverted_reasons: {},
    skipped: 0,
    error: "",
  },
};
const PARSED = { links: [{ link: LINK, kind: "gdrive", item: { type: "file", id: "new-file" }, unreadable: "" }] };
const UPDATED = {
  name: "notes",
  items: [{ type: "file", id: "new-file" }],
  include: ["docs", "pdf"],
  config: {},
  added: [{ type: "file", id: "new-file" }],
  removed: [ITEM],
  probe: { documents: 0, more: false, sample: [] },
};

beforeEach(() => {
  vi.resetAllMocks();
  useStore.setState({
    authed: true,
    sessionExpired: false,
    identity: { ...NS, agent: "loom-ui" },
    whoami: { authenticated: true, subject: "reader-alias", org: "acme", roles: ["writer"] },
  });
  vi.mocked(parseKnowledgeSourceLinks).mockResolvedValue(PARSED);
  vi.mocked(updateKnowledgeSource).mockResolvedValue(UPDATED);
  vi.mocked(listKnowledgeSources).mockResolvedValue({ enabled: true, sources: [SOURCE] });
});
afterEach(() => {
  cleanup();
  useStore.setState({ whoami: null });
});

function open(source = SOURCE, canManage = true) {
  const onClose = vi.fn(),
    onUpdated = vi.fn();
  render(
    <EditSourceItemsDialog {...NS} source={source} canManage={canManage} onClose={onClose} onUpdated={onUpdated} />,
  );
  return { onClose, onUpdated };
}
function addLink(value = LINK) {
  fireEvent.input(screen.getByLabelText(/^Content links to add/), { target: { value } });
}
async function review() {
  await userEvent.click(screen.getByRole("button", { name: "Review changes" }));
}

describe("EditSourceItemsDialog", () => {
  it("saves only the reviewed incremental selection with labels and no credential or access override", async () => {
    const callbacks = open();
    await userEvent.click(screen.getByRole("checkbox", { name: "Folder existing-folder (with subfolders)" }));
    addLink();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    await review();
    expect(await screen.findByText(/1 links to add · 1 items to remove/)).toBeInTheDocument();
    expect(parseKnowledgeSourceLinks).toHaveBeenCalledWith(NS, [LINK], { signal: expect.any(AbortSignal) });
    expect(await axeViolations(document.body)).toEqual([]);
    expect(unlabeledControls(document.body)).toEqual([]);
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(updateKnowledgeSource).toHaveBeenCalledExactlyOnceWith(NS, "notes", {
      add_links: [LINK],
      remove_items: [ITEM],
    });
    expect(callbacks.onUpdated).toHaveBeenCalledOnce();
    expect(callbacks.onClose).toHaveBeenCalledOnce();
  });

  it("reviews removal-only edits without calling a parser with an empty selection", async () => {
    open();
    await userEvent.click(screen.getByRole("checkbox", { name: "File keep-file" }));
    await review();
    expect(parseKnowledgeSourceLinks).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(updateKnowledgeSource).toHaveBeenCalledExactlyOnceWith(NS, "notes", {
      remove_items: [{ type: "file", id: "keep-file" }],
    });
  });

  it.each([
    { kind: "notion", item: { type: "page", id: "other-page" }, unreadable: "" },
    { kind: "", item: {}, unreadable: "Unsupported content" },
  ])("keeps failed link previews visible and refuses $kind content: $unreadable", async (row) => {
    vi.mocked(parseKnowledgeSourceLinks).mockResolvedValue({
      links: [{ link: LINK, ...row }],
    });
    open();
    addLink();
    await review();
    await screen.findByRole("alert");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(within(screen.getByRole("region", { name: "Content link preview" })).getByText(LINK)).toBeInTheDocument();
    expect(updateKnowledgeSource).not.toHaveBeenCalled();
  });

  it("rejects a reordered or incomplete preview instead of silently changing the saved selection", async () => {
    vi.mocked(parseKnowledgeSourceLinks).mockResolvedValue({ links: [] });
    open();
    addLink();
    await review();
    expect(await screen.findByRole("alert")).toHaveTextContent(/did not match/);
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(updateKnowledgeSource).not.toHaveBeenCalled();
  });

  it("invalidates review when a link or removal selection changes", async () => {
    open();
    addLink();
    await review();
    await waitFor(() => expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled());
    await userEvent.click(screen.getByRole("checkbox", { name: "File keep-file" }));
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    await review();
    await waitFor(() => expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled());
    addLink("https://drive.google.com/file/d/different/view");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(updateKnowledgeSource).not.toHaveBeenCalled();
  });

  it("preserves input and selected removals after a save refusal without retrying it", async () => {
    vi.mocked(updateKnowledgeSource).mockRejectedValue(new Error("Provider refused the selected file"));
    const callbacks = open();
    addLink();
    await userEvent.click(screen.getByRole("checkbox", { name: "File keep-file" }));
    await review();
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText(/Save could not be confirmed/)).toBeInTheDocument();
    expect(screen.getByLabelText(/^Content links to add/)).toHaveValue(LINK);
    expect(screen.getByRole("checkbox", { name: "File keep-file" })).toBeChecked();
    expect(updateKnowledgeSource).toHaveBeenCalledTimes(1);
    expect(callbacks.onUpdated).not.toHaveBeenCalled();
    expect(callbacks.onClose).not.toHaveBeenCalled();
  });

  it("requires a fresh source read and a new review after a lost save response", async () => {
    vi.mocked(updateKnowledgeSource).mockRejectedValueOnce(new TypeError("Failed to fetch"));
    vi.mocked(listKnowledgeSources).mockResolvedValue({
      enabled: true,
      sources: [
        {
          ...SOURCE,
          items: [
            { type: "file", id: "keep-file" },
            { type: "file", id: "new-file" },
          ],
        },
      ],
    });
    open();
    addLink();
    await userEvent.click(screen.getByRole("checkbox", { name: "Folder existing-folder (with subfolders)" }));
    await review();
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await screen.findByText(/Save could not be confirmed/);
    expect(screen.queryByText(/These changes have not been saved/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Review changes" })).toBeDisabled();
    expect(updateKnowledgeSource).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "Reload current items" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Review changes" })).toBeEnabled());
    expect(listKnowledgeSources).toHaveBeenCalledWith(NS, { signal: expect.any(AbortSignal) });
    expect(
      screen.queryByRole("checkbox", { name: "Folder existing-folder (with subfolders)" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "File keep-file" })).not.toBeChecked();
    expect(screen.getByLabelText(/^Content links to add/)).toHaveValue(LINK);
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox", { name: "File keep-file" }));
    await review();
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(updateKnowledgeSource).toHaveBeenLastCalledWith(NS, "notes", {
      add_links: [LINK],
      remove_items: [{ type: "file", id: "keep-file" }],
    });
    expect(updateKnowledgeSource).toHaveBeenCalledTimes(2);
  });

  it("closes stale ownership and drops a late review after the signed-in identity changes", async () => {
    let finish!: (value: typeof PARSED) => void;
    vi.mocked(parseKnowledgeSourceLinks).mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    open();
    addLink();
    await review();
    await act(async () => {
      useStore.setState({ whoami: { authenticated: true, subject: "other-reader", org: "acme", roles: ["writer"] } });
    });
    await act(async () => {
      finish(PARSED);
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(updateKnowledgeSource).not.toHaveBeenCalled();
  });

  it("offers no edit when ownership or current manage permission is missing", () => {
    open({ ...SOURCE, owned_by_caller: false }, true);
    expect(screen.getByRole("alert")).toHaveTextContent(/no longer have access/);
    expect(screen.getByRole("button", { name: "Review changes" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
  });

  it("keeps the dialog open while an incremental save is pending", async () => {
    let reject!: (error: Error) => void;
    vi.mocked(updateKnowledgeSource).mockImplementationOnce(
      () =>
        new Promise((_, fail) => {
          reject = fail;
        }),
    );
    const callbacks = open();
    await userEvent.click(screen.getByRole("checkbox", { name: "File keep-file" }));
    await review();
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await userEvent.keyboard("{Escape}");
    const closesWhilePending = callbacks.onClose.mock.calls.length;
    await act(async () => {
      reject(new TypeError("Failed to fetch"));
    });
    await screen.findByText(/Save could not be confirmed/);
    expect(closesWhilePending).toBe(0);
    expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled();
    expect(updateKnowledgeSource).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(callbacks.onClose).toHaveBeenCalledOnce();
  });
});
