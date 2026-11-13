import { act, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { axeViolations } from "../test/axe";
import { useStore } from "../lib/state";
import { useDock } from "../lib/assistantDock";
import type { ChatHandlers, ChatMessage } from "../api/assistantChat";
import { ask, newConversation, resetChat, useChat } from "../views/assistant/chatStore";
import { ApiError } from "../lib/errors";
import { AssistantLauncher } from "../layout/AssistantLauncher";
import { searchMemory } from "../api";
import { HeaderSearch } from "./HeaderSearch";

// The header search answers a question in place: an "Ask AI" card through the assistant's
// chat store, streaming into the card, and Continue in the assistant on the same
// conversation.

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listMemories: vi.fn(async () => ({ entries: [], truncated: false, total: 0 })),
    listKnowledge: vi.fn(async () => ({ rows: [] })),
    listProfile: vi.fn(async () => ({ entries: [] })),
    browseNamespaces: vi.fn(async () => []),
    searchMemory: vi.fn(async () => ({
      results: [{ memory_key: "billing/invoices", agent: "a", memory_type: "semantic", content_excerpt: "weekly" }],
    })),
  };
});
vi.mock("../lib/sharedReads", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/sharedReads")>();
  return { ...actual, sharedMine: vi.fn(async () => []) };
});

/** Each chat request the store sends, and the handlers the test answers it through. */
const chats = vi.hoisted(
  () =>
    [] as Array<{
      namespace: string;
      messages: ChatMessage[];
      on: ChatHandlers;
      end: () => void;
      fail: (e: unknown) => void;
    }>,
);
vi.mock("../api/assistantChat", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/assistantChat")>();
  return {
    ...actual,
    streamAssistantChat: vi.fn(
      (namespace: string, messages: ChatMessage[], on: ChatHandlers) =>
        new Promise<void>((end, fail) => chats.push({ namespace, messages, on, end, fail })),
    ),
  };
});

const field = () => screen.getByRole("combobox", { name: "Search Loom" });
const status = () => document.getElementById("loom-assistant-status") as HTMLElement;
// The popover's own violations: the page's landmarks are the shell's (e2e/a11y.spec.ts).
const panelViolations = async () =>
  (await axeViolations(document.querySelector("[data-header-search-panel]") as Element)).filter(
    (v) => !v.startsWith("region "),
  );

/** The chat request answered: `text`, a supported label for one found source, the end. */
async function answer(text: string) {
  await waitFor(() => expect(chats).toHaveLength(1));
  const c = chats[0];
  await act(async () => {
    c.on.onText(text);
  });
  await act(async () => {
    c.on.onTextDone?.();
    c.on.onCitations?.(
      [{ kind: "memory", text: "[memory:billing/invoices@v1]", verified: true, reason: "", ref: "billing/invoices" }],
      "supported",
      [],
      { steps: [{ op: "memory.search", text: "Searched memories", ok: true }], cut: false, listed: true },
    );
    c.end();
  });
}

beforeEach(() => {
  chats.length = 0;
  window.__LOOM_ASSISTANT = undefined;
  resetChat(null);
  useDock.setState({ open: false, mounted: false, unread: false, working: false, reason: null, reasonShort: "" });
  useStore.setState({
    identity: { org: "dev", namespace: "team", agent: "dashboard" },
    whoami: { subject: "ada", org: "dev", namespaces: ["team"] } as never,
    myNamespaces: null,
    view: "memories",
  });
  const region = document.createElement("p");
  region.id = "loom-assistant-status";
  document.body.append(region);
});

afterEach(() => {
  localStorage.removeItem("loom_token_v1");
  status().remove();
  window.__LOOM_ASSISTANT = undefined;
});

