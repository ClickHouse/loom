/** A dialog whose namespace changes while a load is in flight shows the new namespace's
 *  data, even when the old namespace's response lands last. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "../test/rtl";
import { MembersDialog } from "./MembersDialog";
import { ProfileSectionDialog } from "./ProfileSectionDialog";
import { InboxSettingsDialog } from "./InboxSettingsDialog";
import { NamespaceSettingsDialog } from "./NamespaceSettingsDialog";
import { useStore } from "../lib/state";
import * as api from "../api";
import * as tasks from "../api/tasks";
import type { GrantEntry, NamespaceInfo, ProfileListResponse } from "../api";
import type { Inbox, PosterPolicy } from "../api/tasks";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, listGrants: vi.fn(), listProfile: vi.fn(), getNamespace: vi.fn(), reregisterNamespace: vi.fn() };
});
vi.mock("../api/tasks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/tasks")>();
  return {
    ...actual,
    listPrincipals: vi.fn(async () => ({ principals: [], truncated: false })),
    listPolicies: vi.fn(),
  };
});

function deferred<T>() {
  let resolve: (v: T) => void = () => undefined;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

const noop = () => undefined;

beforeEach(() => {
  vi.mocked(api.listGrants).mockReset();
  vi.mocked(api.listProfile).mockReset();
  vi.mocked(api.getNamespace).mockReset();
  vi.mocked(api.reregisterNamespace).mockReset();
  vi.mocked(tasks.listPolicies).mockReset();
  useStore.setState({ whoami: null });
});

const grant = (subject: string): GrantEntry => ({
  subject,
  role: "viewer",
  status: "active",
  granted_at: "2026-09-20T00:00:00Z",
  granted_by: "admin",
});

const nsRow = (namespace: string, description = `${namespace} description`): NamespaceInfo => ({
  namespace,
  owner_email: `${namespace}@example.com`,
  description,
  tags: [],
  active: true,
  consolidation_enabled: false,
  conflict_threshold: null,
  created_at: "",
  updated_at: "",
});

describe("a stale namespace load is dropped", () => {
  it("MembersDialog", async () => {
    useStore.setState({ whoami: { authenticated: true, subject: "me", org: "dev", roles: [] } });
    const oldLoad = deferred<{ rows: GrantEntry[] }>();
    const newLoad = deferred<{ rows: GrantEntry[] }>();
    vi.mocked(api.listGrants).mockReturnValueOnce(oldLoad.promise).mockReturnValueOnce(newLoad.promise);
    const { rerender } = render(<MembersDialog open onOpenChange={noop} org="dev" namespace="old-ns" />);
    rerender(<MembersDialog open onOpenChange={noop} org="dev" namespace="new-ns" />);
    await act(async () => newLoad.resolve({ rows: [grant("new-member")] }));
    await act(async () => oldLoad.resolve({ rows: [grant("old-member")] }));
    expect(screen.getByText("new-member")).toBeInTheDocument();
    expect(screen.queryByText("old-member")).toBeNull();
  });

  it("ProfileSectionDialog", async () => {
    const section = (value: string): ProfileListResponse => ({
      entries: [{ label: "bio", value, char_limit: null, content_sha256: "x", version: 1, created_at: "" }],
      truncated: false,
    });
    const oldLoad = deferred<ProfileListResponse>();
    const newLoad = deferred<ProfileListResponse>();
    vi.mocked(api.listProfile).mockReturnValueOnce(oldLoad.promise).mockReturnValueOnce(newLoad.promise);
    useStore.setState({ identity: { org: "dev", namespace: "old-ns", agent: "a" }, inspectedProfileLabel: "bio" });
    render(<ProfileSectionDialog />);
    act(() => useStore.setState({ identity: { org: "dev", namespace: "new-ns", agent: "a" } }));
    await act(async () => newLoad.resolve(section("new namespace bio")));
    await act(async () => oldLoad.resolve(section("old namespace bio")));
    expect(screen.getByText("new namespace bio")).toBeInTheDocument();
    expect(screen.queryByText("old namespace bio")).toBeNull();
    act(() => useStore.setState({ inspectedProfileLabel: null, identity: null }));
  });

  it("InboxSettingsDialog trust table", async () => {
    const rule = (poster: string): PosterPolicy => ({
      org: "dev",
      inbox: "bot",
      poster,
      kind: "*",
      mode: "review",
      require_evidence: [],
      set_by: "",
      set_at: "",
      version: 1,
    });
    const inbox: Inbox = {
      org: "dev",
      inbox: "bot",
      owner_agent: "bot",
      owner_namespace: "old-ns",
      description: "",
      posters: [],
      accept_disclosure: ["internal"],
      default_lease_seconds: 1800,
      max_attempts: 3,
      max_defers: 5,
      triage_escalate_after_seconds: 14400,
      webhook_url: "",
      created_by: "",
      created_at: "2026-09-20T00:00:00",
      updated_at: "2026-09-20T00:00:00",
      version: 1,
    };
    const oldLoad = deferred<PosterPolicy[]>();
    const newLoad = deferred<PosterPolicy[]>();
    vi.mocked(tasks.listPolicies).mockReturnValueOnce(oldLoad.promise).mockReturnValueOnce(newLoad.promise);
    const { rerender } = render(
      <InboxSettingsDialog
        open
        onOpenChange={noop}
        identity={{ org: "dev", namespace: "old-ns" }}
        inbox={inbox}
        onSaved={noop}
      />,
    );
    rerender(
      <InboxSettingsDialog
        open
        onOpenChange={noop}
        identity={{ org: "dev", namespace: "new-ns" }}
        inbox={inbox}
        onSaved={noop}
      />,
    );
    await act(async () => newLoad.resolve([rule("new-poster")]));
    await act(async () => oldLoad.resolve([rule("old-poster")]));
    expect(screen.getByText("new-poster")).toBeInTheDocument();
    expect(screen.queryByText("old-poster")).toBeNull();
  });

  it("NamespaceSettingsDialog", async () => {
    useStore.setState({ whoami: { authenticated: true, subject: "me", org: "dev", roles: [] } });
    const firstLoad = deferred<NamespaceInfo | null>();
    const lateLoad = deferred<NamespaceInfo | null>();
    const newLoad = deferred<NamespaceInfo | null>();
    vi.mocked(api.getNamespace)
      .mockReturnValueOnce(firstLoad.promise)
      .mockReturnValueOnce(lateLoad.promise)
      .mockReturnValueOnce(newLoad.promise);
    const { rerender } = render(<NamespaceSettingsDialog open onOpenChange={noop} org="dev" namespace="old-ns" />);
    await act(async () => firstLoad.resolve(nsRow("old-ns")));
    expect(screen.getByDisplayValue("old-ns description")).toBeInTheDocument();

    // Close and reopen on old-ns (its read stays in flight), then switch to new-ns: the
    // row already loaded for old-ns is not shown under new-ns while new-ns's read is out.
    rerender(<NamespaceSettingsDialog open={false} onOpenChange={noop} org="dev" namespace="old-ns" />);
    rerender(<NamespaceSettingsDialog open onOpenChange={noop} org="dev" namespace="old-ns" />);
    await act(async () => undefined);
    rerender(<NamespaceSettingsDialog open onOpenChange={noop} org="dev" namespace="new-ns" />);
    expect(screen.queryByDisplayValue("old-ns description")).toBeNull();
    expect(await screen.findByText("Loading namespace settings")).toBeInTheDocument();

    await act(async () => newLoad.resolve(nsRow("new-ns")));
    await act(async () => lateLoad.resolve(nsRow("old-ns")));
    expect(screen.getByDisplayValue("new-ns description")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("old-ns description")).toBeNull();
  });

  it("NamespaceSettingsDialog save refetch", async () => {
    useStore.setState({
      whoami: { authenticated: true, subject: "me", org: "dev", roles: [] },
      myNamespaces: null,
    });
    const refetch = deferred<NamespaceInfo | null>();
    vi.mocked(api.reregisterNamespace).mockResolvedValue({ registered: true, namespace: "old-ns", updated_at: "" });
    vi.mocked(api.getNamespace)
      .mockResolvedValueOnce(nsRow("old-ns"))
      .mockReturnValueOnce(refetch.promise)
      .mockResolvedValueOnce(nsRow("new-ns"));
    const { rerender } = render(<NamespaceSettingsDialog open onOpenChange={noop} org="dev" namespace="old-ns" />);
    await act(async () => undefined);
    fireEvent.change(screen.getByDisplayValue("old-ns description"), { target: { value: "edited" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save" })));
    expect(api.reregisterNamespace).toHaveBeenCalledWith({ org: "dev", namespace: "old-ns" }, nsRow("old-ns"), {
      description: "edited",
    });

    rerender(<NamespaceSettingsDialog open onOpenChange={noop} org="dev" namespace="new-ns" />);
    await act(async () => undefined);
    await act(async () => refetch.resolve(nsRow("old-ns", "edited")));
    expect(screen.getByDisplayValue("new-ns description")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("edited")).toBeNull();
  });
});
