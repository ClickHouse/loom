import { act, fireEvent, render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { useDock } from "../lib/assistantDock";
import { resetChat } from "../views/assistant/chatStore";
import { HeaderSearch } from "./HeaderSearch";

// A second Enter while the first ask is still on its way (its chunk's import has not
// answered) asks nothing more: one question goes out, and the card is not marked busy by
// its own first ask.

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

it("asks once for two Enters before the first ask has returned", async () => {
  render(<HeaderSearch />);
  const field = screen.getByRole("combobox", { name: "Search Loom" });
  await userEvent.setup().type(field, "what does billing remember");
  await screen.findByRole("option", { name: 'Ask AI about "what does billing remember"' });
  // The card's chunk is loaded (the card preloads it): the first Enter's ask still waits
  // for its import to answer, a later task, when the second Enter lands in the same go.
  await import("../views/assistant/SearchAnswer");
  act(() => {
    fireEvent.keyDown(field, { key: "Enter" });
    fireEvent.keyDown(field, { key: "Enter" });
  });
  await waitFor(() => expect(asks.n).toBe(1));
  expect(await screen.findByRole("option", { name: "Continue in chat" })).toBeInTheDocument();
  // A second ask would have found the first answering and marked the card busy.
  expect(screen.queryByText("An answer is still coming in.")).toBeNull();
  expect(asks.n).toBe(1);
});
