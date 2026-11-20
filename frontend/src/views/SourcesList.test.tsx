import { act, cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../lib/errors";
import type { KnowledgeSource, KnowledgeSourceList } from "../lib/knowledgeSources";
import { useStore } from "../lib/state";
import { SourcesList } from "./SourcesList";

// A failed read of the document sources shows the error, its request id and a Retry that
// reads them again; a reader who cannot manage the sources gets no section at all.

const sources = vi.hoisted(() => ({ listKnowledgeSources: vi.fn(), updateKnowledgeSource: vi.fn() }));
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

function connectedSource(name: string, owner?: string): KnowledgeSource {
  return {
    ...LISTED.sources[0]!,
    name,
    credential_mode: "connected",
    owner_subject: owner,
    owned_by_caller: owner === "alice",
  };
}

function writer() {
  useStore.setState({ whoami: { authenticated: true, subject: "alice", org: "acme", roles: ["writer"] } });
}

describe("SourcesList", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    useStore.setState({ whoami: null });
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

  it("lets a writer add a source through their own connected account", async () => {
    writer();
    sources.listKnowledgeSources.mockResolvedValue({ enabled: true, sources: [] });
    render(<SourcesList org="acme" namespace="docs" place={place} manageable={false} connectedManageable />);
    expect(await screen.findByRole("button", { name: "Add a source" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Refresh sources" })).toBeEnabled();
  });

  it("uses server-proven ownership when the signed-in subject is an alias", async () => {
    useStore.setState({ whoami: { authenticated: true, subject: "alice-alias", org: "acme", roles: ["writer"] } });
    sources.listKnowledgeSources.mockResolvedValue({ enabled: true, sources: [connectedSource("own-alias", "alice")] });
    render(<SourcesList org="acme" namespace="docs" place={place} manageable={false} connectedManageable />);
    await userEvent.click(await screen.findByRole("button", { name: "Actions for own-alias" }));
    expect(await screen.findByRole("menuitem", { name: "Manage connection" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Sync now" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Edit items" })).toBeInTheDocument();
  });

  it("discards the prior caller's ownership projection and reloads after a sign-in change", async () => {
    writer();
    sources.listKnowledgeSources.mockResolvedValueOnce({
      enabled: true,
      sources: [connectedSource("private-selection", "alice")],
    });
    let finish!: (value: KnowledgeSourceList) => void;
    sources.listKnowledgeSources.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    render(<SourcesList org="acme" namespace="docs" place={place} manageable={false} connectedManageable />);
    await screen.findByRole("button", { name: "Actions for private-selection" });
    await act(async () => {
      useStore.setState({ whoami: { authenticated: true, subject: "bob", org: "acme", roles: ["writer"] } });
    });
    await waitFor(() => expect(sources.listKnowledgeSources).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("button", { name: "private-selection" })).not.toBeInTheDocument();
    await act(async () => {
      finish({
        enabled: true,
        sources: [{ ...connectedSource("private-selection", "alice"), owned_by_caller: false }],
      });
    });
    await screen.findByRole("button", { name: "private-selection" });
    expect(screen.queryByRole("button", { name: "Actions for private-selection" })).not.toBeInTheDocument();
  });

  it("does not offer an admin mutation controls over another person's connected source", async () => {
    useStore.setState({ whoami: { authenticated: true, subject: "alice", org: "acme", roles: ["admin"] } });
    sources.listKnowledgeSources.mockResolvedValue({
      enabled: true,
      sources: [connectedSource("another-owner", "bob")],
    });
    render(<SourcesList org="acme" namespace="docs" place={place} manageable connectedManageable />);
    await screen.findByRole("button", { name: "another-owner" });
    expect(screen.queryByRole("button", { name: "Actions for another-owner" })).not.toBeInTheDocument();
  });

  it("gives a writer no mutation controls for another owner's connection or an admin's shared source", async () => {
    writer();
    sources.listKnowledgeSources.mockResolvedValue({
      enabled: true,
      sources: [connectedSource("another-owner", "bob"), { ...LISTED.sources[0]!, credential_mode: "shared" }],
    });
    render(<SourcesList org="acme" namespace="docs" place={place} manageable={false} connectedManageable />);
    expect(await screen.findByRole("button", { name: "another-owner" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "design-docs" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Actions for/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add a source" })).toBeEnabled();
  });

  it("keeps connected source actions unavailable while the caller and owner are unknown", async () => {
    useStore.setState({ whoami: null });
    sources.listKnowledgeSources.mockResolvedValue({ enabled: true, sources: [connectedSource("owner-unknown")] });
    render(<SourcesList org="acme" namespace="docs" place={place} manageable={false} connectedManageable />);
    expect(await screen.findByRole("button", { name: "owner-unknown" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Actions for owner-unknown" })).not.toBeInTheDocument();
  });

  it("lets the owner check, sync and retire a connection and recover consent without a credential form", async () => {
    writer();
    sources.listKnowledgeSources.mockResolvedValue({
      enabled: true,
      sources: [
        {
          ...connectedSource("own-pages", "alice"),
          kind: "notion",
          sync: { ...LISTED.sources[0]!.sync, status: "credential_invalid", error: "Consent expired" },
        },
      ],
    });
    render(<SourcesList org="acme" namespace="docs" place={place} manageable={false} connectedManageable />);
    await userEvent.click(await screen.findByRole("button", { name: "Actions for own-pages" }));
    const menu = await screen.findByRole("menu", { name: "Actions for own-pages" });
    expect(within(menu).getByRole("menuitem", { name: "Check" })).toBeInTheDocument();
    expect(within(menu).getByRole("menuitem", { name: "Sync now" })).toBeInTheDocument();
    expect(within(menu).getByRole("menuitem", { name: "Retire" })).toBeInTheDocument();
    expect(within(menu).queryByRole("menuitem", { name: "Rotate credential" })).not.toBeInTheDocument();
    expect(within(menu).queryByRole("menuitem", { name: "Push notifications" })).not.toBeInTheDocument();
    await userEvent.click(within(menu).getByRole("menuitem", { name: "Manage connection" }));
    expect(await screen.findByRole("link", { name: /Manage your connection/ })).toHaveAttribute(
      "href",
      "/settings/connected-accounts?kind=notion&from=source",
    );
    expect(screen.getByText("Return here after connecting, then check the source again.")).toBeInTheDocument();
    expect(screen.queryByLabelText(/password|integration token|service account key/i)).not.toBeInTheDocument();
  });

  it.each(["connected", "shared"] as const)("uses stored mirrored policy for %s source retirement", async (mode) => {
    writer();
    sources.listKnowledgeSources.mockResolvedValue({
      enabled: true,
      sources: [
        {
          ...connectedSource("own-pages", "alice"),
          credential_mode: mode,
          access: "mirrored",
        },
      ],
    });
    render(
      <SourcesList org="acme" namespace="docs" place={place} manageable={mode === "shared"} connectedManageable />,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Actions for own-pages" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Retire" }));
    expect(await screen.findByText(/Mirrored copies are always queued for retirement/)).toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("keeps the namespace retirement choice for a connected source", async () => {
    writer();
    sources.listKnowledgeSources.mockResolvedValue({
      enabled: true,
      sources: [
        {
          ...connectedSource("own-pages", "alice"),
          access: "namespace",
        },
      ],
    });
    render(<SourcesList org="acme" namespace="docs" place={place} manageable={false} connectedManageable />);
    await userEvent.click(await screen.findByRole("button", { name: "Actions for own-pages" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Retire" }));
    expect(await screen.findByRole("checkbox", { name: /Also retire the documents/ })).not.toBeChecked();
    expect(screen.queryByText(/Mirrored copies are always/)).not.toBeInTheDocument();
  });

  it("preserves an admin's shared-source credential rotation without offering another person's account", async () => {
    useStore.setState({ whoami: { authenticated: true, subject: "alice", org: "acme", roles: ["admin"] } });
    sources.listKnowledgeSources.mockResolvedValue(LISTED);
    render(<SourcesList org="acme" namespace="docs" place={place} manageable connectedManageable />);
    await userEvent.click(await screen.findByRole("button", { name: "Actions for design-docs" }));
    const menu = await screen.findByRole("menu", { name: "Actions for design-docs" });
    expect(within(menu).getByRole("menuitem", { name: "Rotate credential" })).toBeInTheDocument();
    expect(within(menu).getByRole("menuitem", { name: "Check" })).toBeInTheDocument();
    expect(within(menu).getByRole("menuitem", { name: "Sync now" })).toBeInTheDocument();
    expect(within(menu).getByRole("menuitem", { name: "Retire" })).toBeInTheDocument();
    expect(within(menu).queryByRole("menuitem", { name: "Manage connection" })).not.toBeInTheDocument();
  });

  it("shows an incomplete pass and pending request as reported, then refreshes the document counts", async () => {
    writer();
    const source = connectedSource("own-folder", "alice");
    sources.listKnowledgeSources
      .mockResolvedValueOnce({
        enabled: true,
        sources: [
          {
            ...source,
            sync: {
              ...source.sync,
              requested_at: "2026-01-01T00:11:00Z",
              documents_seen: 6,
              changed: 3,
              unconverted: 2,
              unconverted_reasons: { too_large: 2 },
              skipped: 1,
              error: "listing: more pages remain",
            },
          },
        ],
      })
      .mockResolvedValueOnce({
        enabled: true,
        sources: [{ ...source, sync: { ...source.sync, documents_seen: 8, changed: 8 } }],
      });
    render(<SourcesList org="acme" namespace="docs" place={place} manageable={false} connectedManageable />);
    expect(await screen.findByText("Incomplete")).toBeInTheDocument();
    expect(screen.getByText("6 documents listed · 3 written · 2 not converted · 1 skipped")).toBeInTheDocument();
    expect(screen.getByText(/sync requested/)).toBeInTheDocument();
    expect(screen.queryByText(/^Syncing$/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Refresh sources" }));
    expect(await screen.findByText("8 documents listed · 8 written")).toBeInTheDocument();
    expect(screen.queryByText("Incomplete")).not.toBeInTheDocument();
    expect(screen.queryByText(/sync requested/)).not.toBeInTheDocument();
    expect(sources.listKnowledgeSources).toHaveBeenCalledTimes(2);
  });

  it.each(["settled", "pending"])("reloads current items before reopening an unconfirmed %s edit", async (outcome) => {
    writer();
    const initial = {
      ...connectedSource("own-selection", "alice"),
      items: [
        { type: "file", id: "removed-file" },
        { type: "file", id: "remaining-file" },
      ],
    };
    let finish!: (value: KnowledgeSourceList) => void;
    sources.listKnowledgeSources
      .mockReset()
      .mockResolvedValue({ enabled: true, sources: [{ ...initial, items: [] }] })
      .mockResolvedValueOnce({ enabled: true, sources: [initial] });
    sources.listKnowledgeSources.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    let failSave!: (error: Error) => void;
    sources.updateKnowledgeSource.mockReset();
    if (outcome === "pending") {
      sources.updateKnowledgeSource.mockImplementationOnce(
        () =>
          new Promise((_, reject) => {
            failSave = reject;
          }),
      );
    } else {
      sources.updateKnowledgeSource.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    }
    sources.updateKnowledgeSource.mockResolvedValueOnce({});
    render(<SourcesList org="acme" namespace="docs" place={place} manageable={false} connectedManageable />);
    await userEvent.click(await screen.findByRole("button", { name: "Actions for own-selection" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Edit items" }));
    await userEvent.click(await screen.findByRole("checkbox", { name: "File removed-file" }));
    await userEvent.click(screen.getByRole("button", { name: "Review changes" }));
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    if (outcome === "pending") {
      await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
      await userEvent.keyboard("{Escape}");
      expect(screen.getByRole("dialog")).toBeInTheDocument();
      expect(sources.listKnowledgeSources).toHaveBeenCalledTimes(1);
      expect(sources.updateKnowledgeSource).toHaveBeenCalledTimes(1);
      await act(async () => {
        failSave(new TypeError("Failed to fetch"));
      });
    }
    await screen.findByText(/Save could not be confirmed/);
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(sources.listKnowledgeSources).toHaveBeenCalledTimes(2));
    const action = screen.getByRole("button", { name: "Actions for own-selection" });
    await waitFor(() => expect(screen.getByRole("region", { name: "Document sources" })).toHaveFocus());
    await userEvent.click(action);
    const edit = await screen.findByRole("menuitem", { name: "Edit items" });
    expect(edit).toHaveAttribute("aria-disabled", "true");
    await userEvent.click(edit);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(sources.updateKnowledgeSource).toHaveBeenCalledTimes(1);
    await userEvent.keyboard("{Escape}");
    await act(async () => {
      finish({ enabled: true, sources: [{ ...initial, items: [{ type: "file", id: "remaining-file" }] }] });
    });
    await userEvent.click(await screen.findByRole("button", { name: "Actions for own-selection" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Edit items" }));
    expect(await screen.findByRole("checkbox", { name: "File remaining-file" })).not.toBeChecked();
    expect(screen.queryByRole("checkbox", { name: "File removed-file" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("checkbox", { name: "File remaining-file" }));
    await userEvent.click(screen.getByRole("button", { name: "Review changes" }));
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(sources.updateKnowledgeSource).toHaveBeenNthCalledWith(
      2,
      { org: "acme", namespace: "docs" },
      "own-selection",
      { remove_items: [{ type: "file", id: "remaining-file" }] },
    );
  });
});
