import { act, cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ProfileActivityEntry } from "../api";
import { useStore } from "../lib/state";
import { Profile, readPathLabel, BOT_SNIPPET } from "./Profile";

// The page says where the profile actually goes. The plugins inject it at session start
// and every MCP client gets it on connect, so "type use loom" is the fallback, not the
// headline; the bot snippet folds shut; the "read by" chip counts every read path and
// says which one.

const listProfile = vi.fn();
const getProfileActivity = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listProfile: (...args: unknown[]) => listProfile(...args),
    getProfileActivity: (...args: unknown[]) => getProfileActivity(...args),
    isAuthDisabled: () => false,
  };
});

const entry = (over: Partial<ProfileActivityEntry> = {}): ProfileActivityEntry => ({
  agent: "claude-code",
  last_activated_at: new Date(Date.now() - 5 * 60_000).toISOString(),
  event_count: 4,
  operations: ["context.pack"],
  ...over,
});

async function open() {
  render(<Profile />);
  await waitFor(() => expect(listProfile).toHaveBeenCalledTimes(1));
  await screen.findByText("Where this profile goes");
}

describe("readPathLabel", () => {
  it("names each read path in the page's words, and has no words for an unknown op", () => {
    expect(readPathLabel("context.pack")).toBe("session context");
    expect(readPathLabel("mcp.initialize")).toBe("MCP connect");
    expect(readPathLabel("profile.render")).toBe("SDK");
    expect(readPathLabel("loom_activate")).toBe("“use loom”");
    expect(readPathLabel("profile.structured")).toBe("structured profile");
    expect(readPathLabel("something.new")).toBeNull();
  });
});

describe("Profile: where the profile goes", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listProfile.mockResolvedValue({ entries: [{ label: "persona", value: "Terse.", char_limit: 8192 }] });
    getProfileActivity.mockResolvedValue({ entries: [] });
    useStore.setState({
      authed: true,
      identity: { org: "dev", namespace: "ns", agent: "loom-ui" },
      view: "profile",
    });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ identity: null });
  });

  it("leads with automatic delivery and keeps 'use loom' as the fallback for a client that ignores instructions", async () => {
    await open();
    expect(screen.getByText(/Delivered when a session starts/)).toBeInTheDocument();
    expect(screen.getByText(/Delivered when the client connects/)).toBeInTheDocument();
    const useLoom = screen.getByText("use loom");
    expect(useLoom.closest("div")?.textContent).toMatch(/If a client ignores server instructions/);
    expect(screen.queryByText(/At the start of a new conversation, type/)).toBeNull();
  });

  it("offers the plugin install commands behind a disclosure", async () => {
    await open();
    expect(screen.getByText(/claude plugin install loom@loom/)).toBeInTheDocument();
  });

  it("folds the bot snippet shut and pins a current model", async () => {
    await open();
    const details = screen.getByText("Your own bot").closest("details");
    expect(details).toBeInstanceOf(HTMLDetailsElement);
    if (!(details instanceof HTMLDetailsElement)) return;
    expect(details.open).toBe(false);
    expect(BOT_SNIPPET).toContain('model="claude-opus-5"');
  });

  it("the chip says no client read the profile in the window, not that none ever did", async () => {
    await open();
    expect(await screen.findByText("No AI client has read this in the last 7 days")).toBeInTheDocument();
    expect(screen.queryByText("Not yet read by any AI client")).toBeNull();
  });

  it("the chip lists each reader with the path it used", async () => {
    getProfileActivity.mockResolvedValue({
      entries: [entry(), entry({ agent: "cursor", operations: ["mcp.initialize", "loom_activate"], event_count: 2 })],
    });
    await open();
    fireEvent.click(await screen.findByRole("button", { name: /Read by 2 clients/ }));
    expect(screen.getByText("via session context")).toBeInTheDocument();
    expect(screen.getByText("via MCP connect, “use loom”")).toBeInTheDocument();
  });

  it("shows an op it has no words for as another call, the op in the tooltip", async () => {
    getProfileActivity.mockResolvedValue({ entries: [entry({ operations: ["context.pack", "profile.newer_read"] })] });
    await open();
    fireEvent.click(await screen.findByRole("button", { name: /Read by/ }));
    // The line names the path in words; the op is only the tooltip's.
    expect(screen.getByText(/via session context/)).toHaveTextContent("via session context, another call");
    const other = screen.getByText("another call");
    act(() => other.focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent("profile.newer_read");
  });

  it("an empty profile says its readers found nothing, not that they read it", async () => {
    listProfile.mockResolvedValue({ entries: [] });
    getProfileActivity.mockResolvedValue({ entries: [entry(), entry({ agent: "codex" })] });
    render(<Profile />);
    const chip = await screen.findByText(/Checked by 2 clients/);
    expect(chip.textContent).toContain("empty, nothing to read yet");
    expect(screen.queryByText(/Read by/)).toBeNull();
  });
});
