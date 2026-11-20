import { StrictMode } from "react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "../test/rtl";
import { axeViolations } from "../test/axe";
import * as api from "../api/connections";
import { confirmAction } from "../components/ConfirmDialog";
import { toast, toastError } from "../components/Toast";
import { canCallOwnConnections } from "../lib/canCall";
import { goToConnectionConsent } from "../lib/navigation";
import { navigationHref, pathnameView, useStore } from "../lib/state";
import { ConnectedAccounts } from "./ConnectedAccounts";

vi.mock("../api/connections", async (original) => ({
  ...(await original<typeof import("../api/connections")>()),
  listConnections: vi.fn(),
  startConnection: vi.fn(),
  disconnectConnection: vi.fn(),
}));
vi.mock("../components/ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));
vi.mock("../components/Toast", () => ({ toast: vi.fn(), toastError: vi.fn() }));
vi.mock("../lib/navigation", () => ({ goToConnectionConsent: vi.fn() }));

function entries(email = "reader@example.com"): api.ConnectionEntry[] {
  return api.CONNECTION_KINDS.map((kind) => ({
    kind,
    connected: kind === "gdrive",
    account_email: kind === "gdrive" ? email : null,
    scopes: ["private-scope-proof"],
    connect_url: `/settings/connected-accounts?kind=${kind}`,
  }));
}

function pending<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function account(subject = "reader", org = "acme") {
  useStore.setState({
    authed: true,
    sessionExpired: false,
    identity: { org, namespace: "", agent: "loom-ui" },
    whoami: { authenticated: true, subject, org, roles: ["viewer"] },
    view: "connected-accounts",
    deniedNamespace: null,
  });
}

beforeEach(() => {
  vi.resetAllMocks();
  history.replaceState(null, "", "/settings/connected-accounts?kind=notion");
  account();
  vi.mocked(api.listConnections).mockResolvedValue({ connections: entries() });
  vi.mocked(api.startConnection).mockResolvedValue({
    authorization_url: "https://consent.example/authorize?state=consent-proof",
  });
  vi.mocked(api.disconnectConnection).mockResolvedValue({ disconnected: true });
  vi.mocked(confirmAction).mockResolvedValue(true);
});
afterEach(() => {
  cleanup();
  useStore.setState({ whoami: null, identity: null, authed: false });
});

describe("Connected accounts", () => {
  it("resolves the public deep link, drops unrelated keys, and focuses the requested provider", async () => {
    expect(pathnameView()).toBe("connected-accounts");
    history.replaceState(null, "", "/settings/connected-accounts?kind=notion&state=drop&code=drop");
    expect(navigationHref("connected-accounts")).toBe("/settings/connected-accounts?kind=notion");
    render(<ConnectedAccounts />);
    await screen.findByText("reader@example.com");
    await waitFor(() => expect(screen.getByRole("heading", { name: "Notion" })).toHaveFocus());
    expect(api.startConnection).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Back to settings" }));
    expect(useStore.getState().view).toBe("setup");
  });

  it("shows three statuses and only supplied emails, without scopes or credential controls", async () => {
    const { container } = render(<ConnectedAccounts />);
    await screen.findByText("reader@example.com");
    expect(screen.getAllByText("Connected", { exact: true })).toHaveLength(1);
    expect(screen.getAllByText("Not connected", { exact: true })).toHaveLength(2);
    expect(screen.queryByText("private-scope-proof")).toBeNull();
    expect(container.querySelector("input")).toBeNull();
    expect(api.listConnections).toHaveBeenCalledWith("acme", { signal: expect.any(AbortSignal) });
    expect(await axeViolations(container)).toEqual([]);
  });

  it("keeps an unconfigured provider's reason keyboard reachable without starting consent", async () => {
    vi.mocked(api.listConnections).mockResolvedValue({
      connections: entries().map((entry) => (entry.kind === "notion" ? { ...entry, connect_available: false } : entry)),
    });
    const { container } = render(<ConnectedAccounts />);
    const connect = await screen.findByRole("button", { name: "Connect Notion" });
    expect(connect).toHaveAttribute("aria-disabled", "true");
    expect(connect).not.toBeDisabled();
    expect(connect).toHaveAccessibleDescription(
      "An operator must configure Notion connections before you can connect your account.",
    );
    connect.focus();
    expect(connect).toHaveFocus();
    await userEvent.keyboard("{Enter}");
    await userEvent.click(connect);
    expect(api.startConnection).not.toHaveBeenCalled();
    expect(goToConnectionConsent).not.toHaveBeenCalled();
    expect(container.querySelector("input")).toBeNull();
    expect(await axeViolations(container)).toEqual([]);
  });

  it("allows disconnect while reconnect needs operator configuration", async () => {
    vi.mocked(api.listConnections).mockResolvedValue({
      connections: entries().map((entry) => (entry.kind === "gdrive" ? { ...entry, connect_available: false } : entry)),
    });
    render(<ConnectedAccounts />);
    const reconnect = await screen.findByRole("button", { name: "Reconnect Google Drive" });
    expect(reconnect).toHaveAttribute("aria-disabled", "true");
    await userEvent.click(screen.getByRole("button", { name: "Disconnect Google Drive" }));
    expect(api.disconnectConnection).toHaveBeenCalledWith("acme", "gdrive", { signal: expect.any(AbortSignal) });
    expect(api.startConnection).not.toHaveBeenCalled();
  });

  it("explains returning to the source tab without starting consent or storing a draft", async () => {
    history.replaceState(null, "", "/settings/connected-accounts?kind=notion&from=source");
    const localBefore = JSON.stringify(localStorage);
    const sessionBefore = JSON.stringify(sessionStorage);
    render(<ConnectedAccounts />);
    expect(screen.getByText("After connecting, return to the tab where you are adding a source.")).toBeInTheDocument();
    await screen.findByRole("button", { name: "Connect Notion" });
    expect(api.startConnection).not.toHaveBeenCalled();
    expect(goToConnectionConsent).not.toHaveBeenCalled();
    expect(JSON.stringify(localStorage)).toBe(localBefore);
    expect(JSON.stringify(sessionStorage)).toBe(sessionBefore);
  });

  it("omits source-tab instructions for an ordinary settings visit", async () => {
    render(<ConnectedAccounts />);
    await screen.findByRole("button", { name: "Connect Notion" });
    expect(screen.queryByText("After connecting, return to the tab where you are adding a source.")).toBeNull();
  });

  it("reads a personal viewer's connections without a namespace and refuses other known identities", () => {
    const whoami = useStore.getState().whoami;
    expect(canCallOwnConnections(whoami)).toBe(true);
    for (const identity of [
      null,
      { authenticated: false },
      { ...whoami, authenticated: true, is_service_account: true },
      { ...whoami, authenticated: true, org: "*" },
    ]) {
      expect(canCallOwnConnections(identity)).toBe(false);
    }
  });

  it("starts consent only after a click and keeps the URL out of dashboard history and storage", async () => {
    render(
      <StrictMode>
        <ConnectedAccounts />
      </StrictMode>,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Connect Notion" }));
    await waitFor(() =>
      expect(goToConnectionConsent).toHaveBeenCalledWith("https://consent.example/authorize?state=consent-proof"),
    );
    const opts = vi.mocked(api.startConnection).mock.calls[0][1];
    expect(opts?.signal?.aborted).toBe(false);
    expect(location.search).toBe("?kind=notion");
    expect(JSON.stringify(localStorage)).not.toContain("consent-proof");
    expect(JSON.stringify(sessionStorage)).not.toContain("consent-proof");
    expect(screen.queryByText(/consent-proof/)).toBeNull();
  });

  it("offers reconnect through the same consent route", async () => {
    render(<ConnectedAccounts />);
    await userEvent.click(await screen.findByRole("button", { name: "Reconnect Google Drive" }));
    expect(api.startConnection).toHaveBeenCalledWith("gdrive", { signal: expect.any(AbortSignal) });
  });

  it("confirms that disconnect stops owned sources, then refreshes status", async () => {
    render(<ConnectedAccounts />);
    await userEvent.click(await screen.findByRole("button", { name: "Disconnect Google Drive" }));
    expect(confirmAction).toHaveBeenCalledWith(
      expect.objectContaining({
        message: "This stops your document sources that use this connection.",
        confirmLabel: "Disconnect",
        danger: true,
      }),
    );
    expect(api.disconnectConnection).toHaveBeenCalledWith("acme", "gdrive", { signal: expect.any(AbortSignal) });
    await waitFor(() => expect(api.listConnections).toHaveBeenCalledTimes(2));
    expect(toast).toHaveBeenCalledWith("Google Drive disconnected", "ok");
  });

  it("does not disconnect when confirmation is cancelled", async () => {
    vi.mocked(confirmAction).mockResolvedValue(false);
    render(<ConnectedAccounts />);
    await userEvent.click(await screen.findByRole("button", { name: "Disconnect Google Drive" }));
    expect(api.disconnectConnection).not.toHaveBeenCalled();
  });

  it("reports consent and disconnect failures through the shared error presentation", async () => {
    const failure = new Error("connection unavailable");
    vi.mocked(api.startConnection).mockRejectedValueOnce(failure);
    vi.mocked(api.disconnectConnection).mockRejectedValueOnce(failure);
    render(<ConnectedAccounts />);
    await userEvent.click(await screen.findByRole("button", { name: "Connect Notion" }));
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Couldn't connect account", failure));
    await userEvent.click(screen.getByRole("button", { name: "Disconnect Google Drive" }));
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Disconnect failed", failure));
    expect(screen.getByText("reader@example.com")).toBeInTheDocument();
    expect(goToConnectionConsent).not.toHaveBeenCalled();
  });

  it("keeps pending and failed reads distinct from disconnected accounts and supports Retry", async () => {
    const load = pending<{ connections: api.ConnectionEntry[] }>();
    vi.mocked(api.listConnections).mockReturnValueOnce(load.promise);
    render(<ConnectedAccounts />);
    expect(screen.queryByText("Not connected")).toBeNull();
    await act(async () => load.reject(new Error("read failed")));
    expect(await screen.findByText("Couldn't load connected accounts")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("reader@example.com")).toBeInTheDocument();
  });

  it("keeps keyboard focus on Refresh and ignores another press while its read is pending", async () => {
    const user = userEvent.setup();
    render(<ConnectedAccounts />);
    await screen.findByText("reader@example.com");
    const load = pending<{ connections: api.ConnectionEntry[] }>();
    vi.mocked(api.listConnections).mockReturnValueOnce(load.promise);
    const refresh = screen.getByRole("button", { name: "Refresh" });
    screen.getByRole("button", { name: "Back to settings" }).focus();
    await user.tab();
    expect(refresh).toHaveFocus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(refresh).toHaveAttribute("aria-busy", "true"));
    expect(refresh).not.toBeDisabled();
    expect(refresh).toHaveFocus();
    expect(screen.getByText("reader@example.com")).toBeInTheDocument();
    await user.keyboard("{Enter}");
    expect(api.listConnections).toHaveBeenCalledTimes(2);
    await act(async () => load.resolve({ connections: entries("refreshed@example.com") }));
    expect(await screen.findByText("refreshed@example.com")).toBeInTheDocument();
    expect(refresh).not.toHaveAttribute("aria-busy");
    expect(refresh).not.toBeDisabled();
    expect(refresh).toHaveFocus();
  });

  it.each(["person", "organization"])("clears displayed and late data on a change of %s", async (change) => {
    render(<ConnectedAccounts />);
    await screen.findByText("reader@example.com");
    const old = pending<{ connections: api.ConnectionEntry[] }>();
    vi.mocked(api.listConnections).mockReturnValueOnce(old.promise);
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    const oldSignal = vi.mocked(api.listConnections).mock.calls.at(-1)?.[1]?.signal;
    const next = pending<{ connections: api.ConnectionEntry[] }>();
    vi.mocked(api.listConnections).mockReturnValueOnce(next.promise);
    act(() => account(change === "person" ? "next-reader" : "reader", change === "organization" ? "next-org" : "acme"));
    expect(screen.queryByText("reader@example.com")).toBeNull();
    expect(oldSignal?.aborted).toBe(true);
    await act(async () => old.resolve({ connections: entries("old-private@example.com") }));
    expect(screen.queryByText("old-private@example.com")).toBeNull();
    await act(async () => next.resolve({ connections: entries("next@example.com") }));
    expect(await screen.findByText("next@example.com")).toBeInTheDocument();
  });

  it("drops late consent navigation and aborts the action after an owner change", async () => {
    const consent = pending<{ authorization_url: string }>();
    vi.mocked(api.startConnection).mockReturnValueOnce(consent.promise);
    render(<ConnectedAccounts />);
    await userEvent.click(await screen.findByRole("button", { name: "Connect Linear" }));
    const signal = vi.mocked(api.startConnection).mock.calls[0][1]?.signal;
    act(() => account("next-reader"));
    expect(signal?.aborted).toBe(true);
    await act(async () => consent.resolve({ authorization_url: "https://consent.example/private" }));
    expect(goToConnectionConsent).not.toHaveBeenCalled();
  });

  it("does not send an old owner's disconnect after confirmation", async () => {
    const confirm = pending<boolean>();
    vi.mocked(confirmAction).mockReturnValueOnce(confirm.promise);
    render(<ConnectedAccounts />);
    await userEvent.click(await screen.findByRole("button", { name: "Disconnect Google Drive" }));
    act(() => account("next-reader"));
    await act(async () => confirm.resolve(true));
    expect(api.disconnectConnection).not.toHaveBeenCalled();
    expect(toastError).not.toHaveBeenCalled();
  });

  it("does not show a previous owner's late action failure", async () => {
    const stop = pending<{ disconnected: boolean }>();
    vi.mocked(api.disconnectConnection).mockReturnValueOnce(stop.promise);
    render(<ConnectedAccounts />);
    await userEvent.click(await screen.findByRole("button", { name: "Disconnect Google Drive" }));
    act(() => account("next-reader"));
    await act(async () => stop.reject(new Error("old owner's failure")));
    expect(toastError).not.toHaveBeenCalled();
    expect(toast).not.toHaveBeenCalled();
  });

  it("makes no connection request for a service account", () => {
    const whoami = useStore.getState().whoami;
    useStore.setState({ whoami: { ...whoami, authenticated: true, is_service_account: true } });
    render(<ConnectedAccounts />);
    expect(screen.getByRole("status")).toHaveTextContent("Sign in with a personal account");
    expect(api.listConnections).not.toHaveBeenCalled();
  });

  it("shows no previous account data or actions after session expiry", async () => {
    render(<ConnectedAccounts />);
    await screen.findByText("reader@example.com");
    act(() => useStore.setState({ sessionExpired: true }));
    expect(screen.queryByText("reader@example.com")).toBeNull();
    expect(screen.queryByRole("button", { name: "Disconnect Google Drive" })).toBeNull();
  });
});
