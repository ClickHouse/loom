import { cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { invalidateShared } from "../lib/sharedReads";
import { NamespacePicker } from "./NamespacePicker";

// A namespace someone just shared is badged New in the picker until it is picked there,
// which tells the server it was seen.

const row = (namespace: string, is_new: boolean): MyNamespaceEntry => ({
  namespace,
  role: "viewer",
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new,
});
const rows = [row("team", false), row("billing", true)];

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  isAuthDisabled: () => false,
  listMine: vi.fn(async () => rows),
  markNamespaceSeen: vi.fn(async () => undefined),
  setSavedNamespace: vi.fn(async () => undefined),
}));
import { markNamespaceSeen } from "../api";

const whoami: WhoAmI = {
  authenticated: true,
  subject: "v@x",
  org: "dev",
  roles: ["VIEWER"],
  namespaces: ["team", "billing"],
};

describe("NamespacePicker's New badge", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    invalidateShared();
    useStore.setState({
      authed: true,
      whoami,
      myNamespaces: rows,
      identity: { org: "dev", namespace: "team", agent: "loom-ui" },
    });
  });
  afterEach(() => cleanup());

  const open = () => {
    render(<NamespacePicker />);
    fireEvent.click(screen.getByRole("button", { name: /team/ }));
  };
  const rowButton = (name: string) =>
    waitFor(() => {
      const el = document.querySelector<HTMLElement>(`[data-nav="ns:${name}"]`);
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });

  it("badges a new namespace, and picking it marks it seen and drops the badge", async () => {
    open();
    const billing = await rowButton("billing");
    expect(within(billing).getByText("New")).toBeInTheDocument();
    fireEvent.click(billing);
    await waitFor(() => expect(markNamespaceSeen).toHaveBeenCalledWith("dev", "billing"));
    expect(useStore.getState().myNamespaces?.find((r) => r.namespace === "billing")?.is_new).toBe(false);
  });

  it("marks nothing when the namespace picked is not new", async () => {
    useStore.setState({ identity: { org: "dev", namespace: "billing", agent: "loom-ui" } });
    render(<NamespacePicker />);
    fireEvent.click(screen.getByRole("button", { name: /billing/ }));
    fireEvent.click(await rowButton("team"));
    await waitFor(() => expect(useStore.getState().identity?.namespace).toBe("team"));
    expect(markNamespaceSeen).not.toHaveBeenCalled();
  });
});
