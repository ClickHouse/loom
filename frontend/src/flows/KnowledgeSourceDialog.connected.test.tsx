import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import { act, cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import { axeViolations, unlabeledControls } from "../test/axe";
import { listConnections } from "../api/connections";
import { addKnowledgeSource } from "../api/knowledgeSources";
import { ApiError } from "../lib/errors";
import { sourceAddBody } from "../lib/knowledgeSources";
import { useStore } from "../lib/state";
import { AddSourceDialog } from "./KnowledgeSourceDialog";

vi.mock("../api/connections", async (load) => ({
  ...(await load<typeof import("../api/connections")>()),
  listConnections: vi.fn(),
}));
vi.mock("../api/knowledgeSources", async (load) => ({
  ...(await load<typeof import("../api/knowledgeSources")>()),
  addKnowledgeSource: vi.fn(),
}));

const LINK = "https://drive.google.com/file/d/selected-file/view";
const OTHER = "https://docs.google.com/document/d/selected-doc/edit";
const NS = { org: "acme", namespace: "col-team" };
const connections = (connected = true, available = true) => ({
  connections: [
    {
      kind: "gdrive" as const,
      connected,
      connect_available: available,
      account_email: connected ? "reader@example.com" : null,
      scopes: [],
      connect_url: "",
    },
    {
      kind: "notion" as const,
      connected: true,
      connect_available: true,
      account_email: null,
      scopes: [],
      connect_url: "",
    },
    {
      kind: "linear" as const,
      connected: true,
      connect_available: true,
      account_email: null,
      scopes: [],
      connect_url: "",
    },
  ],
});
const result = (dry_run = true) => ({
  name: "selected",
  kind: "gdrive",
  namespace: NS.namespace,
  config: {},
  probe: { documents: 0, more: false, sample: [], checked: 2, sampled: false },
  dry_run,
});

beforeEach(() => {
  vi.resetAllMocks();
  useStore.setState({
    authed: true,
    sessionExpired: false,
    identity: { ...NS, agent: "loom-ui" },
    whoami: { authenticated: true, subject: "reader", org: NS.org, roles: ["writer"] },
  });
  vi.mocked(listConnections).mockResolvedValue(connections());
  vi.mocked(addKnowledgeSource).mockImplementation(async (_id, _form, dry) => result(dry));
});
afterEach(() => {
  cleanup();
  useStore.setState({ whoami: null, identity: null, authed: false, sessionExpired: false });
});

function open(onAdded = vi.fn()) {
  render(
    <AddSourceDialog open onOpenChange={() => {}} {...NS} label="Team library" allowShared={false} onAdded={onAdded} />,
  );
  return onAdded;
}
async function fill() {
  await screen.findByText("Using reader@example.com.");
  fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: "selected" } });
  fireEvent.input(screen.getByLabelText(/^Content links/), { target: { value: `${LINK}\n${OTHER}` } });
  expect(screen.getByLabelText(/^Content links/)).toHaveValue(`${LINK}\n${OTHER}`);
  expect(screen.queryByRole("list", { name: "Before checking" })?.textContent ?? "").toBe("");
  await waitFor(() => expect(screen.getByRole("button", { name: "Check" })).toBeEnabled());
}
async function check() {
  await waitFor(() => expect(screen.getByRole("button", { name: "Check" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Check" }));
  await screen.findByText(/Access checked for 2 items/);
}

describe("connected account to document source", () => {
  it("checks the exact selection before Add and sends no credential or unselected config", async () => {
    const added = open();
    await fill();
    expect(screen.queryByLabelText(/Service account key/)).toBeNull();
    expect(screen.getByText("Team library")).toBeVisible();
    expect(screen.getByRole("button", { name: "Add source" })).toBeDisabled();
    await check();
    await userEvent.click(screen.getByRole("button", { name: "Add source" }));
    await waitFor(() => expect(added).toHaveBeenCalledWith("selected"));
    const bodies = vi
      .mocked(addKnowledgeSource)
      .mock.calls.map(([id, form, dry]) => sourceAddBody(id.org, id.namespace, form, dry));
    expect(bodies).toEqual(
      [true, false].map((dry_run) => ({
        ...NS,
        name: "selected",
        kind: "gdrive",
        links: [LINK, OTHER],
        include: ["docs", "slides", "pdf", "pptx"],
        credential_mode: "connected",
        dry_run,
      })),
    );
    expect(unlabeledControls(document.body)).toEqual([]);
    expect(await axeViolations(document.body)).toEqual([]);
  });

  it("keeps the draft on a refused check and invalidates a passed check after content or account refresh", async () => {
    open();
    await fill();
    vi.mocked(addKnowledgeSource).mockRejectedValueOnce(
      new ApiError({
        status: 400,
        code: "SOURCE_UNREACHABLE",
        message: "Share the selected file with your connected account.",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Check" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Share the selected file");
    expect(screen.getByLabelText(/^Content links/)).toHaveValue(`${LINK}\n${OTHER}`);
    await check();
    fireEvent.input(screen.getByLabelText(/^Content links/), { target: { value: LINK } });
    expect(screen.getByRole("button", { name: "Add source" })).toBeDisabled();
    await check();
    await userEvent.click(screen.getByRole("button", { name: "Refresh account" }));
    await screen.findByText("Using reader@example.com.");
    expect(screen.getByRole("button", { name: "Add source" })).toBeDisabled();
    expect(screen.getByLabelText(/^Content links/)).toHaveValue(LINK);
  });

  it.each([
    ["Notion", "notion", "https://www.notion.so/Selected-1234567890abcdef1234567890abcdef"],
    ["Linear", "linear", "https://linear.app/team/project/selected-project"],
  ] as const)("uses the caller's %s account and only the selected links", async (label, kind, link) => {
    open();
    await screen.findByText("Using reader@example.com.");
    await userEvent.click(screen.getByRole("radio", { name: label }));
    await screen.findByText(`Using your ${label} account.`);
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: "selected" } });
    fireEvent.input(screen.getByLabelText(/^Content links/), { target: { value: link } });
    await check();
    const [id, form, dry] = vi.mocked(addKnowledgeSource).mock.calls[0];
    expect(sourceAddBody(id.org, id.namespace, form, dry)).toEqual({
      ...NS,
      name: "selected",
      kind,
      links: [link],
      credential_mode: "connected",
      dry_run: true,
    });
  });

  it("clears a shared credential draft when administration is removed while the dialog is open", async () => {
    const props = { open: true, onOpenChange: vi.fn(), ...NS, label: "Team library", onAdded: vi.fn() };
    const { rerender } = render(<AddSourceDialog {...props} allowShared />);
    await screen.findByText("Using reader@example.com.");
    await userEvent.click(screen.getByRole("radio", { name: "Shared credential" }));
    fireEvent.change(screen.getByLabelText(/^Service account key/), { target: { value: "private-shared-key" } });
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: "old-admin-draft" } });
    rerender(<AddSourceDialog {...props} allowShared={false} />);
    expect(screen.queryByLabelText(/^Service account key/)).toBeNull();
    expect(screen.getByLabelText(/^Name/)).toHaveValue("");
    expect(screen.getByRole("radio", { name: "My connected account" })).toBeChecked();
    expect(screen.getByRole("button", { name: "Add source" })).toBeDisabled();
    expect(addKnowledgeSource).not.toHaveBeenCalled();
  });

  it("keeps keyboard focus on Refresh account during its own read", async () => {
    open();
    await screen.findByText("Using reader@example.com.");
    let resolve!: (value: ReturnType<typeof connections>) => void;
    vi.mocked(listConnections).mockImplementationOnce(
      () =>
        new Promise((yes) => {
          resolve = yes;
        }),
    );
    const refresh = screen.getByRole("button", { name: "Refresh account" });
    await userEvent.click(refresh);
    expect(refresh).toHaveFocus();
    expect(refresh).toHaveAttribute("aria-busy", "true");
    await userEvent.click(refresh);
    expect(listConnections).toHaveBeenCalledTimes(2);
    await act(async () => resolve(connections()));
    expect(refresh).toHaveFocus();
    expect(refresh).not.toHaveAttribute("aria-busy");
  });

  it("shows an actionable operator requirement without asking for OAuth app secrets", async () => {
    vi.mocked(listConnections).mockResolvedValue(connections(false, false));
    open();
    expect(await screen.findByText(/An operator must configure Google Drive/)).toBeVisible();
    expect(screen.queryByRole("link", { name: /Connect Google Drive/ })).toBeNull();
    expect(screen.getByRole("button", { name: "Check" })).toBeDisabled();
    expect(document.querySelector('input[type="password"]')).toBeNull();
  });

  it("opens account management separately and keeps private content out of history and storage", async () => {
    vi.mocked(listConnections).mockResolvedValue(connections(false));
    open();
    const link = await screen.findByRole("link", { name: /Connect Google Drive/ });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noreferrer");
    expect(link).toHaveAttribute("href", "/settings/connected-accounts?kind=gdrive&from=source");
    fireEvent.input(screen.getByLabelText(/^Content links/), { target: { value: LINK } });
    expect(`${location.href}${JSON.stringify(localStorage)}${JSON.stringify(sessionStorage)}`).not.toContain(LINK);
    expect(addKnowledgeSource).not.toHaveBeenCalled();
  });

  it("drops late account and check answers after the signed-in principal changes", async () => {
    let resolve!: (value: ReturnType<typeof result>) => void;
    vi.mocked(addKnowledgeSource).mockImplementation(
      () =>
        new Promise((yes) => {
          resolve = yes;
        }),
    );
    const added = open();
    await fill();
    fireEvent.click(screen.getByRole("button", { name: "Check" }));
    await waitFor(() => expect(addKnowledgeSource).toHaveBeenCalledTimes(1));
    act(() => useStore.setState({ whoami: { authenticated: true, subject: "other", org: NS.org, roles: ["writer"] } }));
    await act(async () => resolve(result()));
    expect(screen.queryByText(/Access checked for/)).toBeNull();
    expect(screen.getByLabelText(/^Content links/)).toHaveValue("");
    expect(added).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Add source" })).toBeDisabled();
  });

  it("does not list personal accounts for a service account", async () => {
    useStore.setState({
      whoami: { authenticated: true, subject: "sa:robot", org: NS.org, roles: ["writer"], is_service_account: true },
    });
    open();
    expect(screen.getByText(/Sign in with a personal account/)).toBeVisible();
    await act(async () => {});
    expect(listConnections).not.toHaveBeenCalled();
  });
});
