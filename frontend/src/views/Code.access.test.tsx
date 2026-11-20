/**
 * The Access tab's request and reader rows: each time is the list's one time format, and a
 * page of waiting requests has no primary button per row (one primary per surface).
 */
import { cleanup, fireEvent, render, screen, within } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { GrantEntry } from "../api";
import { ApiError } from "../lib/errors";
import { useStore } from "../lib/state";
import { AccessTab } from "./Code";

const grant = (subject: string, over: Partial<GrantEntry> = {}): GrantEntry => ({
  subject,
  role: "viewer",
  status: "active",
  granted_at: "2025-03-04 10:00:00",
  granted_by: "alice",
  ...over,
});

const listGrants = vi.fn();
const approveGrant = vi.fn();
const revokeGrant = vi.fn();
const removeCodeRepo = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listGrants: (...a: unknown[]) => listGrants(...a),
    approveGrant: (...a: unknown[]) => approveGrant(...a),
    revokeGrant: (...a: unknown[]) => revokeGrant(...a),
    removeCodeRepo: (...a: unknown[]) => removeCodeRepo(...a),
    getNamespace: async () => ({ namespace: "code-app", join_policy: "approval" }),
  };
});
vi.mock("../lib/sharedReads", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/sharedReads")>();
  return { ...actual, sharedIndexStatus: async () => ({ org_readable: false }) };
});

beforeEach(() => {
  useStore.setState({ whoami: { authenticated: true, subject: "alice", org: "dev", roles: ["ADMIN"] } });
  listGrants.mockResolvedValue({
    rows: [
      grant("alice", { role: "admin" }),
      grant("bob", { status: "pending", pending_since: "2025-03-05 09:00:00" }),
      grant("carol", { status: "pending", pending_since: "2025-03-06 09:00:00" }),
    ],
  });
});
afterEach(() => cleanup());

describe("AccessTab rows", () => {
  it("draws a request's wait and a reader's grant time with ListTime", async () => {
    render(<AccessTab id={{ org: "dev", namespace: "code-app" }} />);
    const waiting = await screen.findByRole("region", { name: "Waiting for approval" });
    const asked = waiting.querySelectorAll("time");
    expect([...asked].map((t) => t.getAttribute("dateTime"))).toEqual([
      "2025-03-05T09:00:00.000Z",
      "2025-03-06T09:00:00.000Z",
    ]);
    const readers = screen.getByRole("region", { name: "Readers" });
    const table = within(readers).getByRole("table", { name: "Readers" });
    const added = within(table).getByText(/by alice$/);
    expect(added.querySelector("time")).toHaveAttribute("dateTime", "2025-03-04T10:00:00.000Z");
  });

  // No waiting row has a primary Approve: a list of them would be a column of primaries.
  it("gives no waiting row a primary button", async () => {
    render(<AccessTab id={{ org: "dev", namespace: "code-app" }} />);
    const waiting = await screen.findByRole("region", { name: "Waiting for approval" });
    const approves = within(waiting).getAllByRole("button", { name: /^Approve as/ });
    expect(approves).toHaveLength(2);
    for (const b of approves) expect(b.className).not.toMatch(/primary/);
    expect(within(waiting).getAllByRole("button", { name: "Decline" })[0].className).toMatch(/secondary/);
  });
});

describe("AccessTab before whoami is read", () => {
  it("names no requirement while the caller is unknown, though the readers answered, and sends nothing", async () => {
    useStore.setState({ whoami: null });
    render(<AccessTab id={{ org: "dev", namespace: "code-app" }} />);
    const waiting = await screen.findByRole("region", { name: "Waiting for approval" });
    expect(screen.queryByText(/^Needs maintainer/)).toBeNull();
    const decline = within(waiting).getAllByRole("button", { name: "Decline" })[0];
    expect(decline).toHaveAccessibleDescription("Your role on this namespace is still loading.");
    for (const b of [
      decline,
      within(waiting).getByRole("button", { name: "Approve all" }),
      screen.getByRole("button", { name: "Remove alice" }),
    ])
      fireEvent.click(b);
    await new Promise((r) => setTimeout(r, 20));
    expect(approveGrant).not.toHaveBeenCalled();
    expect(revokeGrant).not.toHaveBeenCalled();
  });
});

