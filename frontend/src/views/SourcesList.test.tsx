import { act, cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../lib/errors";
import type { KnowledgeSourceList } from "../lib/knowledgeSources";
import { SourcesList } from "./SourcesList";

// A failed read of the document sources shows the error, its request id and a Retry that
// reads them again; a reader who cannot manage the sources gets no section at all.

const sources = vi.hoisted(() => ({ listKnowledgeSources: vi.fn() }));
vi.mock("../api/knowledgeSources", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/knowledgeSources")>()),
  ...sources,
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
});