describe("the search's answer card", () => {
  it("leads the results for a question, highlighted, and typing asks nothing", async () => {
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(field(), "what does billing remember");
    const card = await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
    expect(screen.getAllByRole("option")[0]).toBe(card);
    await waitFor(() => expect(field()).toHaveAttribute("aria-activedescendant", card.id));
    expect(card).toHaveTextContent("Enter");
    await new Promise((r) => setTimeout(r, 300));
    expect(chats).toHaveLength(0);
    expect(await panelViolations()).toEqual([]);
  });

  it("is a row after the results for keywords, and Tab or a click on the row shows it first", async () => {
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(field(), "billing invoices");
    await screen.findByRole("group", { name: "Best matches" });
    const options = screen.getAllByRole("option");
    expect(options[0]).toHaveTextContent("billing/invoices");
    expect(options.at(-1)).toHaveAccessibleName("Ask AI: billing invoices");
    // Enter opens the highlighted result as before.
    expect(field()).toHaveAttribute("aria-activedescendant", options[0].id);
    await user.keyboard("{Tab}");
    expect(field()).toHaveFocus();
    const card = screen.getAllByRole("option")[0];
    expect(card).toHaveAccessibleName("Ask AI: billing invoices");
    expect(within(screen.getByRole("group", { name: "Assistant" })).getByRole("option")).toBe(card);
    expect(field()).toHaveAttribute("aria-activedescendant", card.id);
    expect(chats).toHaveLength(0);

    await user.clear(field());
    await user.type(field(), "billing weekly");
    await screen.findByRole("group", { name: "Best matches" });
    await user.click(screen.getAllByRole("option").at(-1) as HTMLElement);
    expect(screen.getAllByRole("option")[0]).toHaveAccessibleName("Ask AI: billing weekly");
    expect(chats).toHaveLength(0);
  });

  it("Enter asks once, the answer streams into the card, and its end is said, not marked unread", async () => {
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(field(), "how often are invoices sent?");
    await screen.findByRole("option", { name: "Ask AI: how often are invoices sent?" });
    await user.keyboard("{Enter}");
    await waitFor(() => expect(chats).toHaveLength(1));
    // The conversation's question, with the page line the panel sends.
    expect(chats[0].namespace).toBe("team");
    expect(chats[0].messages).toEqual([
      { role: "user", content: "Context: Memories page\n\nhow often are invoices sent?" },
    ]);
    expect(useDock.getState().working).toBe(true);
    await answer("Invoices go out **weekly**, on Fridays.");
    const card = screen.getByTestId("search-answer");
    expect(within(card).getByTestId("search-answer-text")).toHaveTextContent("Invoices go out weekly, on Fridays.");
    expect(within(card).getByTestId("assistant-support")).toHaveTextContent("Checked against what Loom holds");
    // The source by its key, numbered as its marker would be in the text.
    expect(within(card).getByRole("list", { name: "Sources" })).toHaveTextContent("billing/invoices");
    expect(within(card).getByRole("list", { name: "Sources" })).not.toHaveTextContent("memory:");
    expect(card).toHaveTextContent("What it read (1)");
    expect(within(card).getByRole("button", { name: "Continue in the assistant" })).toBeInTheDocument();
    expect(field()).toHaveFocus();
    await waitFor(() => expect(status()).toHaveTextContent("The answer is complete."));
    expect(useDock.getState().unread).toBe(false);
    expect(useDock.getState().working).toBe(false);
    expect(chats).toHaveLength(1);
    expect(await panelViolations()).toEqual([]);
  });

  it("Enter once the answer has started, or Continue, opens the panel on the same conversation", async () => {
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(field(), "what does billing remember");
    await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
    await user.keyboard("{Enter}");
    await answer("Weekly invoices.");
    const id = useChat.getState().id;
    expect(id).not.toBeNull();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(useDock.getState().open).toBe(true));
    expect(screen.queryByRole("listbox", { name: "Search results" })).toBeNull();
    const s = useChat.getState();
    expect(s.id).toBe(id);
    expect(s.turns.at(-1)).toMatchObject({ question: "what does billing remember", answer: "Weekly invoices." });
    expect(chats).toHaveLength(1);
  });

  it("Ctrl+Enter asks and continues at once", async () => {
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(field(), "who wrote the invoice policy");
    await screen.findByRole("option", { name: "Ask AI: who wrote the invoice policy" });
    await user.keyboard("{Control>}{Enter}{/Control}");
    await waitFor(() => expect(useDock.getState().open).toBe(true));
    await waitFor(() => expect(chats).toHaveLength(1));
    expect(useChat.getState().turns.at(-1)).toMatchObject({ question: "who wrote the invoice policy" });
    await answer("Ada.");
    expect(useChat.getState().turns.at(-1)).toMatchObject({ answer: "Ada.", state: "done" });
  });

  it("closed while it answers, the answer goes on and is marked ready", async () => {
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(field(), "what does billing remember");
    await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
    await user.keyboard("{Enter}");
    await waitFor(() => expect(chats).toHaveLength(1));
    await user.keyboard("{Escape}");
    expect(screen.queryByTestId("search-answer")).toBeNull();
    expect(useDock.getState().working).toBe(true);
    await answer("Weekly invoices.");
    expect(useDock.getState().unread).toBe(true);
    // Opened again, the card shows the answer.
    await user.click(field());
    expect(await screen.findByTestId("search-answer")).toHaveTextContent("Weekly invoices.");
  });

  it("asks a conversation about another namespace in a new one", async () => {
    // The signed-in person's conversation (the owner the search adopts), about another namespace.
    useChat.setState({
      owner: { subject: "ada", org: "dev", scope: "-", user: "-" },
      id: "old",
      namespace: "other",
      turns: [{ id: "t1", question: "before", answer: "earlier", state: "done", at: 1 }],
    });
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(field(), "what does billing remember");
    await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
    await user.keyboard("{Enter}");
    await waitFor(() => expect(chats).toHaveLength(1));
    expect(chats[0].messages).toHaveLength(1);
    expect(useChat.getState().id).not.toBe("old");
    expect(useChat.getState().namespace).toBe("team");
    // Said as the panel's Ask about this says it.
    const line = "New conversation in team; the one about other is in History.";
    expect(useChat.getState().notice).toBe(line);
    await waitFor(() => expect(status()).toHaveTextContent(line));
  });

  it("is not offered with the chat off or a reason known, and Tab is left alone", async () => {
    window.__LOOM_ASSISTANT = false;
    const user = userEvent.setup();
    const { unmount } = render(
      <>
        <HeaderSearch />
        <button type="button">after</button>
      </>,
    );
    await user.type(field(), "what does billing remember");
    await screen.findByRole("group", { name: "Best matches" });
    expect(screen.queryByRole("option", { name: /^Ask AI/ })).toBeNull();
    await user.keyboard("{Tab}");
    expect(field()).not.toHaveFocus();
    unmount();

    window.__LOOM_ASSISTANT = undefined;
    useDock.setState({ reason: { kind: "model_calls_off" }, reasonShort: "model calls are off" });
    render(<HeaderSearch />);
    await user.type(field(), "what does billing remember");
    await screen.findByRole("group", { name: "Best matches" });
    expect(screen.queryByRole("option", { name: /^Ask AI/ })).toBeNull();
    await user.keyboard("{Enter}");
    expect(chats).toHaveLength(0);
  });

  it("once asked is named after what Enter does: nothing before the answer starts, then continue", async () => {
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(field(), "what does billing remember");
    await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
    await user.keyboard("{Enter}");
    const waiting = await screen.findByRole("option", { name: "Answering: what does billing remember" });
    expect(waiting).toHaveTextContent("Answering: what does billing remember");
    expect(waiting).not.toHaveTextContent("Enter");
    // Enter does nothing yet.
    await user.keyboard("{Enter}");
    expect(useDock.getState().open).toBe(false);
    await act(async () => {
      chats[0].on.onText("Weekly.");
    });
    const card = await screen.findByRole("option", { name: "Continue in the assistant: what does billing remember" });
    expect(card).toHaveTextContent("Continue in the assistant: what does billing remember");
    // The row drops its Enter hint: Continue's own chord says how to go on.
    expect(card).not.toHaveTextContent("Enter");
    await user.keyboard("{Enter}");
    await waitFor(() => expect(useDock.getState().open).toBe(true));
  });

  it("found busy, leaves the panel's answer unread when it ends, and says nothing of it", async () => {
    // The panel's own question, still answering.
    ask("from the panel", "team");
    await waitFor(() => expect(chats).toHaveLength(1));
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(field(), "what does billing remember");
    await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
    await user.keyboard("{Enter}");
    expect(await screen.findByTestId("search-answer")).toHaveTextContent("An answer is still coming in.");
    await act(async () => {
      chats[0].on.onText("The panel's answer.");
      chats[0].on.onTextDone?.();
      chats[0].end();
    });
    await waitFor(() => expect(useDock.getState().unread).toBe(true));
    expect(status()).not.toHaveTextContent("The answer is complete.");
    expect(chats).toHaveLength(1);
    // Nothing answers now: the card asks again, and Enter asks.
    const card = await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
    expect(card).toHaveTextContent("Enter");
    await user.keyboard("{Enter}");
    await waitFor(() => expect(chats).toHaveLength(2));
    // Given back to the panel's field while busy, it leaves it once asked.
    expect(useDock.getState().draft).toBe("");
  });

  it("is not offered for a token whose claims keep the assistant from answering, the panel never loaded", async () => {
    const b64 = (o: object) => btoa(JSON.stringify(o)).replace(/=+$/, "");
    localStorage.setItem("loom_token_v1", `${b64({ alg: "HS256" })}.${b64({ org: "*", sub: "ada" })}.sig`);
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(field(), "what does billing remember");
    await screen.findByRole("group", { name: "Best matches" });
    expect(screen.queryByRole("option", { name: /^Ask AI/ })).toBeNull();
    await user.keyboard("{Enter}");
    expect(chats).toHaveLength(0);
  });

  it("after the route refuses for a reason, says it, marks the launcher, and offers no card again", async () => {
    const user = userEvent.setup();
    render(
      <>
        <AssistantLauncher />
        <HeaderSearch />
      </>,
    );
    await user.type(field(), "what does billing remember");
    await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
    await user.keyboard("{Enter}");
    await waitFor(() => expect(chats).toHaveLength(1));
    await act(async () => {
      chats[0].fail(
        new ApiError({ status: 403, code: "AUTH_INSUFFICIENT", message: "narrowed", detail: { reason: "narrowed" } }),
      );
    });
    expect(await screen.findByTestId("search-answer")).toHaveTextContent("The assistant can't answer");
    expect(screen.getByRole("button", { name: "Ask AI, unavailable" })).toBeInTheDocument();
    await user.clear(field());
    await user.type(field(), "who wrote the invoice policy");
    await screen.findByRole("group", { name: "Best matches" });
    expect(screen.queryByRole("option", { name: /^Ask AI/ })).toBeNull();
    expect(chats).toHaveLength(1);
  });

  it("goes back to asking once its turn leaves the conversation shown", async () => {
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(field(), "what does billing remember");
    await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
    await user.keyboard("{Enter}");
    await answer("Weekly invoices.");
    act(() => newConversation());
    const card = await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
    expect(card).toHaveTextContent("Enter");
    expect(screen.queryByTestId("search-answer")).toBeNull();
  });

  it("leaves Tab alone once Esc has closed the results", async () => {
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(field(), "billing invoices");
    await screen.findByRole("group", { name: "Best matches" });
    await user.keyboard("{Escape}");
    await user.keyboard("{Tab}");
    // The focus moves on (to the field's clear button), and nothing reopens.
    expect(field()).not.toHaveFocus();
    expect(screen.queryByRole("listbox", { name: "Search results" })).toBeNull();
  });

  it("keeps the card, saying why, when another tab holds the conversation, and Enter continues", async () => {
    // Another tab holds every conversation's lock.
    const locks = { request: (_n: string, _o: unknown, f: (l: null) => unknown) => Promise.resolve(f(null)) };
    Object.defineProperty(navigator, "locks", { value: locks, configurable: true });
    try {
      const user = userEvent.setup();
      render(<HeaderSearch />);
      await user.type(field(), "what does billing remember");
      await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
      await user.keyboard("{Enter}");
      expect(await screen.findByTestId("search-answer")).toHaveTextContent("Another tab was answering");
      await new Promise((r) => setTimeout(r, 100));
      expect(screen.getByTestId("search-answer")).toHaveTextContent("Another tab was answering");
      expect(chats).toHaveLength(0);
      await screen.findByRole("option", { name: "Continue in the assistant: what does billing remember" });
      await user.keyboard("{Enter}");
      await waitFor(() => expect(useDock.getState().open).toBe(true));
      // The question it could not ask waits in the panel's field.
      expect(useDock.getState().draft).toBe("what does billing remember");
    } finally {
      Reflect.deleteProperty(navigator, "locks");
    }
  });

  it("found busy, says so only while that answer runs: a refusal of it is said instead", async () => {
    ask("from the panel", "team");
    await waitFor(() => expect(chats).toHaveLength(1));
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(field(), "what does billing remember");
    await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
    await user.keyboard("{Enter}");
    const card = await screen.findByTestId("search-answer");
    expect(card).toHaveTextContent("An answer is still coming in.");
    await act(async () => {
      chats[0].fail(
        new ApiError({ status: 403, code: "AUTH_INSUFFICIENT", message: "narrowed", detail: { reason: "narrowed" } }),
      );
    });
    await waitFor(() => expect(screen.getByTestId("search-answer")).toHaveTextContent("The assistant can't answer"));
    expect(screen.getByTestId("search-answer")).not.toHaveTextContent("still coming in");
  });

  it("refused, gives the question to the panel's field and leaves its Ask about this chip", async () => {
    const item = { view: "memories" as const, org: "dev", namespace: "team", chip: "key a/b", line: 'memory "a/b"' };
    useDock.setState({ item, pageOff: false, draft: "" });
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(field(), "what does billing remember");
    await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
    await user.keyboard("{Enter}");
    await waitFor(() => expect(chats).toHaveLength(1));
    // The search's question went without the item.
    expect(chats[0].messages.at(-1)?.content).toBe("Context: Memories page\n\nwhat does billing remember");
    await act(async () => {
      chats[0].fail(
        new ApiError({ status: 403, code: "AUTH_INSUFFICIENT", message: "narrowed", detail: { reason: "narrowed" } }),
      );
    });
    await waitFor(() => expect(useDock.getState().draft).toBe("what does billing remember"));
    expect(useDock.getState().item).toEqual(item);
    expect(useDock.getState().pageOff).toBe(false);
  });

  it("after Tab, an edit goes back to results first, and Enter opens the top hit", async () => {
    const inspect = vi.fn();
    useStore.setState({ inspect });
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(field(), "billing");
    await screen.findByRole("group", { name: "Best matches" });
    await user.keyboard("{Tab}");
    expect(screen.getAllByRole("option")[0]).toHaveAccessibleName("Ask AI: billing");
    // Edited without ever emptying the field.
    await user.type(field(), " invoices");
    await screen.findByRole("group", { name: "Best matches" });
    expect(screen.getAllByRole("option")[0]).not.toHaveAccessibleName(/^Ask AI/);
    await waitFor(() => expect(screen.getAllByRole("option")[0]).toHaveTextContent("billing/invoices"));
    await user.keyboard("{Enter}");
    expect(inspect).toHaveBeenCalledWith("billing/invoices", { agent: "a" });
    expect(chats).toHaveLength(0);
  });

  it("found busy, Continue opens the panel with the question in its field and its chip kept", async () => {
    const item = { view: "memories" as const, org: "dev", namespace: "team", chip: "key a/b", line: 'memory "a/b"' };
    useDock.setState({ item, pageOff: false, draft: "" });
    ask("from the panel", "team", { context: null, item: null }, null, { fromField: false });
    await waitFor(() => expect(chats).toHaveLength(1));
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(field(), "what does billing remember");
    await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
    await user.keyboard("{Enter}");
    expect(await screen.findByTestId("search-answer")).toHaveTextContent("An answer is still coming in.");
    await user.click(screen.getByRole("button", { name: "Continue in the assistant" }));
    await waitFor(() => expect(useDock.getState().open).toBe(true));
    expect(useDock.getState().draft).toBe("what does billing remember");
    expect(useDock.getState().item).toEqual(item);
    expect(chats).toHaveLength(1);
  });

  it("says an answer that is only a table holds one, to read in the assistant", async () => {
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(field(), "what does billing remember");
    await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
    await user.keyboard("{Enter}");
    await answer("| Account | Cadence |\n| --- | --- |\n| north | weekly |");
    const card = screen.getByTestId("search-answer");
    expect(within(card).queryByTestId("search-answer-text")).toBeNull();
    expect(card).toHaveTextContent("The answer holds a table or code: continue in the assistant to read it.");
  });

  describe("Ctrl/⌘ Enter on a card not asked yet, when the question cannot be asked", () => {
    const item = { view: "memories" as const, org: "dev", namespace: "team", chip: "key a/b", line: 'memory "a/b"' };

    it("with another answer coming in, opens the panel with the question in its field", async () => {
      useDock.setState({ item, pageOff: false, draft: "" });
      ask("from the panel", "team", { context: null, item: null }, null, { fromField: false });
      await waitFor(() => expect(chats).toHaveLength(1));
      const user = userEvent.setup();
      render(<HeaderSearch />);
      await user.type(field(), "what does billing remember");
      await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
      await user.keyboard("{Control>}{Enter}{/Control}");
      await waitFor(() => expect(useDock.getState().open).toBe(true));
      expect(useDock.getState().draft).toBe("what does billing remember");
      expect(useDock.getState().item).toEqual(item);
      expect(chats).toHaveLength(1);
    });

    it("with another tab holding the conversation, opens the panel with the question in its field", async () => {
      useDock.setState({ item, pageOff: false, draft: "" });
      const locks = { request: (_n: string, _o: unknown, f: (l: null) => unknown) => Promise.resolve(f(null)) };
      Object.defineProperty(navigator, "locks", { value: locks, configurable: true });
      try {
        const user = userEvent.setup();
        render(<HeaderSearch />);
        await user.type(field(), "what does billing remember");
        await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
        await user.keyboard("{Meta>}{Enter}{/Meta}");
        await waitFor(() => expect(useDock.getState().open).toBe(true));
        await waitFor(() => expect(useDock.getState().draft).toBe("what does billing remember"));
        expect(useDock.getState().item).toEqual(item);
        expect(chats).toHaveLength(0);
      } finally {
        Reflect.deleteProperty(navigator, "locks");
      }
    });
  });

  it("a question another tab kept from being asked leaves the panel's field once it is asked", async () => {
    let held = true;
    const locks = {
      request: (_n: string, _o: unknown, f: (l: object | null) => unknown) => Promise.resolve(f(held ? null : {})),
    };
    Object.defineProperty(navigator, "locks", { value: locks, configurable: true });
    try {
      const user = userEvent.setup();
      render(<HeaderSearch />);
      await user.type(field(), "what does billing remember");
      await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
      await user.keyboard("{Enter}");
      await waitFor(() => expect(useDock.getState().draft).toBe("what does billing remember"));
      expect(chats).toHaveLength(0);
      // The other tab let go; the card asks again, and this time it is asked.
      held = false;
      await user.clear(field());
      await user.type(field(), "what does billing remember");
      await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
      await user.keyboard("{Enter}");
      await waitFor(() => expect(chats).toHaveLength(1));
      expect(useDock.getState().draft).toBe("");
    } finally {
      Reflect.deleteProperty(navigator, "locks");
    }
  });

  it("offers no card for a question longer than one request carries, and sends nothing", async () => {
    const user = userEvent.setup();
    render(<HeaderSearch />);
    // 20,001 characters ending in "?": a question, over the route's length with or without
    // the page line.
    const long = `what ${"x".repeat(19_995)}?`;
    expect(long).toHaveLength(20_001);
    await user.click(field());
    await user.paste(long);
    await screen.findByRole("group", { name: "Best matches" });
    expect(screen.queryByRole("option", { name: /^Ask AI/ })).toBeNull();
    await user.keyboard("{Tab}");
    expect(screen.queryByRole("option", { name: /^Ask AI/ })).toBeNull();
    expect(chats).toHaveLength(0);
  });

  it("with the page line on, a question that would pass the length only without it gets a card once the chip is off", async () => {
    const user = userEvent.setup();
    render(<HeaderSearch />);
    // 19,990 characters: under one request's length alone, over it with "Context: Memories page".
    const long = `what ${"x".repeat(19_984)}?`;
    expect(long).toHaveLength(20_000 - 10);
    await user.click(field());
    await user.paste(long);
    await screen.findByRole("group", { name: "Best matches" });
    expect(screen.queryByRole("option", { name: /^Ask AI/ })).toBeNull();
    act(() => useDock.setState({ pageOff: true }));
    expect(await screen.findByRole("option", { name: `Ask AI: ${long}` })).toBeInTheDocument();
    act(() => useDock.setState({ pageOff: false }));
  });

  it("is offered while the setup starts or fails, and says a refusal it causes", async () => {
    useDock.setState({ reason: { kind: "starting" }, reasonShort: "setting up" });
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(field(), "what does billing remember");
    await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
    await user.keyboard("{Enter}");
    await waitFor(() => expect(chats).toHaveLength(1));
    await act(async () => {
      chats[0].fail(
        new ApiError({
          status: 503,
          code: "ASSISTANT_UNAVAILABLE",
          message: "not set up yet",
          detail: { reason: "starting" },
        }),
      );
    });
    expect(await screen.findByTestId("search-answer")).toHaveTextContent("The assistant can't answer: setting up.");
  });

  it("keeps results first for a question with a search filter, and Tab still shows the card", async () => {
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(field(), "tag:billing what is due?");
    await screen.findByRole("group", { name: "Best matches" });
    expect(screen.getAllByRole("option")[0]).not.toHaveAccessibleName(/^Ask AI/);
    expect(screen.getAllByRole("option").at(-1)).toHaveAccessibleName("Ask AI: tag:billing what is due?");
    await user.keyboard("{Tab}");
    expect(screen.getAllByRole("option")[0]).toHaveAccessibleName("Ask AI: tag:billing what is due?");
    expect(chats).toHaveLength(0);
  });

  it("drops an answer asked in one namespace when the header moves to another", async () => {
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(field(), "what does billing remember");
    await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
    await user.keyboard("{Enter}");
    await answer("Weekly invoices.");
    expect(screen.getByTestId("search-answer")).toHaveTextContent("Weekly invoices.");
    act(() => useStore.setState({ identity: { org: "dev", namespace: "ops", agent: "dashboard" } }));
    await waitFor(() => expect(screen.queryByTestId("search-answer")).toBeNull());
    expect(await screen.findByRole("option", { name: "Ask AI: what does billing remember" })).toHaveTextContent(
      "Enter",
    );
  });

  it("says no miss and no filter tips while the card is offered", async () => {
    vi.mocked(searchMemory).mockResolvedValue({ results: [] } as never);
    try {
      const user = userEvent.setup();
      render(<HeaderSearch />);
      await user.type(field(), "what does billing remember");
      await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
      // The search has answered and its "Searching" status gone: what is left is what shows.
      await waitFor(() =>
        expect(searchMemory).toHaveBeenCalledWith(expect.anything(), "what does billing remember", expect.anything()),
      );
      const popover = document.querySelector("[data-header-search-panel]") as HTMLElement;
      await waitFor(() => expect(within(popover).queryByRole("status")).toBeNull());
      expect(screen.queryByText(/No matches for/)).toBeNull();
      expect(screen.queryByText(/^Try/)).toBeNull();
      // Keywords with no card still say they matched nothing.
      await user.clear(field());
      window.__LOOM_ASSISTANT = false;
      await user.type(field(), "zzzz");
      expect(await screen.findByText(/No matches for/)).toBeInTheDocument();
    } finally {
      vi.mocked(searchMemory).mockResolvedValue({
        results: [{ memory_key: "billing/invoices", agent: "a", memory_type: "semantic", content_excerpt: "weekly" }],
      } as never);
    }
  });

  it("draws each citation in the preview as its number, linking to its row once checked", async () => {
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(field(), "what does billing remember");
    await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
    await user.keyboard("{Enter}");
    // The answer cites two memories; the check found the first (the `answer` helper's).
    await answer("Weekly [memory:billing/invoices@v1], retried daily [memory:billing/retries@v2].");
    const text = screen.getByTestId("search-answer-text");
    expect(text).not.toHaveTextContent("memory:");
    expect(text).toHaveTextContent("Weekly 1, retried daily 2.");
    const one = within(text).getByRole("link", { name: "Source 1" });
    // No tab stop: the rows below are the keyboard's way to each source.
    expect(one).toHaveAttribute("tabindex", "-1");
    expect(within(text).queryByRole("link", { name: "Source 2" })).toBeNull();
    const row = within(screen.getByRole("list", { name: "Sources" })).getByRole("listitem");
    expect(row).toHaveTextContent(/^1\. billing\/invoices$/);
    await user.click(one);
    expect(within(row).getByRole("button", { name: "billing/invoices" })).toHaveFocus();
  });

  it("says it also asks in its placeholder where the card can be offered", () => {
    const { unmount } = render(<HeaderSearch />);
    expect(field()).toHaveAttribute("placeholder", "Search or ask a question");
    unmount();
    window.__LOOM_ASSISTANT = false;
    render(<HeaderSearch />);
    expect(field()).toHaveAttribute("placeholder", "Search profile, memories, library…");
  });

  it("draws numbers inside emphasis, code and list items, with no raw Markdown and no duplicate keys", async () => {
    const errors = vi.spyOn(console, "error");
    try {
      const user = userEvent.setup();
      render(<HeaderSearch />);
      await user.type(field(), "what does billing remember");
      await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
      await user.keyboard("{Enter}");
      await answer(
        "**Weekly [memory:billing/invoices@v1]** and `retry [memory:billing/codes@v1]`.\n\n- reminders [memory:billing/retries@v2]\n- [memory:billing/invoices@v1] again",
      );
      const text = screen.getByTestId("search-answer-text");
      expect(text).not.toHaveTextContent("**");
      expect(text).not.toHaveTextContent("`");
      expect(within(text).getByText("Weekly", { exact: false }).closest("strong")).not.toBeNull();
      // The bold marker is a number; the one inside a code span stays code.
      expect(text.querySelector("strong sup")).toHaveTextContent("1");
      expect(text.querySelector("code")).toHaveTextContent("retry [memory:billing/codes@v1]");
      // The code span's marker takes no number: the list item's is 2, not 3.
      expect(text).toHaveTextContent("reminders 2");
      expect(text).toHaveTextContent("1 again");
      expect(errors.mock.calls.flat().join(" ")).not.toMatch(/same key/);
    } finally {
      errors.mockRestore();
    }
  });

  it("lists the card's sources in the order of their numbers in the text", async () => {
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(field(), "what does billing remember");
    await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
    await user.keyboard("{Enter}");
    await waitFor(() => expect(chats).toHaveLength(1));
    const c = chats[0];
    await act(async () => {
      c.on.onText("Retried [memory:billing/retries@v2], weekly [memory:billing/invoices@v1].");
      c.on.onTextDone?.();
      // The server lists them the other way round.
      c.on.onCitations?.(
        [
          { kind: "memory", text: "[memory:billing/invoices@v1]", verified: true, reason: "", ref: "billing/invoices" },
          { kind: "memory", text: "[memory:billing/retries@v2]", verified: true, reason: "", ref: "billing/retries" },
        ],
        "supported",
        [],
        { steps: [], cut: false, listed: true },
      );
      c.end();
    });
    const rows = within(screen.getByRole("list", { name: "Sources" })).getAllByRole("listitem");
    expect(rows.map((r) => r.textContent)).toEqual(["1. billing/retries", "2. billing/invoices"]);
  });

  it("numbers only the markers the preview draws: one in a code span takes none", async () => {
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(field(), "what does billing remember");
    await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
    await user.keyboard("{Enter}");
    await waitFor(() => expect(chats).toHaveLength(1));
    const c = chats[0];
    await act(async () => {
      c.on.onText("`[memory:a@v1]` then [memory:b@v1]");
      c.on.onTextDone?.();
      c.on.onCitations?.(
        [
          { kind: "memory", text: "[memory:a@v1]", verified: true, reason: "", ref: "a" },
          { kind: "memory", text: "[memory:b@v1]", verified: true, reason: "", ref: "b" },
        ],
        "supported",
        [],
        { steps: [], cut: false, listed: true },
      );
      c.end();
    });
    const text = screen.getByTestId("search-answer-text");
    expect(within(text).getByRole("link", { name: "Source 1" })).toHaveTextContent("1");
    expect(text).toHaveTextContent("then 1");
    const rows = within(screen.getByRole("list", { name: "Sources" })).getAllByRole("listitem");
    expect(rows.map((r) => r.textContent)).toEqual(["1. b", "a"]);
  });

  it("links a number only once its row shows: not while the sources are still checked", async () => {
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(field(), "what does billing remember");
    await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
    await user.keyboard("{Enter}");
    await waitFor(() => expect(chats).toHaveLength(1));
    const c = chats[0];
    await act(async () => {
      c.on.onText("Weekly [memory:billing/invoices@v1].");
      c.on.onTextDone?.();
      c.on.onCitations?.(
        [{ kind: "memory", text: "[memory:billing/invoices@v1]", verified: true, reason: "", ref: "billing/invoices" }],
        "supported",
        [],
        { steps: [], cut: false, listed: true },
      );
    });
    // The check holds the answer's slot until its stream ends: no rows, so no link yet.
    const text = screen.getByTestId("search-answer-text");
    expect(text).toHaveTextContent("Weekly 1.");
    expect(within(text).queryByRole("link", { name: "Source 1" })).toBeNull();
    expect(screen.queryByRole("list", { name: "Sources" })).toBeNull();
    await act(async () => c.end());
    expect(await within(text).findByRole("link", { name: "Source 1" })).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Sources" })).toBeInTheDocument();
  });

  it("links no number for citations a server sends before the end that never comes", async () => {
    const user = userEvent.setup();
    render(<HeaderSearch />);
    await user.type(field(), "what does billing remember");
    await screen.findByRole("option", { name: "Ask AI: what does billing remember" });
    await user.keyboard("{Enter}");
    await waitFor(() => expect(chats).toHaveLength(1));
    const c = chats[0];
    // A server a release behind: its citations come with no text_done, before the end.
    await act(async () => {
      c.on.onText("Weekly [memory:billing/invoices@v1].");
      c.on.onCitations?.(
        [{ kind: "memory", text: "[memory:billing/invoices@v1]", verified: true, reason: "", ref: "billing/invoices" }],
        "supported",
        [],
        { steps: [], cut: false, listed: true },
      );
    });
    const text = screen.getByTestId("search-answer-text");
    expect(within(text).queryByRole("link", { name: "Source 1" })).toBeNull();
    // Then the stream breaks: the turn fails, with no rows for a number to link to.
    await act(async () => c.fail(new ApiError({ status: 0, code: "STREAM_ENDED", message: "the stream ended" })));
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    expect(within(screen.getByTestId("search-answer-text")).queryByRole("link", { name: "Source 1" })).toBeNull();
    expect(screen.queryByRole("list", { name: "Sources" })).toBeNull();
  });
});