describe("AccessTab for a reader who is no maintainer", () => {
  const line = "Needs maintainer on acme/app @ main. A maintainer of it can grant that.";
  const repo = { org: "dev", namespace: "code-app", repo: "acme/app", branch: "main", source: "registry" } as never;

  it("shows every maintainer control unavailable, named by the repository, and sends nothing", async () => {
    useStore.setState({ whoami: { authenticated: true, subject: "dave", org: "dev", roles: ["VIEWER"] } });
    listGrants.mockResolvedValue({
      rows: [
        grant("alice", { role: "admin" }),
        grant("dave"),
        grant("bob", { status: "pending", pending_since: "2025-03-05 09:00:00" }),
        grant("carol", { status: "pending", pending_since: "2025-03-06 09:00:00" }),
      ],
    });
    render(<AccessTab id={{ org: "dev", namespace: "code-app" }} repo={repo} />);
    const waiting = await screen.findByRole("region", { name: "Waiting for approval" });
    expect(screen.getByTestId("access-locked")).toHaveTextContent(line);
    expect(screen.getByLabelText("Join rule")).toHaveAccessibleDescription(line);
    const unavailable = (b: HTMLElement) => {
      expect(b).toHaveAttribute("aria-disabled", "true");
      expect(b).toHaveAccessibleDescription(line);
    };
    unavailable(within(waiting).getByRole("button", { name: "Approve all" }));
    for (const b of within(waiting).getAllByRole("button", { name: /^(Approve as|Decline)/ })) unavailable(b);
    unavailable(screen.getByRole("button", { name: "Remove alice" }));
    unavailable(within(screen.getByRole("form", { name: "Add a reader" })).getByRole("button", { name: "Add" }));
    unavailable(screen.getByRole("button", { name: "Description & more" }));
    unavailable(screen.getByRole("button", { name: "Retire…" }));
    expect(screen.queryByRole("button", { name: "Remove dave" })).toBeNull();
    expect(screen.getByLabelText("Subject to add")).toBeDisabled();
    for (const b of [
      screen.getByRole("button", { name: "Description & more" }),
      within(screen.getByRole("form", { name: "Add a reader" })).getByRole("button", { name: "Add" }),
      within(waiting).getByRole("button", { name: "Approve all" }),
      ...within(waiting).getAllByRole("button", { name: /^(Approve as|Decline)/ }),
      screen.getByRole("button", { name: "Remove alice" }),
      screen.getByRole("button", { name: "Retire…" }),
    ])
      fireEvent.click(b);
    await new Promise((r) => setTimeout(r, 20));
    expect(approveGrant).not.toHaveBeenCalled();
    expect(revokeGrant).not.toHaveBeenCalled();
    expect(removeCodeRepo).not.toHaveBeenCalled();
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("AccessTab's reasons for an unread or a scoped caller", () => {
  const repo = { org: "dev", namespace: "code-app", repo: "acme/app", branch: "main", source: "registry" } as never;

  it("says the access could not be checked when the readers cannot be read", async () => {
    useStore.setState({ whoami: { authenticated: true, subject: "dave", org: "dev", roles: ["VIEWER"] } });
    listGrants.mockRejectedValue(new Error("down"));
    render(<AccessTab id={{ org: "dev", namespace: "code-app" }} repo={repo} />);
    const line = "Couldn't check your access to acme/app @ main.";
    expect(await screen.findByTestId("access-locked")).toHaveTextContent(line);
    expect(screen.getByLabelText("Join rule")).toHaveAccessibleDescription(line);
    for (const name of ["Description & more", "Retire…"])
      expect(screen.getByRole("button", { name })).toHaveAccessibleDescription(line);
    fireEvent.click(screen.getByRole("button", { name: "Retire…" }));
    expect(removeCodeRepo).not.toHaveBeenCalled();
  });

  it("names the maintainer role when the readers read refuses the caller, who is then no reader", async () => {
    useStore.setState({ whoami: { authenticated: true, subject: "dave", org: "dev", roles: ["VIEWER"] } });
    listGrants.mockRejectedValue(new ApiError({ status: 403, code: "AUTH_INSUFFICIENT" }));
    render(<AccessTab id={{ org: "dev", namespace: "code-app" }} repo={repo} />);
    expect(await screen.findByTestId("access-locked")).toHaveTextContent(/^Needs maintainer on acme\/app @ main\./);
    fireEvent.click(screen.getByRole("button", { name: "Retire…" }));
    expect(removeCodeRepo).not.toHaveBeenCalled();
  });

  it("on a restricted repository, says a refused readers read could not check the access", async () => {
    useStore.setState({ whoami: { authenticated: true, subject: "dave", org: "dev", roles: ["VIEWER"] } });
    listGrants.mockRejectedValue(new ApiError({ status: 403, code: "AUTH_INSUFFICIENT" }));
    const restricted = { ...(repo as object), classification: "restricted" } as never;
    render(<AccessTab id={{ org: "dev", namespace: "code-app" }} repo={restricted} />);
    expect(await screen.findByTestId("access-locked")).toHaveTextContent(
      "Couldn't check your access to acme/app @ main.",
    );
  });

  it.each([
    [
      "a maintainer whose token claims below admin",
      { roles: ["VIEWER"], namespaces: ["code-app"] },
      "admin",
      "To change its access, your token must carry an admin claim.",
    ],
    [
      "an admin claim over `*` with no direct grant",
      { roles: ["ADMIN"], namespaces: ["*"] },
      null,
      "To change its access, your token must name code-app, and you need a direct maintainer grant on acme/app @ main.",
    ],
  ])(
    "on a restricted repository, keeps %s from every maintainer write, and sends nothing",
    async (_l, token, own, line) => {
      useStore.setState({
        whoami: { authenticated: true, subject: "dave", org: "dev", ...token },
        myNamespaces: own
          ? [
              {
                namespace: "code-app",
                role: own,
                status: "active",
                visibility: "private",
                description: "",
                joined_at: null,
                is_new: false,
              } as never,
            ]
          : [],
      });
      listGrants.mockResolvedValue({
        rows: [
          grant("dave", { role: "admin" }),
          grant("bob", { status: "pending", pending_since: "2025-03-05 09:00:00" }),
        ],
      });
      const restricted = { ...(repo as object), classification: "restricted" } as never;
      render(<AccessTab id={{ org: "dev", namespace: "code-app" }} repo={restricted} />);
      expect(await screen.findByTestId("access-locked")).toHaveTextContent(line);
      const approve = screen.getAllByRole("button", { name: /^Approve as/ })[0];
      const retire = screen.getByRole("button", { name: "Retire…" });
      for (const b of [approve, retire]) {
        expect(b).toHaveAttribute("aria-disabled", "true");
        fireEvent.click(b);
      }
      await new Promise((r) => setTimeout(r, 20));
      expect(approveGrant).not.toHaveBeenCalled();
      expect(removeCodeRepo).not.toHaveBeenCalled();
      useStore.setState({ myNamespaces: null });
    },
  );

  it("on a restricted repository, says what its gate lacks when the readers read is refused", async () => {
    useStore.setState({
      whoami: { authenticated: true, subject: "dave", org: "dev", roles: ["VIEWER"], namespaces: ["*"] },
      myNamespaces: [],
    });
    listGrants.mockRejectedValue(new ApiError({ status: 403, code: "AUTH_INSUFFICIENT" }));
    const restricted = { ...(repo as object), classification: "restricted" } as never;
    render(<AccessTab id={{ org: "dev", namespace: "code-app" }} repo={restricted} />);
    expect(await screen.findByTestId("access-locked")).toHaveTextContent(
      "To change its access, your token must name code-app and carry an admin claim, and you need a direct maintainer grant on acme/app @ main.",
    );
    useStore.setState({ myNamespaces: null });
  });

  it("says the server decides for a scoped token with no grant row", async () => {
    useStore.setState({
      whoami: { authenticated: true, subject: "dave", org: "dev", roles: [], namespaces: ["code-app"] },
    });
    render(<AccessTab id={{ org: "dev", namespace: "code-app" }} repo={repo} />);
    expect(await screen.findByTestId("access-locked")).toHaveTextContent(
      "The server decides this token's access to acme/app @ main; it does not show here.",
    );
  });

  it("shows a reader Reader instead on a pending maintainer request, unavailable, and sends nothing", async () => {
    useStore.setState({ whoami: { authenticated: true, subject: "dave", org: "dev", roles: ["VIEWER"] } });
    listGrants.mockResolvedValue({
      rows: [
        grant("alice", { role: "admin" }),
        grant("dave"),
        grant("erin", { status: "pending", role: "admin", pending_since: "2025-03-05 09:00:00" }),
      ],
    });
    render(<AccessTab id={{ org: "dev", namespace: "code-app" }} repo={repo} />);
    const instead = await screen.findByRole("button", { name: "Reader instead" });
    expect(instead).toHaveAttribute("aria-disabled", "true");
    expect(instead).toHaveAccessibleDescription(
      "Needs maintainer on acme/app @ main. A maintainer of it can grant that.",
    );
    fireEvent.click(instead);
    await new Promise((r) => setTimeout(r, 20));
    expect(approveGrant).not.toHaveBeenCalled();
  });
});
