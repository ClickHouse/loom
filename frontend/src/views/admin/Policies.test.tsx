/** The mode badge shows the server's mode verbatim; a mode the UI does not know
 *  takes the off styling and blurb instead of an undefined lookup. */
import { act, cleanup, fireEvent, render, screen, waitFor } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../api";
import { useStore } from "../../lib/state";
import { ConfirmViewport } from "../../components/ConfirmDialog";
import { Policies } from "./Policies";
import type { MyNamespaceEntry, WhoAmI } from "../../api";
import { resetNamespaceRoleCache } from "../../lib/namespaceRole";

// The caller's own grant on each namespace the tests use: the role its writes need.
const grant = (namespace: string, role: MyNamespaceEntry["role"] = "admin"): MyNamespaceEntry => ({
  namespace,
  role,
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
});
const person: WhoAmI = {
  authenticated: true,
  subject: "p@example.com",
  org: "dev",
  roles: ["USER"],
  namespaces: ["ns1", "a", "b"],
};

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    // The wire field is a string; a newer server can send a mode this build has no entry for.
    listPolicies: vi.fn(async () => ({ rules: [], mode: "audit-only" })),
    listPolicyDecisions: vi.fn(async () => ({ rows: [] })),
    setPolicy: vi.fn(async (_id, body) => ({ rule: { ...body }, leaf_count: 1 })),
    deletePolicy: vi.fn(async () => ({ deleted: true })),
  };
});

beforeEach(() => {
  resetNamespaceRoleCache();
  useStore.setState({
    whoami: person,
    myNamespaces: [grant("ns1"), grant("a"), grant("b")],
    identity: { org: "dev", namespace: "ns1", agent: "loom-ui" },
  });
});
afterEach(() => {
  cleanup();
  useStore.setState({ whoami: null, myNamespaces: null });
  vi.clearAllMocks();
});

