/**
 * Edit details: a save that committed with only lists and search behind closes the dialog and
 * reads the list again, as after a success; a failure keeps the dialog open.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  updateKnowledgeMetadata: vi.fn(),
}));

import { updateKnowledgeMetadata, type KnowledgeEntry } from "../api";
import { ToastViewport } from "../components/Toast";
import { ApiError } from "../lib/errors";
import { EditDocumentMetadataDialog } from "./EditDocumentMetadataDialog";

const ENTRY = {
  source_uri: "docs/refunds.md",
  title: "Refunds",
  description: "",
  tags: [],
} as unknown as KnowledgeEntry;

function mount() {
  const onSaved = vi.fn();
  const onOpenChange = vi.fn();
  render(
    <>
      <ToastViewport />
      <EditDocumentMetadataDialog
        open
        onOpenChange={onOpenChange}
        identity={{ org: "acme", namespace: "team" }}
        entry={ENTRY}
        onSaved={onSaved}
      />
    </>,
  );
  fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Refund escalation" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  return { onSaved, onOpenChange };
}

describe("EditDocumentMetadataDialog", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("closes and reads the list again on a save that committed with only search behind", async () => {
    vi.mocked(updateKnowledgeMetadata).mockRejectedValue(
      new ApiError({ status: 503, code: "PUBLICATION_PENDING", detail: { retryable: false } }),
    );
    const { onSaved, onOpenChange } = mount();
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    expect(onSaved).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/^Done, but lists and search have not caught up yet/)).toBeInTheDocument();
  });

  it("stays open on a failure", async () => {
    vi.mocked(updateKnowledgeMetadata).mockRejectedValue(new ApiError({ status: 500, message: "boom" }));
    const { onSaved, onOpenChange } = mount();
    // The failure is said: only then is it known that nothing closed.
    expect(await screen.findByText(/^Save failed: /)).toBeInTheDocument();
    expect(onSaved).not.toHaveBeenCalled();
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });
});
