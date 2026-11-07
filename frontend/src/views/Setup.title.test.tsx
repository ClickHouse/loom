import { cleanup, render, screen } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { listTokens } from "../api";
import { Setup } from "./Setup";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listTokens: vi.fn(async () => []),
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

// A token's created and used times are the list's one time format, quiet in the row.
describe("Settings tokens", () => {
  it("draws a token's created and last-used times with ListTime", async () => {
    vi.mocked(listTokens).mockResolvedValueOnce([
      {
        name: "ci-bot",
        sa_subject: "sa:ci-bot",
        namespaces: ["team"],
        roles: ["writer"],
        ttl_days: 90,
        created_by: "v@x",
        created_at: "2025-03-04 10:00:00",
        expires_at: null,
        revoked_at: null,
        revoked_by: "",
        last_used_at: "2025-03-05 09:00:00",
      },
    ]);
    seed("team");
    render(<Setup />);
    const created = await screen.findByText(/created/, { selector: "div" }, WAIT);
    const times = [...created.querySelectorAll("time")];
    expect(times.map((t) => t.getAttribute("dateTime"))).toEqual([
      "2025-03-04T10:00:00.000Z",
      "2025-03-05T09:00:00.000Z",
    ]);
    expect(times.every((t) => !t.hasAttribute("tabindex"))).toBe(true);
  });
});
