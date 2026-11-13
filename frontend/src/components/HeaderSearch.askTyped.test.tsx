import { act, fireEvent, render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { useDock } from "../lib/assistantDock";
import { resetChat } from "../views/assistant/chatStore";
import { HeaderSearch } from "./HeaderSearch";

// Text typed before the answer card's chunk has answered an Enter is another question: the
// one the Enter was for is not asked, and the card names the text in the field.

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

it("asks nothing for an Enter whose text changed while the card's chunk loaded", async () => {
  const user = userEvent.setup();
  render(<HeaderSearch />);
  const field = screen.getByRole("combobox", { name: "Search Loom" });
  await user.type(field, "what does billing remember");
  await screen.findByRole("option", { name: 'Ask AI about "what does billing remember"' });
  // The card's chunk, preloaded once the card shows, then resolves in a later task: Enter
  // and the next keystroke land before it, in one go.
  await import("../views/assistant/SearchAnswer");
  act(() => {
    fireEvent.keyDown(field, { key: "Enter" });
    fireEvent.change(field, { target: { value: "what does billing remember now" } });
  });
  const card = await screen.findByRole("option", { name: 'Ask AI about "what does billing remember now"' });
  await new Promise((r) => setTimeout(r, 50));
  expect(asks.n).toBe(0);
  expect(card).toHaveTextContent("Ask");
  // The text in the field is asked on its own Enter.
  await user.keyboard("{Enter}");
  await waitFor(() => expect(asks.n).toBe(1));
  expect(await screen.findByRole("option", { name: "Continue in chat" })).toBeInTheDocument();
});
