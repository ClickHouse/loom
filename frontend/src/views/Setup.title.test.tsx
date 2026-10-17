import { cleanup, render, screen } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { Setup } from "./Setup";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listServiceAccounts: vi.fn(async () => []),
    listMine: vi.fn(async () => []),
    countMemories: vi.fn(async () => 0),
    listCodeRepos: vi.fn(async () => []),
    listBlacklist: vi.fn(async () => ({ rows: [] })),
  };
});

const WAIT = { timeout: 5000 };

function seed(namespace: string) {
  useStore.setState({
    authed: true,
    whoami: { authenticated: true, subject: "v@x", org: "dev", roles: ["VIEWER"], namespaces: [] },
    myNamespaces: [],
    identity: { org: "dev", namespace, agent: "loom-ui" },
    view: "setup",
  });
}

beforeEach(() => {
  window.history.replaceState(null, "", "/settings");
  resetNamespaceRoleCache();
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("{}", { status: 200 })),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  useStore.setState({ whoami: null, myNamespaces: null, identity: null });
});

describe("Settings", () => {
  it("titles the document after the open tab", async () => {
    seed("team");
    render(<Setup />);
    await screen.findByRole("tab", { name: "Tokens" }, WAIT);
    expect(document.title).toBe("Tokens · Settings · Loom");
    await userEvent.click(screen.getByRole("tab", { name: "Memory" }));
    expect(document.title).toBe("Memory · Settings · Loom");
  });

  it("announces a missing namespace from the alert itself, with no wrapper", async () => {
    seed("");
    render(<Setup />);
    const lead = await screen.findByText("No namespace selected.", undefined, WAIT);
    expect(lead.closest("[role='status']")).toBe(lead.closest("[data-testid='click-alert']"));
  });
});
