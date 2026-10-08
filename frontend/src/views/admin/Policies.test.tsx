/** The mode badge shows the server's mode verbatim; a mode the UI does not know
 *  takes the off styling and blurb instead of an undefined lookup. */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../api";
import { useStore } from "../../lib/state";
import { Policies } from "./Policies";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    // The wire field is a string; a newer server can send a mode this build has no entry for.
    listPolicies: vi.fn(async () => ({ rules: [], mode: "audit-only" })),
    listPolicyDecisions: vi.fn(async () => ({ rows: [] })),
  };
});

beforeEach(() => {
  useStore.setState({ identity: { org: "dev", namespace: "ns1", agent: "loom-ui" } });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Policies mode badge", () => {
  it("shows an unknown mode verbatim with the off blurb", async () => {
    render(<Policies />);
    expect(await screen.findByText("mode: audit-only")).toBeTruthy();
    expect(screen.getByText(/Rules are stored but never evaluated/)).toBeTruthy();
  });

  it("uses the known mode's blurb", async () => {
    vi.mocked(api.listPolicies).mockResolvedValueOnce({ rules: [], mode: "enforce" });
    render(<Policies />);
    expect(await screen.findByText("mode: enforce")).toBeTruthy();
    expect(screen.getByText(/A deny blocks the call with 403 POLICY_FORBIDDEN/)).toBeTruthy();
  });
});

describe("Policies condition summary", () => {
  it("reads an array where a condition node or args_equal belongs as not an object", async () => {
    vi.mocked(api.listPolicies).mockResolvedValueOnce({
      mode: "enforce",
      rules: [
        {
          policy_id: "p1",
          effect: "forbid",
          actions: ["memory.delete"],
          condition: {
            op: "all",
            of: [{ op: "formerly", operation: "memory.get", within: "1h", args_equal: ["memory_key"] }, ["x"]],
          },
          description: "",
          enabled: true,
          created_by: "alice",
          created_at: null,
          updated_at: null,
        },
      ],
    });
    render(<Policies />);
    expect(await screen.findByText("all(formerly memory.get within 1h AND x)")).toBeTruthy();
  });
});