describe("Policies mode badge", () => {
  it("shows an unknown mode verbatim with the off blurb", async () => {
    render(<Policies />);
    expect(await screen.findByText("mode: audit-only")).toBeTruthy();
    expect(screen.getByText(/Rules are stored but never evaluated/)).toBeTruthy();
  });

  it("announces how many rules it holds", async () => {
    render(<Policies />);
    expect(await screen.findByText("0 rules")).toHaveAttribute("aria-live", "polite");
  });

  it("uses the known mode's blurb", async () => {
    vi.mocked(api.listPolicies).mockResolvedValueOnce({ rules: [], mode: "enforce" });
    render(<Policies />);
    expect(await screen.findByText("mode: enforce")).toBeTruthy();
    expect(screen.getByText(/A deny blocks the call, and the caller is told a policy refused it/)).toBeTruthy();
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

  it("opens the condition's JSON from a disclosure button", async () => {
    vi.mocked(api.listPolicies).mockResolvedValueOnce({
      mode: "enforce",
      rules: [
        {
          policy_id: "p1",
          effect: "require",
          actions: ["memory.delete"],
          condition: { op: "all", of: [] },
          description: "",
          enabled: true,
          created_by: "alice",
          created_at: null,
          updated_at: null,
        },
      ],
    });
    render(<Policies />);
    const toggle = await screen.findByRole("button", { name: "all()" });
    const json = document.getElementById(toggle.getAttribute("aria-controls") ?? "");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(json).not.toBeVisible();
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(json).toBeVisible();
    expect(screen.getByText("Require")).toBeInTheDocument();
  });
});

describe("Policies add rule", () => {
  it("submits the dialog's fields with the chosen effect", async () => {
    const user = userEvent.setup();
    render(<Policies />);
    await user.click(await screen.findByRole("button", { name: /Add rule/ }));
    await user.type(screen.getByLabelText("Policy ID"), "no-blind-delete");
    await user.click(screen.getByRole("button", { name: /Effect/ }));
    // Click UI's Select items carry no option role; the open list's item is the one with a data-value.
    await waitFor(() => expect(document.querySelector('[data-value="require"]')).not.toBeNull());
    await user.click(document.querySelector<HTMLElement>('[data-value="require"]') as HTMLElement);
    await user.click(screen.getByRole("button", { name: /Save rule/ }));
    await waitFor(() =>
      expect(api.setPolicy).toHaveBeenCalledWith(
        expect.anything(),
        expect.objectContaining({ policy_id: "no-blind-delete", effect: "require", actions: ["memory.delete"] }),
      ),
    );
  });
});

describe("Policies add rule, role falls short", () => {
  it("closes the open dialog when the role falls below admin, and sends nothing", async () => {
    const user = userEvent.setup();
    render(<Policies />);
    await user.click(await screen.findByRole("button", { name: /Add rule/ }));
    expect(await screen.findByLabelText("Policy ID")).toBeInTheDocument();
    act(() => useStore.setState({ myNamespaces: [grant("ns1", "writer")] }));
    await waitFor(() => expect(screen.queryByLabelText("Policy ID")).toBeNull());
    expect(api.setPolicy).not.toHaveBeenCalled();
  });
});

describe("Policies below admin", () => {
  it("keeps Add rule and Delete in view, unavailable with why, and sends nothing", async () => {
    useStore.setState({ myNamespaces: [grant("ns1", "writer")] });
    vi.mocked(api.listPolicies).mockResolvedValueOnce({
      mode: "enforce",
      rules: [
        {
          policy_id: "p1",
          effect: "forbid",
          actions: ["memory.delete"],
          condition: { op: "all", of: [] },
          description: "",
          enabled: true,
          created_by: "alice",
          created_at: null,
          updated_at: null,
        },
      ],
    });
    const user = userEvent.setup();
    render(<Policies />);
    const needs = "Needs admin on ns1. An admin of it can grant that.";
    const add = await screen.findByRole("button", { name: /Add rule/ });
    const remove = await screen.findByRole("button", { name: "Delete p1" });
    for (const button of [add, remove]) {
      expect(button).toHaveAttribute("aria-disabled", "true");
      expect(button).toHaveAccessibleDescription(needs);
      await user.click(button);
    }
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(api.deletePolicy).not.toHaveBeenCalled();
  });

  it("deletes a rule for an admin", async () => {
    vi.mocked(api.listPolicies).mockResolvedValueOnce({
      mode: "enforce",
      rules: [
        {
          policy_id: "p1",
          effect: "forbid",
          actions: ["memory.delete"],
          condition: { op: "all", of: [] },
          description: "",
          enabled: true,
          created_by: "alice",
          created_at: null,
          updated_at: null,
        },
      ],
    });
    render(
      <>
        <Policies />
        <ConfirmViewport />
      </>,
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Delete p1" }));
    await user.click(await screen.findByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.deletePolicy).toHaveBeenCalled());
  });
});

// A rule's "updated" time is the list's one time format, a <time> with the absolute UTC time
// behind it.
describe("Policies rule time", () => {
  it("draws a rule's update time with ListTime", async () => {
    vi.mocked(api.listPolicies).mockResolvedValueOnce({
      mode: "enforce",
      rules: [
        {
          policy_id: "p1",
          effect: "forbid",
          actions: ["memory.delete"],
          condition: { op: "all", of: [] },
          description: "",
          enabled: true,
          created_by: "alice",
          created_at: null,
          updated_at: "2025-03-04 10:00:00",
        },
      ],
    });
    render(<Policies />);
    const time = (await screen.findByText(/^updated/)).querySelector("time");
    expect(time).toHaveAttribute("dateTime", "2025-03-04T10:00:00.000Z");
    expect(time).toHaveTextContent("Mar 4, 2025");
  });
});

describe("Policies across a namespace switch", () => {
  it("a delete answered after a namespace switch does not paint the old namespace's rules", async () => {
    const rule = (id: string) => ({
      policy_id: id,
      effect: "forbid",
      actions: ["memory.delete"],
      condition: { op: "all", of: [] },
      description: "",
      enabled: true,
      created_by: "alice",
      created_at: null,
      updated_at: "2025-03-04 10:00:00",
    });
    vi.mocked(api.listPolicies).mockImplementation(async (id) =>
      (id as { namespace: string }).namespace === "b"
        ? ({ rules: [rule("b-rule")], mode: "enforce" } as never)
        : ({ rules: [rule("a-rule")], mode: "enforce" } as never),
    );
    let answer: () => void = () => {};
    vi.mocked(api.deletePolicy).mockImplementationOnce(() => new Promise<never>((r) => (answer = r as () => void)));
    useStore.setState({ identity: { org: "dev", namespace: "a", agent: "loom-ui" } });
    render(
      <>
        <Policies />
        <ConfirmViewport />
      </>,
    );
    await screen.findByText("a-rule");
    fireEvent.click(screen.getByRole("button", { name: /Delete/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    act(() => useStore.setState({ identity: { org: "dev", namespace: "b", agent: "loom-ui" } }));
    await screen.findByText("b-rule");
    await act(async () => answer());
    expect(screen.queryByText("a-rule")).toBeNull();
    expect(screen.getByText("b-rule")).toBeInTheDocument();
  });
});

describe("Policies, a failed read", () => {
  it("says the rules and the decisions failed to load, never that there are none, and Retry reads again", async () => {
    vi.mocked(api.listPolicies).mockRejectedValueOnce(new Error("down"));
    vi.mocked(api.listPolicyDecisions).mockRejectedValueOnce(new Error("down"));
    render(<Policies />);
    expect(await screen.findByText("Couldn't load rules")).toBeInTheDocument();
    expect(await screen.findByText("Couldn't load decisions")).toBeInTheDocument();
    expect(screen.queryByText("No rules")).toBeNull();
    const retries = screen.getAllByRole("button", { name: "Retry" });
    fireEvent.click(retries[0]);
    fireEvent.click(retries[1]);
    await waitFor(() => expect(screen.queryByText("Couldn't load rules")).toBeNull());
    await waitFor(() => expect(screen.queryByText("Couldn't load decisions")).toBeNull());
    expect(api.listPolicies).toHaveBeenCalledTimes(2);
    expect(api.listPolicyDecisions).toHaveBeenCalledTimes(2);
  });
});

describe("Policies mode before a read answers", () => {
  it("claims no mode while loading, nor after a failed read", async () => {
    vi.mocked(api.listPolicies).mockRejectedValueOnce(new Error("down"));
    render(<Policies />);
    expect(screen.queryByText(/^mode: /)).toBeNull();
    expect(await screen.findByText("Couldn't load rules")).toBeInTheDocument();
    expect(screen.queryByText(/^mode: /)).toBeNull();
  });
});
