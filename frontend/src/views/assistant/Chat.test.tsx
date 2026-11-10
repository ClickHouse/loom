import { act, cleanup, render, screen, waitFor, within } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { axeViolations } from "../../test/axe";
import { useStore } from "../../lib/state";
import { AssistantChat, conversation, MAX_MESSAGE_CHARS } from "./Chat";

// The chat posts OpenAI's chat body to /v1/assistant/chat/completions and reads the
// server-sent chunks the route streams (server/assistant/app/routes.py): `data: {chunk}`
// events, an `error` event for a refusal after the stream began, and `data: [DONE]`.

const enc = new TextEncoder();
// A conversation is kept in the tab under its key: each test's chat has its own.
let keys = 0;
const key = () => `test-${++keys}`;

/** A streamed answer the test feeds event by event; the request's abort errors it, as a
 *  browser's fetch does. */
function sse() {
  let ctl!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({ start: (c) => void (ctl = c) });
  const send = (raw: string) => ctl.enqueue(enc.encode(raw));
  return {
    response: new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
    text: (content: string, id = "msg-1") =>
      send(`data: ${JSON.stringify({ id, choices: [{ delta: { content } }] })}\n\n`),
    raw: send,
    error: (error: object) => send(`data: ${JSON.stringify({ error })}\n\n`),
    done: () => {
      send("data: [DONE]\n\n");
      ctl.close();
    },
    close: () => ctl.close(),
    fail: (e: Error) => ctl.error(e),
    abort: () => ctl.error(new DOMException("The operation was aborted.", "AbortError")),
  };
}

type Call = {
  url: string;
  init: RequestInit;
  body: { stream: boolean; messages: { role: string; content: string }[] };
};
let calls: Call[] = [];
let answers: Array<() => Response | ReturnType<typeof sse>> = [];

beforeEach(() => {
  calls = [];
  answers = [];
  localStorage.setItem("loom_token_v1", "session-token");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url, init, body: JSON.parse(String(init.body)) });
      const next = answers.shift();
      if (!next) throw new Error("no answer queued");
      const a = next();
      if (a instanceof Response) return a;
      init.signal?.addEventListener("abort", a.abort);
      return a.response;
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
});

const refusal = (status: number, detail: object) => () =>
  new Response(JSON.stringify({ detail }), { status, headers: { "Content-Type": "application/json" } });

async function ask(question: string) {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Question"), question);
  await user.click(screen.getByRole("button", { name: "Ask" }));
  return user;
}

/** The stream's citations event, with the answer's support. */
const citationsEvent = (citations: object[], support: string) =>
  `data: ${JSON.stringify({ object: "loom.citations", id: "msg-1", choices: [], citations, support })}\n\n`;

const lastAnswer = () => {
  // The conversation's own items: an answer's lists hold items too.
  const turns = screen.getByRole("list", { name: "Conversation" }).children;
  return turns[turns.length - 1] as HTMLElement;
};

