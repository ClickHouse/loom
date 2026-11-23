import { act, cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../lib/errors";
import type { KnowledgeSourceList } from "../lib/knowledgeSources";
import { inspectKnowledgeSource } from "../api/knowledgeSources";
import { SourcesList } from "./SourcesList";

// A failed read of the document sources shows the error, its request id and a Retry that
// reads them again; a reader who cannot manage the sources gets no section at all.

const sources = vi.hoisted(() => ({ listKnowledgeSources: vi.fn() }));
vi.mock("../api/knowledgeSources", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/knowledgeSources")>()),
  ...sources,
  inspectKnowledgeSource: vi.fn(),
}));

const LISTED: KnowledgeSourceList = {
  enabled: true,
  sources: [
    {
      name: "design-docs",
      kind: "gdrive",
      config: { folder_ids: ["1AbCdEfGhIjKlMnOp"] },
      created_by: "alice",
      created_at: "2026-01-01T00:00:00Z",
      sync: {
        requested_at: "",
        last_synced_at: "2026-01-01T00:10:00Z",
        status: "ok",
        documents_seen: 4,
        changed: 4,
        removed: 0,
        unconverted: 0,
        unconverted_reasons: {},
        skipped: 0,
        error: "",
      },
    },
  ],
};

const failure = () =>
  new ApiError({
    status: 503,
    code: "BACKEND_UNAVAILABLE",
    message: "the store did not answer",
    requestId: "req-7f3a",
  });

const place = { label: "acme/docs", noun: "repository" } as const;

describe("SourcesList", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("shows a failed read's error and request id, and Retry reads the sources again", async () => {
    sources.listKnowledgeSources.mockRejectedValueOnce(failure()).mockResolvedValueOnce(LISTED);
    render(<SourcesList org="acme" namespace="docs" place={place} manageable />);
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("Couldn't load document sources")).toBeInTheDocument();
    expect(alert).toHaveTextContent("the store did not answer");
    expect(within(alert).getByText("req-7f3a")).toBeInTheDocument();
    await userEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("button", { name: "design-docs" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(sources.listKnowledgeSources).toHaveBeenCalledTimes(2);
  });

  it("shows a reader no section when the read fails", async () => {
    sources.listKnowledgeSources.mockRejectedValue(failure());
    const panel = document.createElement("div");
    document.body.append(panel);
    render(
      <SourcesList org="acme" namespace="docs" place={place} manageable={false} section="Sources" panel={panel} />,
    );
    await waitFor(() => expect(sources.listKnowledgeSources).toHaveBeenCalled());
    await act(async () => {
      await sources.listKnowledgeSources.mock.results[0]?.value.catch(() => {});
    });
    expect(screen.queryByRole("button", { name: /^Sources/ })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    panel.remove();
  });

  it("shows an admin the section, whose panel holds the error and Retry", async () => {
    sources.listKnowledgeSources.mockRejectedValueOnce(failure()).mockResolvedValueOnce(LISTED);
    const panel = document.createElement("div");
    document.body.append(panel);
    render(<SourcesList org="acme" namespace="docs" place={place} manageable section="Sources" panel={panel} />);
    await userEvent.click(await screen.findByRole("button", { name: /^Sources/ }));
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("req-7f3a")).toBeInTheDocument();
    await userEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("button", { name: "design-docs" })).toBeInTheDocument();
    panel.remove();
  });
  it.each(["gdrive", "notion", "linear", "unknown-kind"])("keeps %s metadata unavailable without native runtime actions", async (kind) => {
    sources.listKnowledgeSources.mockResolvedValue({ ...LISTED, sources: LISTED.sources.map((source) => ({ ...source, kind })) });
    render(<SourcesList org="acme" namespace="docs" place={place} manageable />);
    expect(await screen.findByText(`Unsupported source (${kind})`)).toBeInTheDocument();
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    expect(screen.getByText(/Unavailable retained registration/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Actions for design-docs" })).toBeNull();
    expect(screen.queryByText("Synced")).toBeNull();
    expect(screen.getByRole("button", { name: "design-docs" })).toBeInTheDocument();
    expect(inspectKnowledgeSource).not.toHaveBeenCalled();
  });
  it("offers recording-only setup from the real entry point", async () => {
    sources.listKnowledgeSources.mockResolvedValue({ enabled: true, accepts_sources: true, sources: [] });
    render(<SourcesList org="acme" namespace="docs" place={place} manageable />);
    await userEvent.click(await screen.findByRole("button", { name: "Add recording source" }));
    expect(await screen.findByRole("dialog", { name: /Add a recording source/ })).toBeInTheDocument();
    expect(screen.getByLabelText(/^Meetings/)).toBeInTheDocument();
    expect(screen.getByLabelText(/^Read-only credential/)).toHaveAttribute("type", "password");
    expect(screen.queryByLabelText(/^Folders/)).toBeNull();
    expect(screen.queryByRole("radio", { name: "Pages" })).toBeNull();
  });
  it("preserves recording management actions", async () => {
    sources.listKnowledgeSources.mockResolvedValue({ ...LISTED, sources: LISTED.sources.map((source) => ({ ...source, kind: "recording", config: { provider: "zoom", ids: ["81234567890"] } })) });
    render(<SourcesList org="acme" namespace="docs" place={place} manageable />);
    await userEvent.click(await screen.findByRole("button", { name: "Actions for design-docs" }));
    for (const name of ["Check", "Sync now", "Rotate credential", "Retire"]) expect(screen.getByRole("menuitem", { name, exact: true })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Push notifications" })).toBeNull();
  });
});
