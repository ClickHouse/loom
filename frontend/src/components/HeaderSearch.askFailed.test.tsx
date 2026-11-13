import { render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { useDock } from "../lib/assistantDock";
import { resetChat } from "../views/assistant/chatStore";
import { HeaderSearch } from "./HeaderSearch";

// The answer card's chunk fails to load: the ask says so, and the next Enter tries again.

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listMemories: vi.fn(async () => ({ entries: [], truncated: false, total: 0 })),
    listKnowledge: vi.fn(async () => ({ rows: [] })),
    listProfile: vi.fn(async () => ({ entries: [] })),
    browseNamespaces: vi.fn(async () => []),
    searchMemory: vi.fn(async () => ({ results: [] })),
  };
});
vi.mock("../lib/sharedReads", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/sharedReads")>();
  return { ...actual, sharedMine: vi.fn(async () => []) };
});

const asks = vi.hoisted(() => ({ n: 0 }));
// The card's chunk never loads (a redeploy removed it, the network dropped).
vi.mock("../views/assistant/SearchAnswer", () => {
  throw new Error("Failed to fetch dynamically imported module");
});
const toasts = vi.hoisted(() => [] as string[]);
vi.mock("./Toast", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./Toast")>();
  return { ...actual, toastError: (context: string) => void toasts.push(context) };
});
vi.mock("../api/assistantChat", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/assistantChat")>();
  // Each question asked is one chat request, which stays open.
  return {
    ...actual,
    streamAssistantChat: vi.fn(() => {
      asks.n += 1;
      return new Promise<void>(() => undefined);
    }),
  };
});

beforeEach(() => {
  resetChat(null);
  useDock.setState({ open: false, mounted: false, reason: null, reasonShort: "" });
  useStore.setState({
    identity: { org: "dev", namespace: "team", agent: "dashboard" },
    whoami: { subject: "ada", org: "dev", namespaces: ["team"] } as never,
    myNamespaces: null,
    view: "memories",
  });
});

it("says an ask whose chunk failed to load, and asks again on the next Enter", async () => {
  const user = userEvent.setup();
  render(<HeaderSearch />);
  await user.type(screen.getByRole("combobox", { name: "Search Loom" }), "what does billing remember");
  await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
  await user.keyboard("{Enter}");
  await waitFor(() => expect(toasts).toEqual(["Couldn't ask the assistant"]));
  await user.keyboard("{Enter}");
  await waitFor(() => expect(toasts).toHaveLength(2));
  expect(asks.n).toBe(0);
  expect(screen.getByRole("option", { name: "Ask AI: what does billing remember" })).toBeInTheDocument();
});