describe("AssistantChat", () => {
  it("streams the answer in as it arrives, with the session's token and the page's namespace", async () => {
    const s = sse();
    answers.push(() => s);
    render(<AssistantChat conversationKey={key()} namespace="team" blocked={null} />);
    await ask("What do we bill?");

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("/v1/assistant/chat/completions");
    const headers = calls[0].init.headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer session-token");
    expect(headers["X-Loom-Namespace"]).toBe("team");
    expect(calls[0].body).toEqual({ stream: true, messages: [{ role: "user", content: "What do we bill?" }] });
    expect(screen.getByText("Answering…")).toBeInTheDocument();

    await act(async () => s.text("Invoices go "));
    await waitFor(() => expect(lastAnswer()).toHaveTextContent("Invoices go"));
    expect(lastAnswer()).not.toHaveTextContent("Fridays");
    await act(async () => s.text("out on Fridays."));
    await waitFor(() => expect(lastAnswer()).toHaveTextContent("Invoices go out on Fridays."));
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();

    await act(async () => s.done());
    await waitFor(() => expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument());
    expect(screen.getByRole("status")).toHaveTextContent("The answer is complete.");
  });

  it("sends the whole conversation each turn", async () => {
    const first = sse();
    const second = sse();
    answers.push(
      () => first,
      () => second,
    );
    render(<AssistantChat conversationKey={key()} namespace="team" blocked={null} />);
    await ask("Who owns billing?");
    await act(async () => {
      first.text("Ada does.");
      first.done();
    });
    await waitFor(() => expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument());
    await ask("Since when?");
    expect(calls[1].body.messages).toEqual([
      { role: "user", content: "Who owns billing?" },
      { role: "assistant", content: "Ada does." },
      { role: "user", content: "Since when?" },
    ]);
  });

  it("shows a refusal before the stream, keeps the question to ask again, and leaves it out of the next turn", async () => {
    answers.push(
      refusal(429, {
        code: "RATE_LIMITED",
        message: "the assistant's share of 'team''s daily model cost cap is spent",
        retry_after_s: 3600,
      }),
    );
    render(<AssistantChat conversationKey={key()} namespace="team" blocked={null} />);
    await ask("Who owns billing?");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "The assistant cannot answer in this namespace right now: the assistant's share of 'team''s daily model cost cap is spent. Try again in 3600 s.",
    );
    expect(screen.getByLabelText("Question")).toHaveValue("Who owns billing?");

    const s = sse();
    answers.push(() => s);
    await userEvent.setup().click(screen.getByRole("button", { name: "Ask" }));
    expect(calls[1].body.messages).toEqual([{ role: "user", content: "Who owns billing?" }]);
  });

  it.each([
    ["AUTH_INSUFFICIENT", 403, "The assistant refused this request: the caller cannot read 'team' in 'dev'."],
    ["OVERLOADED", 503, "The assistant is busy: the caller cannot read 'team' in 'dev'."],
    [
      "MODEL_CALLS_DISABLED",
      403,
      "Model calls are switched off for this organization: the caller cannot read 'team' in 'dev'.",
    ],
  ])("shows a %s refusal in the chat", async (code, status, shown) => {
    answers.push(refusal(status, { code, message: "the caller cannot read 'team' in 'dev'" }));
    render(<AssistantChat conversationKey={key()} namespace="team" blocked={null} />);
    await ask("Who owns billing?");
    expect(await screen.findByRole("alert")).toHaveTextContent(shown);
  });

  it("shows an error event after part of the answer, and keeps that part", async () => {
    const s = sse();
    answers.push(() => s);
    render(<AssistantChat conversationKey={key()} namespace="team" blocked={null} />);
    await ask("Who owns billing?");
    await act(async () => {
      s.text("Ada ");
      // The route's one error inside a stream, and nothing after it.
      s.error({ code: "ASSISTANT_UPSTREAM_FAILED", message: "LibreChat ended the answer with an error" });
      s.close();
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The assistant could not finish this answer: LibreChat ended the answer with an error.",
    );
    expect(lastAnswer()).toHaveTextContent("Ada");
    // An error inside the stream is the chat's, not the page's: the composer stays.
    expect(screen.getByLabelText("Question")).toBeInTheDocument();
  });

  it("says so when the stream stops without its end", async () => {
    const s = sse();
    answers.push(() => s);
    render(<AssistantChat conversationKey={key()} namespace="team" blocked={null} />);
    await ask("Who owns billing?");
    await act(async () => {
      s.text("Ada");
      s.close();
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The answer was cut off: the answer stopped before it was complete.",
    );
    expect(lastAnswer()).toHaveTextContent("Ada");
  });

  it("says the answer was cut off when the connection drops mid-answer", async () => {
    const s = sse();
    answers.push(() => s);
    render(<AssistantChat conversationKey={key()} namespace="team" blocked={null} />);
    await ask("Who owns billing?");
    await act(async () => {
      s.text("Ada");
      // As a browser's fetch rejects a reset connection's read.
      s.fail(new TypeError("network error"));
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("The answer was cut off");
    expect(lastAnswer()).toHaveTextContent("Ada");
  });

  it("keeps the conversation when the chat goes away and comes back, as after opening a document", async () => {
    const s = sse();
    answers.push(() => s);
    const k = key();
    const { unmount } = render(<AssistantChat conversationKey={k} namespace="team" blocked={null} />);
    await ask("Who owns billing?");
    await act(async () => {
      s.text("Ada.");
      s.done();
    });
    await screen.findByText("Ada.");
    unmount();
    render(<AssistantChat conversationKey={k} namespace="team" blocked={null} />);
    expect(screen.getByText("Who owns billing?")).toBeInTheDocument();
    expect(screen.getByText("Ada.")).toBeInTheDocument();
    cleanup();
    // Another key (another subject, namespace or scope) starts empty.
    render(<AssistantChat conversationKey={key()} namespace="team" blocked={null} />);
    expect(screen.queryByText("Who owns billing?")).not.toBeInTheDocument();
  });

  it("shows an answer cut off by leaving the chat as stopped when it comes back", async () => {
    const s = sse();
    answers.push(() => s);
    const k = key();
    const { unmount } = render(<AssistantChat conversationKey={k} namespace="team" blocked={null} />);
    await ask("Who owns billing?");
    await act(async () => s.text("Ada"));
    await screen.findByText("Ada");
    unmount();
    render(<AssistantChat conversationKey={k} namespace="team" blocked={null} />);
    expect(screen.getByText("Ada")).toBeInTheDocument();
    expect(screen.getByText("Stopped.")).toBeInTheDocument();
    // Nothing is still answering: the next question can be asked.
    expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument();
    answers.push(() => {
      const next = sse();
      queueMicrotask(() => {
        next.text("Grace.");
        next.done();
      });
      return next;
    });
    await ask("And payroll?");
    expect(await screen.findByText("Grace.")).toBeInTheDocument();
  });

  it("asks nothing with a question longer than the route accepts, and says why", async () => {
    render(<AssistantChat conversationKey={key()} namespace="team" blocked={null} />);
    const user = userEvent.setup();
    const field = screen.getByLabelText("Question");
    await user.click(field);
    await user.paste("x".repeat(MAX_MESSAGE_CHARS + 1));
    await user.keyboard("{Enter}");
    expect(calls).toHaveLength(0);
    // Said on the field itself, not only in the Ask button's tooltip.
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(field).toHaveAccessibleDescription(expect.stringContaining("A question is at most 20,000 characters."));
  });

  it("counts a question's length as it is sent, trimmed", async () => {
    answers.push(() => {
      const s = sse();
      queueMicrotask(() => s.done());
      return s;
    });
    render(<AssistantChat conversationKey={key()} namespace="team" blocked={null} />);
    const user = userEvent.setup();
    const field = screen.getByLabelText("Question");
    await user.click(field);
    await user.paste(`${"x".repeat(MAX_MESSAGE_CHARS)}   `);
    expect(field).not.toHaveAttribute("aria-invalid");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(calls).toHaveLength(1);
    expect(calls[0].body.messages.at(-1)?.content).toHaveLength(MAX_MESSAGE_CHARS);
  });

  it("sends the newest messages the route accepts, each cut to its length", () => {
    const turns = Array.from({ length: 30 }, (_, i) => ({
      id: i + 1,
      question: `q${i}`,
      answer: i === 29 ? "x".repeat(25_000) : `a${i}`,
      state: "done" as const,
    }));
    const messages = conversation(turns, "now");
    expect(messages).toHaveLength(51);
    expect(messages[0]).toEqual({ role: "user", content: "q5" });
    expect(messages[49].content).toHaveLength(20_000);
    expect(messages[50]).toEqual({ role: "user", content: "now" });
  });

  it("Stop aborts the request and keeps what came in", async () => {
    const s = sse();
    answers.push(() => s);
    render(<AssistantChat conversationKey={key()} namespace="team" blocked={null} />);
    const user = await ask("Who owns billing?");
    await act(async () => s.text("Ada owns"));
    await waitFor(() => expect(lastAnswer()).toHaveTextContent("Ada owns"));
    const signal = calls[0].init.signal as AbortSignal;
    expect(signal.aborted).toBe(false);
    await user.click(screen.getByRole("button", { name: "Stop" }));
    expect(signal.aborted).toBe(true);
    await waitFor(() => expect(lastAnswer()).toHaveTextContent("Stopped."));
    expect(lastAnswer()).toHaveTextContent("Ada owns");
    expect(screen.getByLabelText("Question")).toHaveFocus();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("aborts the answer when the chat goes away", async () => {
    const s = sse();
    answers.push(() => s);
    const view = render(<AssistantChat conversationKey={key()} namespace="team" blocked={null} />);
    await ask("Who owns billing?");
    view.unmount();
    expect((calls[0].init.signal as AbortSignal).aborted).toBe(true);
  });

  it("shows ASSISTANT_UNAVAILABLE as the chat's state, not a crash, and offers a later try", async () => {
    answers.push(
      refusal(503, {
        code: "ASSISTANT_UNAVAILABLE",
        message: "the assistant is not set up on this replica yet: /v1/health names why",
      }),
    );
    render(<AssistantChat conversationKey={key()} namespace="team" blocked={null} />);
    const user = await ask("Who owns billing?");
    const state = await screen.findByTestId("assistant-chat-unavailable");
    expect(state).toHaveTextContent("The assistant is not available on this deployment.");
    expect(state).toHaveTextContent("the assistant is not set up on this replica yet");
    expect(screen.queryByLabelText("Question")).not.toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Conversation" })).not.toBeInTheDocument();
    // The focused form went: the focus is on the state's own button, then back on the field.
    expect(screen.getByRole("button", { name: "Try again" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(screen.getByLabelText("Question")).toHaveValue("Who owns billing?");
    expect(screen.getByLabelText("Question")).toHaveFocus();
  });

  it("renders Markdown emphasis, lists, tables and code, and HTML only as text", async () => {
    const s = sse();
    answers.push(() => s);
    const { container } = render(<AssistantChat conversationKey={key()} namespace="team" blocked={null} />);
    await ask("Show me");
    const answer = [
      "**Billing** is *weekly* and `snake_case_key` stays.",
      "",
      "- first",
      "- second",
      "",
      "1. one",
      "2. two",
      "",
      "| Key | Owner |",
      "| --- | --- |",
      "| billing | ada |",
      "",
      "Here it is, right under a line:",
      "| Team | Lead |",
      "| --- | --- |",
      "| ops | grace |",
      "",
      "```sql",
      "SELECT 1",
      "```",
      "",
      '<script>window.pwned = 1</script> <img src="https://evil.example/x.png" onerror="alert(1)">',
      "![chart](https://evil.example/rows.png) [click](https://evil.example) https://evil.example/raw",
    ].join("\n");
    await act(async () => {
      s.text(answer);
      s.done();
    });
    const turn = await waitFor(() => {
      const t = lastAnswer();
      expect(t).toHaveTextContent("SELECT 1");
      return t;
    });
    expect(within(turn).getByText("Billing").tagName).toBe("STRONG");
    expect(within(turn).getByText("weekly").tagName).toBe("EM");
    expect(within(turn).getByText("snake_case_key").tagName).toBe("CODE");
    expect(within(turn).getByText("first").closest("ul")).not.toBeNull();
    expect(within(turn).getByText("two").closest("ol")).not.toBeNull();
    const [first, second] = within(turn).getAllByRole("table");
    expect(first).toHaveTextContent("ada");
    expect(second).toHaveTextContent("grace");
    expect(within(turn).getByText("Here it is, right under a line:")).toBeInTheDocument();
    expect(within(turn).getByText("SELECT 1").tagName).toBe("PRE");
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("a")).toBeNull();
    expect(turn).toHaveTextContent("<script>window.pwned = 1</script>");
    expect(turn).toHaveTextContent('<img src="https://evil.example/x.png" onerror="alert(1)">');
    expect(turn).toHaveTextContent("![chart](https://evil.example/rows.png)");
    expect((window as unknown as { pwned?: number }).pwned).toBeUndefined();
  });

  it("shows the answer's citations as written, and the checked ones in its Sources", async () => {
    const s = sse();
    answers.push(() => s);
    const { container } = render(<AssistantChat conversationKey={key()} namespace="team" blocked={null} />);
    await ask("Cite it");
    const cited = [
      { kind: "memory", text: "[memory:billing/invoices@v3]", verified: true, reason: "", ref: "billing/invoices" },
      { kind: "doc", text: "[doc:spec.pdf § Billing]", verified: true, reason: "", ref: "spec.pdf" },
      {
        kind: "code",
        text: "[code:server/__init__.py:12]",
        verified: false,
        reason: "code citations are not checked",
        ref: "server/__init__.py",
      },
    ];
    await act(async () => {
      s.text("Fridays [memory:billing/invoices@v3], per [doc:spec.pdf § Billing] and [code:server/__init__.py:12].");
      s.raw(citationsEvent(cited, "supported"));
      s.done();
    });
    const sources = await screen.findByRole("list", { name: "Sources" });
    // The answer's own text shows each citation as written; nothing in it is a link.
    expect(lastAnswer()).toHaveTextContent("[code:server/__init__.py:12]");
    expect(container.querySelector("a")).toBeNull();
    expect(container.querySelector("strong")).toBeNull(); // `__init__` is not emphasis
    expect(within(sources).getAllByRole("listitem")).toHaveLength(3);
    expect(within(sources).getByText(/code citations are not checked/)).toBeInTheDocument();
    expect(within(sources).queryByRole("button", { name: /code:/ })).toBeNull();
    // Two of its three sources were verified (code is never checked): the label counts them.
    expect(screen.getByTestId("assistant-support")).toHaveTextContent(
      "Checked against what Loom holds: 2 of 3 sources verified",
    );
    const memory = within(sources).getByRole("button", { name: "memory:billing/invoices@v3" });
    await userEvent.setup().click(memory);
    expect(useStore.getState().inspectedMemoryKey).toBe("billing/invoices");
    // Read whichever writer filed it, not as the dashboard's own agent.
    expect(useStore.getState().inspectedAgent).toBe("*");
    await userEvent.setup().click(within(sources).getByRole("button", { name: "doc:spec.pdf § Billing" }));
    expect(useStore.getState().pendingDocSourceUri).toBe("spec.pdf");
  });

  it("marks an answer whose sources do not check out as unconfirmed", async () => {
    const s = sse();
    answers.push(() => s);
    const { container } = render(<AssistantChat conversationKey={key()} namespace="team" blocked={null} />);
    await ask("Cite it");
    const cited = [
      {
        kind: "memory",
        text: "[memory:billing/invoices@v2]",
        verified: false,
        reason: "the current version is 3",
        ref: "billing/invoices",
      },
      {
        kind: "sql",
        text: "```sql\nSELECT count() FROM loom.memory\nWHERE namespace = 'team'\n```",
        verified: false,
        reason: "no insights.query in this answer ran it",
        ref: "",
      },
    ];
    await act(async () => {
      s.text("Fridays [memory:billing/invoices@v2].");
      s.raw(citationsEvent(cited, "unsupported"));
      s.done();
    });
    expect(await screen.findByText("Couldn't confirm: none of its sources were verified")).toBeInTheDocument();
    expect(screen.getByText(/the current version is 3/)).toBeInTheDocument();
    // A statement is named by its first line, so one query of an answer reads apart from another.
    expect(screen.getByText("SQL query: SELECT count() FROM loom.memory")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /memory:/ })).toBeNull();
    expect(await axeViolations(container)).toEqual([]);
  });

  it.each([
    // A greeting or an abstention makes no claim: no label, no sources.
    ["none", "Hello! What would you like to know?", null],
    ["unsupported", "Deploys freeze on Fridays.", "Couldn't confirm: it gives no sources"],
    ["unchecked", "This namespace holds 1,204 memories.", "Not checked: it gives no sources Loom can check"],
    // A value this dashboard does not know reads as unchecked.
    ["a-later-state", "Deploys freeze on Fridays.", "Not checked: it gives no sources Loom can check"],
  ])("labels an uncited answer whose support is %s", async (support, text, label) => {
    const s = sse();
    answers.push(() => s);
    const { container } = render(<AssistantChat conversationKey={key()} namespace="team" blocked={null} />);
    await ask("Hi");
    await act(async () => {
      s.text(text);
      s.raw(citationsEvent([], support));
      s.done();
    });
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("The answer is complete."));
    if (label === null) {
      expect(screen.queryByTestId("assistant-sources")).toBeNull();
      expect(screen.getByRole("status")).toHaveTextContent(/^The answer is complete\.$/);
    } else {
      expect(screen.getByTestId("assistant-support")).toHaveTextContent(label);
      // The label is said with the end of the answer.
      expect(screen.getByRole("status")).toHaveTextContent(`The answer is complete. ${label}.`);
    }
    expect(await axeViolations(container)).toEqual([]);
  });

  it("names each answer's Why button apart and ties it to its label", async () => {
    const first = sse();
    const second = sse();
    answers.push(
      () => first,
      () => second,
    );
    render(<AssistantChat conversationKey={key()} namespace="team" blocked={null} />);
    await ask("One");
    await act(async () => {
      first.text("Fridays.");
      first.raw(citationsEvent([], "unsupported"));
      first.done();
    });
    await ask("Two");
    await act(async () => {
      second.text("Mondays.");
      second.raw(citationsEvent([], "unchecked"));
      second.done();
    });
    const one = await screen.findByRole("button", { name: "Why this label, answer 1" });
    const two = screen.getByRole("button", { name: "Why this label, answer 2" });
    expect(one).toHaveAccessibleDescription("Couldn't confirm: it gives no sources");
    expect(two).toHaveAccessibleDescription("Not checked: it gives no sources Loom can check");
    await userEvent.setup().click(one);
    expect(await screen.findByText(/cites none of them/)).toBeInTheDocument();
  });

  it("asks nothing while blocked, and says why", async () => {
    render(
      <AssistantChat conversationKey={key()} namespace="team" blocked="Your role on this namespace is not known." />,
    );
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Question"), "Who?{Enter}");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(calls).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Ask" })).toHaveAccessibleDescription(
      "Your role on this namespace is not known.",
    );
  });

  it("Enter asks and Shift+Enter adds a line", async () => {
    const s = sse();
    answers.push(() => s);
    render(<AssistantChat conversationKey={key()} namespace="team" blocked={null} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Question"), "line one{Shift>}{Enter}{/Shift}line two{Enter}");
    expect(calls[0].body.messages).toEqual([{ role: "user", content: "line one\nline two" }]);
  });

  it("has no axe violations with a conversation, an answer streaming and a refusal shown", async () => {
    const s = sse();
    answers.push(refusal(503, { code: "OVERLOADED", message: "busy", retry_after_s: 5 }), () => s);
    const { container } = render(<AssistantChat conversationKey={key()} namespace="team" blocked={null} />);
    await ask("First?");
    await screen.findByRole("alert");
    await userEvent.setup().click(screen.getByRole("button", { name: "Ask" }));
    await act(async () => s.text("| a | b |\n| - | - |\n| 1 | 2 |\n\n**done**"));
    await waitFor(() => expect(lastAnswer()).toHaveTextContent("done"));
    expect(await axeViolations(container)).toEqual([]);
  });
});
