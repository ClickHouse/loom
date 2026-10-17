import { describe, expect, it } from "vitest";
import type { MyNamespaceEntry } from "../api";
import { agentTokenScope, describeTokenScope } from "./tokenScope";

const row = (
  namespace: string,
  kind: MyNamespaceEntry["kind"],
  status: "active" | "pending" = "active",
): MyNamespaceEntry => ({
  namespace,
  role: "viewer",
  status,
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
  kind,
});

const repos = [
  { namespace: "code-ch", repo: "org/ch", branch: "master", classification: "standard" },
  { namespace: "code-ch-26-8", repo: "org/ch", branch: "26.8", classification: "standard" },
  { namespace: "code-private", repo: "org/private", branch: "master", classification: "restricted" },
];

describe("agentTokenScope", () => {
  it("reads every active membership; the home pick only sets where it writes", () => {
    const s = agentTokenScope(
      [row("me", "memory"), row("team", "memory"), row("code-ch", "code"), row("code-ch-26-8", "code")],
      repos,
      "me",
    );
    expect(s.home).toBe("me");
    expect(s.namespaces).toEqual(["me", "team"]);
    expect(s.repositories).toEqual([
      {
        repo: "org/ch",
        branches: [
          { namespace: "code-ch", branch: "master" },
          { namespace: "code-ch-26-8", branch: "26.8" },
        ],
      },
    ]);
    expect(s.excludedRestricted).toEqual([]);
    expect(s.pending).toEqual([]);
  });

  it("a restricted repository the person belongs to is listed as excluded, never as readable", () => {
    const s = agentTokenScope([row("me", "memory"), row("code-private", "code")], repos, "me");
    expect(s.repositories).toEqual([]);
    expect(s.excludedRestricted).toEqual([
      { repo: "org/private", branches: [{ namespace: "code-private", branch: "master" }] },
    ]);
  });

  it("pending joins are neither readable nor excluded; they are pending", () => {
    const s = agentTokenScope([row("me", "memory"), row("shared", "memory", "pending")], null, "me");
    expect(s.namespaces).toEqual(["me"]);
    expect(s.pending).toEqual(["shared"]);
  });

  it("the home namespace is always in the read set, even before the list loads", () => {
    const s = agentTokenScope(null, null, "me");
    expect(s.namespaces).toEqual(["me"]);
    expect(s.repositories).toEqual([]);
  });

  it("a code namespace with no repo facts keeps its identifier and counts as readable", () => {
    const s = agentTokenScope([row("code-x", "code")], null, "me");
    expect(s.repositories).toEqual([{ repo: "code-x", branches: [{ namespace: "code-x", branch: "code-x" }] }]);
  });
});

describe("describeTokenScope", () => {
  it("an agent token claiming `*` says reads follow membership", () => {
    expect(describeTokenScope({ kind: "mcp", namespaces: ["me", "*"], roles: ["writer"] })).toBe(
      "writes me · reads every namespace and repository you belong to",
    );
  });
  it("an agent token for a named namespace reads only that one", () => {
    expect(describeTokenScope({ kind: "mcp", namespaces: ["me"], roles: ["writer"] })).toBe(
      "writes me · reads only me",
    );
  });
  it("a wildcard agent token writes anywhere the person can", () => {
    expect(describeTokenScope({ kind: "mcp", namespaces: ["*"], roles: ["writer"] })).toMatch(
      /^writes any namespace you can write · reads every/,
    );
  });
  it("an agent token's further claims are restricted namespaces it reads by name", () => {
    expect(
      describeTokenScope({ kind: "mcp", namespaces: ["me", "code-private", "code-private-26-8"], roles: ["writer"] }),
    ).toBe("writes me · reads only me · plus restricted: code-private, code-private-26-8");
  });
  it("a scoped token reads only what it names", () => {
    expect(describeTokenScope({ kind: "agent", namespaces: ["a", "b"], roles: ["viewer"] })).toBe(
      "reads only viewer on a, b",
    );
  });
});
