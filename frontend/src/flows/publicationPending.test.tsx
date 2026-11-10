// A write that committed while only search lags is saved: each dialog closes as on success,
// so it never invites the second write its line warns against.
import { render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../lib/errors";
import { ToastViewport } from "../components/Toast";
import { AddDocumentDialog } from "./AddDocumentDialog";
import { AddMemoryDialog } from "./AddMemoryDialog";
import { EditDocumentMetadataDialog } from "./EditDocumentMetadataDialog";

const pending = () =>
  new ApiError({
    status: 503,
    code: "PUBLICATION_PENDING",
    message: "the write is committed; do not repeat it",
    detail: { category: "dependency", retryable: false, committed: true },
  });

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    setMemory: vi.fn(async () => {
      throw pending();
    }),
    addKnowledge: vi.fn(async () => {
      throw pending();
    }),
    updateKnowledgeMetadata: vi.fn(async () => {
      throw pending();
    }),
  };
});

const identity = { org: "dev", namespace: "team", agent: "dashboard" };

async function expectClosedAsDone(onOpenChange: ReturnType<typeof vi.fn>, onDone: ReturnType<typeof vi.fn>) {
  await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
  expect(onDone).toHaveBeenCalled();
  expect(await screen.findByText(/^Done, but lists and search have not caught up yet/)).toBeInTheDocument();
  expect(screen.queryByText(/failed/i)).toBeNull();
}

describe("a write whose search copy is still pending closes its dialog", () => {
  it("Add memory", async () => {
    const onOpenChange = vi.fn();
    const onAdded = vi.fn();
    render(
      <>
        <ToastViewport />
        <AddMemoryDialog open onOpenChange={onOpenChange} identity={identity} onAdded={onAdded} />
      </>,
    );
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: /^Key/ }), "user_pref");
    await user.type(screen.getByRole("textbox", { name: /^Value/ }), "dark mode");
    await user.click(screen.getByRole("button", { name: /^Add memory$/ }));
    await expectClosedAsDone(onOpenChange, onAdded);
  });

  it("Add document, pasted text", async () => {
    const onOpenChange = vi.fn();
    const onAdded = vi.fn();
    render(
      <>
        <ToastViewport />
        <AddDocumentDialog open onOpenChange={onOpenChange} identity={identity} onAdded={onAdded} />
      </>,
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("tab", { name: "Paste text" }));
    await user.type(await screen.findByRole("textbox", { name: /^Content/ }), "Runbook text");
    await user.type(screen.getByRole("textbox", { name: /^Source URI/ }), "docs/runbook.md");
    await user.click(screen.getByRole("button", { name: /^Add$/ }));
    await expectClosedAsDone(onOpenChange, onAdded);
  });

  it("Edit details", async () => {
    const onOpenChange = vi.fn();
    const onSaved = vi.fn();
    const entry = {
      source_uri: "docs/a.md",
      title: "A",
      tags: ["x"],
      ingested_at: "2026-01-01T00:00:00Z",
      chunk_count: 1,
      superseded: false,
    };
    render(
      <>
        <ToastViewport />
        <EditDocumentMetadataDialog
          open
          onOpenChange={onOpenChange}
          identity={identity}
          entry={entry}
          onSaved={onSaved}
        />
      </>,
    );
    await userEvent.setup().click(screen.getByRole("button", { name: /^Save$/ }));
    await expectClosedAsDone(onOpenChange, onSaved);
  });
});
