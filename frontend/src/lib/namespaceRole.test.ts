import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import {
  canWrite,
  roleOnNamespace,
  canEditGuide,
  resetNamespaceRoleCache,
  useNamespaceRoleRead,
} from "./namespaceRole";
import { useStore } from "./state";

const listMine = vi.fn<(org: string) => Promise<MyNamespaceEntry[]>>();
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  listMine: (org: string) => listMine(org),
}));

// The role chip and the write-CTA gate share this one rule.

const row = (
  namespace: string,
  role: MyNamespaceEntry["role"],
  status: MyNamespaceEntry["status"] = "active",
): MyNamespaceEntry => ({
  namespace,
  role,
  status,
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
});

const user = (roles: string[]): WhoAmI => ({ authenticated: true, subject: "u@example.com", org: "dev", roles });

describe("roleOnNamespace", () => {
  it("is unknown without whoami or a namespace", () => {
    expect(roleOnNamespace(null, [], "team")).toBeNull();
    expect(roleOnNamespace(user(["WRITER"]), [], "")).toBeNull();
  });

  it("auth off: the operator is admin everywhere", () => {
    expect(roleOnNamespace({ authenticated: false }, null, "local")).toBe("admin");
  });

  it("an admin claim wins before list_mine is even read", () => {
    expect(roleOnNamespace(user(["PLATFORM_ADMIN"]), null, "team")).toBe("admin");
    expect(roleOnNamespace(user(["org_admin"]), [row("team", "viewer")], "team")).toBe("admin");
  });

  it("is unknown while list_mine is loading or failed", () => {
    expect(roleOnNamespace(user(["WRITER"]), null, "team")).toBeNull();
  });

  it("takes the grant row's role", () => {
    const mine = [row("team", "viewer"), row("mine", "admin")];
    expect(roleOnNamespace(user(["WRITER"]), mine, "team")).toBe("viewer");
    expect(roleOnNamespace(user(["WRITER"]), mine, "mine")).toBe("admin");
  });

  it("a claim-only namespace takes the token's flat role — the audit's viewer case", () => {
    const mine = [row("reporter-public", "jwt")];
    expect(roleOnNamespace(user(["VIEWER"]), mine, "reporter-public")).toBe("viewer");
    expect(roleOnNamespace(user(["WRITER"]), mine, "reporter-public")).toBe("writer");
    expect(roleOnNamespace(user([]), mine, "reporter-public")).toBeNull();
  });

  it("a pending join or a namespace not in the list is unknown, not viewer", () => {
    expect(roleOnNamespace(user(["WRITER"]), [row("team", "writer", "pending")], "team")).toBeNull();
    expect(roleOnNamespace(user(["WRITER"]), [row("other", "writer")], "team")).toBeNull();
  });
});

describe("canWrite", () => {
  it("locks only a known viewer", () => {
    expect(canWrite("viewer")).toBe(false);
    expect(canWrite("writer")).toBe(true);
    expect(canWrite("admin")).toBe(true);
    expect(canWrite(null)).toBe(true);
  });
});

describe("canEditGuide", () => {
  it("lets a known writer or admin write a namespace's guide", () => {
    expect(canEditGuide("writer", "team")).toBe(true);
    expect(canEditGuide("admin", "personal-alice")).toBe(true);
  });

  it("keeps a viewer, an unknown role and a personal collection reading", () => {
    expect(canEditGuide("viewer", "team")).toBe(false);
    expect(canEditGuide(null, "team")).toBe(false);
    expect(canEditGuide("admin", "col-me-alice")).toBe(false);
  });
});

describe("useNamespaceRoleRead", () => {
  afterEach(() => {
    resetNamespaceRoleCache();
    listMine.mockReset();
    useStore.setState({ whoami: null, identity: null, myNamespaces: null });
  });

  const signIn = () =>
    useStore.setState({
      whoami: user(["USER"]),
      identity: { org: "dev", namespace: "team", agent: "loom-ui" },
      myNamespaces: null,
    });

  it("is loading until list_mine answers, then holds the role", async () => {
    let answer: (rows: MyNamespaceEntry[]) => void = () => {};
    listMine.mockImplementationOnce(() => new Promise((r) => (answer = r)));
    signIn();
    const { result } = renderHook(() => useNamespaceRoleRead("team"));
    expect(result.current).toEqual({ role: null, loading: true });
    await act(async () => answer([row("team", "viewer")]));
    expect(result.current).toEqual({ role: "viewer", loading: false });
  });

  it("stops loading when list_mine fails, with the role unknown", async () => {
    listMine.mockRejectedValueOnce(new Error("down"));
    signIn();
    const { result } = renderHook(() => useNamespaceRoleRead("team"));
    await act(async () => {});
    expect(result.current).toEqual({ role: null, loading: false });
  });

  it("drops the rows of the org it left, and loads again for the new one", async () => {
    listMine.mockResolvedValueOnce([row("team", "writer")]);
    signIn();
    const { result } = renderHook(() => useNamespaceRoleRead("team"));
    await act(async () => {});
    expect(result.current).toEqual({ role: "writer", loading: false });
    let answer: (rows: MyNamespaceEntry[]) => void = () => {};
    listMine.mockImplementationOnce(() => new Promise((r) => (answer = r)));
    act(() => useStore.setState({ identity: { org: "acme", namespace: "team", agent: "loom-ui" } }));
    expect(result.current).toEqual({ role: null, loading: true });
    await act(async () => answer([row("team", "viewer")]));
    expect(result.current).toEqual({ role: "viewer", loading: false });
    expect(listMine).toHaveBeenLastCalledWith("acme");
  });

  it("is not loading before an org is chosen", () => {
    useStore.setState({ whoami: user(["USER"]), identity: null, myNamespaces: null });
    const { result } = renderHook(() => useNamespaceRoleRead("team"));
    expect(result.current).toEqual({ role: null, loading: false });
  });

  it("reads nothing when the store already holds the rows", () => {
    signIn();
    useStore.setState({ myNamespaces: [row("team", "writer")] });
    const { result } = renderHook(() => useNamespaceRoleRead("team"));
    expect(result.current).toEqual({ role: "writer", loading: false });
    expect(listMine).not.toHaveBeenCalled();
  });
});
