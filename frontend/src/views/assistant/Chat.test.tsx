import { act, cleanup, render, screen, waitFor, within } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { axeViolations } from "../../test/axe";
import { useStore } from "../../lib/state";
import { AssistantChat } from "./Chat";
import { conversation, MAX_MESSAGE_CHARS, resetChat, useChat } from "./chatStore";
import { contextLength, pageContext } from "./pageContext";
import { useDock } from "../../lib/assistantDock";

// The chat posts OpenAI's chat body to /v1/assistant/chat/completions and reads the
// server-sent chunks the route streams (server/assistant/app/routes.py): `data: {chunk}`
// events, an `error` event for a refusal after the stream began, and `data: [DONE]`.

const enc = new TextEncoder();

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
  // Each test starts on a new conversation with an empty field.
  resetChat(null);
  useDock.setState({ draft: "", working: false, unread: false, open: true, item: null, pageOff: false });
  useStore.setState({ view: "memories" });
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

// Each question carries the page it is asked from, as a leading line (./pageContext.ts).
const ON_PAGE = "Context: Memories page\n\n";

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
    render(
      <>
        <AssistantChat namespace="team" blocked={null} />
        <p id="loom-assistant-status" role="status" />
      </>,
    );
    await ask("What do we bill?");

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("/v1/assistant/chat/completions");
    const headers = calls[0].init.headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer session-token");
    expect(headers["X-Loom-Namespace"]).toBe("team");
    expect(calls[0].body).toEqual({
      stream: true,
      messages: [{ role: "user", content: `${ON_PAGE}What do we bill?` }],
    });
    expect(screen.getByText("Answering…")).toBeInTheDocument();

    await act(async () => s.text("Invoices go "));
    await waitFor(() => expect(lastAnswer()).toHaveTextContent("Invoices go"));
    expect(lastAnswer()).not.toHaveTextContent("Fridays");
    await act(async () => s.text("out on Fridays."));
    await waitFor(() => expect(lastAnswer()).toHaveTextContent("Invoices go out on Fridays."));
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();

    await act(async () => s.done());
    await waitFor(() => expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument());
    // Said in the assistant's own live region, which the shell renders.
    await waitFor(() =>
      expect(document.getElementById("loom-assistant-status")).toHaveTextContent("The answer is complete."),
    );
  });

  it("sends the whole conversation each turn", async () => {
    const first = sse();
    const second = sse();
    answers.push(
      () => first,
      () => second,
    );
    render(<AssistantChat namespace="team" blocked={null} />);
    await ask("Who owns billing?");
    await act(async () => {
      first.text("Ada does.");
      first.done();
    });
    await waitFor(() => expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument());
    await ask("Since when?");
    expect(calls[1].body.messages).toEqual([
      { role: "user", content: `${ON_PAGE}Who owns billing?` },
      { role: "assistant", content: "Ada does." },
      { role: "user", content: `${ON_PAGE}Since when?` },
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
    render(<AssistantChat namespace="team" blocked={null} />);
    await ask("Who owns billing?");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Over a limit for now: the assistant's share of 'team''s daily model cost cap is spent. Try again in 3600 s.",
    );
    expect(screen.getByLabelText("Question")).toHaveValue("Who owns billing?");

    const s = sse();
    answers.push(() => s);
    await userEvent.setup().click(screen.getByRole("button", { name: "Ask" }));
    expect(calls[1].body.messages).toEqual([{ role: "user", content: `${ON_PAGE}Who owns billing?` }]);
  });

  it.each([
    ["AUTH_INSUFFICIENT", 403, "Your role doesn't allow this: the caller cannot read 'team' in 'dev'."],
    ["OVERLOADED", 503, "Loom is busy right now: the caller cannot read 'team' in 'dev'."],
  ])("shows a %s refusal in the chat", async (code, status, shown) => {
    answers.push(refusal(status, { code, message: "the caller cannot read 'team' in 'dev'" }));
    render(<AssistantChat namespace="team" blocked={null} />);
    await ask("Who owns billing?");
    expect(await screen.findByRole("alert")).toHaveTextContent(shown);
  });

  it("says a refusal about the token as the token's, not as the person's role", async () => {
    answers.push(
      refusal(403, {
        code: "AUTH_INSUFFICIENT",
        message: "a token whose reads are narrower than its namespace cannot ask the assistant",
        reason: "narrowed",
      }),
    );
    render(<AssistantChat namespace="team" blocked={null} />);
    await ask("Who owns billing?");
    // The panel says why in its own words for the token (Dock.tsx): no alert about a role.
    await waitFor(() =>
      expect(useChat.getState().hint).toEqual({ kind: "narrowed", scope: "less than the namespace" }),
    );
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByLabelText("Question")).toHaveValue("Who owns billing?");
  });

  it("says a refusal with no lead of its own once, with one retry wait", async () => {
    answers.push(
      refusal(503, {
        code: "MODEL_CALL_DEFERRED",
        message: "the model gateway is queueing calls",
        category: "capacity",
        retryable: true,
        retry_after_s: 3,
      }),
    );
    render(<AssistantChat namespace="team" blocked={null} />);
    await ask("Who owns billing?");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Loom is busy: the model gateway is queueing calls. Try again in 3 s.");
    expect(alert.textContent?.match(/Try again/g)).toHaveLength(1);
  });

  it("says Loom could not check access, not that the assistant refused, when the grant read failed", async () => {
    answers.push(
      refusal(403, {
        code: "AUTH_INSUFFICIENT",
        message: "the grant store did not answer",
        category: "dependency",
        retryable: true,
      }),
    );
    render(<AssistantChat namespace="team" blocked={null} />);
    await ask("Who owns billing?");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Loom couldn't check your access just now: the grant store did not answer. Try again in a moment.",
    );
  });

  it("words a server error before the stream as a read: nothing may have been saved", async () => {
    answers.push(
      refusal(500, {
        code: "INTERNAL_ERROR",
        message: "internal error; request_id=r5",
        category: "internal",
        retryable: false,
      }),
    );
    render(<AssistantChat namespace="team" blocked={null} />);
    await ask("Who owns billing?");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Loom hit an internal error.");
    expect(alert).not.toHaveTextContent(/may have been saved/);
  });

  it("shows an error event after part of the answer, and keeps that part", async () => {
    const s = sse();
    answers.push(() => s);
    render(<AssistantChat namespace="team" blocked={null} />);
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
    render(<AssistantChat namespace="team" blocked={null} />);
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
    render(<AssistantChat namespace="team" blocked={null} />);
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
    const { unmount } = render(<AssistantChat namespace="team" blocked={null} />);
    await ask("Who owns billing?");
    await act(async () => {
      s.text("Ada.");
      s.done();
    });
    await screen.findByText("Ada.");
    unmount();
    render(<AssistantChat namespace="team" blocked={null} />);
    expect(screen.getByText("Who owns billing?")).toBeInTheDocument();
    expect(screen.getByText("Ada.")).toBeInTheDocument();
    cleanup();
    // Another person (or token scope) starts empty.
    resetChat({ subject: "bob", org: "acme", scope: "-", user: "-" });
    render(<AssistantChat namespace="team" blocked={null} />);
    expect(screen.queryByText("Who owns billing?")).not.toBeInTheDocument();
  });

  it("keeps answering while the chat is away, and shows the whole answer when it comes back", async () => {
    const s = sse();
    answers.push(() => s);
    const { unmount } = render(<AssistantChat namespace="team" blocked={null} />);
    await ask("Who owns billing?");
    await act(async () => s.text("Ada"));
    await screen.findByText("Ada");
    unmount();
    expect((calls[0].init.signal as AbortSignal).aborted).toBe(false);
    await act(async () => {
      s.text(" owns it.");
      s.done();
    });
    render(<AssistantChat namespace="team" blocked={null} />);
    expect(screen.getByText("Ada owns it.")).toBeInTheDocument();
    expect(screen.queryByText("Stopped.")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument();
  });

  it("asks nothing with a question longer than the route accepts, and says why", async () => {
    render(<AssistantChat namespace="team" blocked={null} />);
    const user = userEvent.setup();
    // Without the page's line the whole length is the question's.
    await user.click(screen.getByRole("button", { name: "Don't include this page" }));
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
    render(<AssistantChat namespace="team" blocked={null} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Don't include this page" }));
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
      id: `t${i + 1}`,
      question: `q${i}`,
      answer: i === 29 ? "x".repeat(25_000) : `a${i}`,
      state: "done" as const,
      at: i,
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
    render(<AssistantChat namespace="team" blocked={null} />);
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

  it("takes an ASSISTANT_UNAVAILABLE refusal out of the conversation and keeps the reason and the question", async () => {
    answers.push(
      refusal(503, {
        code: "ASSISTANT_UNAVAILABLE",
        message: "the assistant is not set up in this web process yet",
        reason: "starting",
      }),
    );
    render(<AssistantChat namespace="team" blocked={null} />);
    await ask("Who owns billing?");
    await waitFor(() => expect(useChat.getState().hint).toEqual({ kind: "starting" }));
    expect(useChat.getState().refused).toEqual({ question: "Who owns billing?" });
    expect(screen.queryByRole("list", { name: "Conversation" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Question")).toHaveValue("Who owns billing?");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("renders Markdown emphasis, lists, tables and code, and HTML only as text", async () => {
    const s = sse();
    answers.push(() => s);
    const { container } = render(<AssistantChat namespace="team" blocked={null} />);
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
    const { container } = render(<AssistantChat namespace="team" blocked={null} />);
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
    // Two of its three sources were found (code is never checked): the label leads with that.
    expect(screen.getByTestId("assistant-support")).toHaveTextContent("Partly confirmed: 2 of 3 sources found in Loom");
    const rows = within(sources).getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent(/^Found in Loom · /);
    expect(rows[2]).toHaveTextContent(/^Not confirmed · .*\(code citations are not checked\)$/);
    expect(sources.parentElement).not.toHaveTextContent(/verified/i);
    const memory = within(sources).getByRole("button", { name: "memory:billing/invoices@v3" });
    await userEvent.setup().click(memory);
    expect(useStore.getState().inspectedMemoryKey).toBe("billing/invoices");
    // Read whichever writer filed it, not as the dashboard's own agent.
    expect(useStore.getState().inspectedAgent).toBe("*");
    await userEvent.setup().click(within(sources).getByRole("button", { name: "doc:spec.pdf § Billing" }));
    expect(useStore.getState().pendingDocSourceUri).toBe("spec.pdf");
  });

  it("opens a verified help citation's page on the Help view at its section, and links nothing in the answer", async () => {
    const s = sse();
    answers.push(() => s);
    const { container } = render(<AssistantChat namespace="team" blocked={null} />);
    await ask("How do I revoke a token?");
    const text = "[help:guides/tokens-and-grants.md § Tokens and grants > Revoke and rotate]";
    const cited = [
      {
        kind: "help",
        text,
        verified: true,
        reason: "",
        ref: "guides/tokens-and-grants.md",
        anchor: "revoke-and-rotate",
      },
      {
        kind: "help",
        text: "[help:guides/absent.md]",
        verified: false,
        reason: "no such page in this build's documentation",
        ref: "guides/absent.md",
        anchor: "",
      },
    ];
    await act(async () => {
      s.text(`Revoke it with loom revoke ${text} and see [help:guides/absent.md].`);
      s.raw(citationsEvent(cited, "supported"));
      s.done();
    });
    const sources = await screen.findByRole("list", { name: "Sources" });
    expect(lastAnswer()).toHaveTextContent(text);
    // The answer's text draws no link; the Sources entry opens the page in the dashboard.
    expect(container.querySelector("a")).toBeNull();
    expect(within(sources).getByText(/no such page in this build/)).toBeInTheDocument();
    await userEvent.setup().click(within(sources).getByRole("button", { name: text.slice(1, -1) }));
    expect(useStore.getState().view).toBe("help");
    expect(window.location.search).toContain("page=guides%2Ftokens-and-grants.md");
    expect(window.location.hash).toBe("#revoke-and-rotate");
  });

  it("names the op an answer may have invented in its label", async () => {
    const s = sse();
    answers.push(() => s);
    render(
      <>
        <AssistantChat namespace="team" blocked={null} />
        <p id="loom-assistant-status" role="status" />
      </>,
    );
    await ask("How do I pin a memory?");
    const cited = [{ kind: "memory", text: "[memory:a/b/c@v3]", verified: true, reason: "", ref: "a/b/c", anchor: "" }];
    await act(async () => {
      s.text("Call `memory.frobnicate` [memory:a/b/c@v3].");
      s.raw(
        `data: ${JSON.stringify({ object: "loom.citations", id: "msg-1", choices: [], citations: cited, support: "unsupported", unknown_names: ["memory.frobnicate"] })}\n\n`,
      );
      s.done();
    });
    const label = "Couldn't confirm: it names memory.frobnicate, which this Loom neither serves nor documents";
    expect(await screen.findByTestId("assistant-support")).toHaveTextContent(label);
    // Said with the end of the answer, too.
    await waitFor(() =>
      expect(document.getElementById("loom-assistant-status")).toHaveTextContent(`The answer is complete. ${label}.`),
    );
  });

  it("marks an answer whose sources do not check out as unconfirmed", async () => {
    const s = sse();
    answers.push(() => s);
    const { container } = render(<AssistantChat namespace="team" blocked={null} />);
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
    expect(await screen.findByText("Couldn't confirm: 0 of 2 sources found in Loom")).toBeInTheDocument();
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
    ["unchecked", "This namespace holds 1,204 memories.", "Not checked: it cites nothing Loom holds"],
    // A value this dashboard does not know reads as unchecked.
    ["a-later-state", "Deploys freeze on Fridays.", "Not checked: it cites nothing Loom holds"],
  ])("labels an uncited answer whose support is %s", async (support, text, label) => {
    const s = sse();
    answers.push(() => s);
    const { container } = render(
      <>
        <AssistantChat namespace="team" blocked={null} />
        <p id="loom-assistant-status" role="status" />
      </>,
    );
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
    render(<AssistantChat namespace="team" blocked={null} />);
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
    expect(two).toHaveAccessibleDescription("Not checked: it cites nothing Loom holds");
    await userEvent.setup().click(one);
    expect(await screen.findByText(/gives no source for what it says/)).toBeInTheDocument();
    await userEvent.setup().click(two);
    expect(
      await screen.findByText(
        "This answer cites nothing: what it read has no rows an answer can cite, a read failed or was refused, or its reads could not be checked.",
      ),
    ).toBeInTheDocument();
  });

  it("asks nothing while blocked, and says why", async () => {
    render(<AssistantChat namespace="team" blocked="Needs viewer on team. An admin of it can grant that." />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Question"), "Who?{Enter}");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(calls).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Ask" })).toHaveAccessibleDescription(
      "Needs viewer on team. An admin of it can grant that.",
    );
  });

  it.each([
    ["the page alone", null],
    [
      "an item",
      {
        view: "memories" as const,
        org: "acme",
        namespace: "team",
        chip: "key user/pref/lang",
        line: 'memory "user/pref/lang"',
      },
    ],
  ])("with the chip on (%s), a question fills what the route accepts after its line", async (_, item) => {
    useDock.setState({ item });
    const line = item ? `Context: Memories page, ${item.line}` : "Context: Memories page";
    const limit = MAX_MESSAGE_CHARS - line.length - 2;
    answers.push(() => {
      const s = sse();
      queueMicrotask(() => s.done());
      return s;
    });
    render(<AssistantChat namespace="team" blocked={null} />);
    const user = userEvent.setup();
    const field = screen.getByLabelText("Question");
    await user.click(field);
    await user.paste("x".repeat(limit + 1));
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(field).toHaveAccessibleDescription(
      expect.stringContaining(`A question is at most ${limit.toLocaleString("en-US")} characters.`),
    );
    await user.keyboard("{Enter}");
    expect(calls).toHaveLength(0);
    await user.keyboard("{Backspace}");
    expect(field).not.toHaveAttribute("aria-invalid");
    await user.keyboard("{Enter}");
    expect(calls).toHaveLength(1);
    const sentText = calls[0].body.messages.at(-1)?.content ?? "";
    expect(sentText).toBe(`${line}\n\n${"x".repeat(limit)}`);
    expect(sentText).toHaveLength(MAX_MESSAGE_CHARS);
  });

  it("Enter asks and Shift+Enter adds a line", async () => {
    const s = sse();
    answers.push(() => s);
    render(<AssistantChat namespace="team" blocked={null} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Question"), "line one{Shift>}{Enter}{/Shift}line two{Enter}");
    expect(calls[0].body.messages).toEqual([{ role: "user", content: `${ON_PAGE}line one\nline two` }]);
  });

  it("has no axe violations with a conversation, an answer streaming and a refusal shown", async () => {
    const s = sse();
    answers.push(refusal(503, { code: "OVERLOADED", message: "busy", retry_after_s: 5 }), () => s);
    const { container } = render(<AssistantChat namespace="team" blocked={null} />);
    await ask("First?");
    await screen.findByRole("alert");
    await userEvent.setup().click(screen.getByRole("button", { name: "Ask" }));
    await act(async () => s.text("| a | b |\n| - | - |\n| 1 | 2 |\n\n**done**"));
    await waitFor(() => expect(lastAnswer()).toHaveTextContent("done"));
    expect(await axeViolations(container)).toEqual([]);
  });
});

describe("asking again", () => {
  /** One answered turn: its question, its answer text and the event before [DONE]. */
  async function answered(question: string, text: string, extra = "") {
    const s = sse();
    answers.push(() => s);
    await ask(question);
    await act(async () => {
      s.text(text);
      if (extra) s.raw(extra);
      s.done();
    });
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
  }
  const textDone = `data: ${JSON.stringify({ object: "loom.text_done", id: "msg-1", choices: [] })}\n\n`;

  it("copies a finished answer as written, citations included", async () => {
    render(<AssistantChat namespace="team" blocked={null} />);
    await answered("When?", "On **Fridays** [memory:billing/invoices].", citationsEvent([], "unchecked"));
    // userEvent's own clipboard stands in for the browser's.
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Copy answer 1" }));
    expect(await navigator.clipboard.readText()).toBe("On **Fridays** [memory:billing/invoices].");
    expect(screen.getByRole("button", { name: "Copy answer 1" })).toHaveTextContent("Copied");
  });

  it("Retry answers the last turn again in its place, with the context it was asked with", async () => {
    render(<AssistantChat namespace="team" blocked={null} />);
    await answered("Who owns billing?", "Ada does.", citationsEvent([], "none"));
    await answered("Since when?", "Since May.", citationsEvent([], "none"));
    // Only the last answer offers Retry.
    expect(screen.getAllByRole("button", { name: "Retry" })).toHaveLength(1);
    // The page changed since: a retry still sends the line the question was asked with.
    useStore.setState({ view: "recall" });
    const s = sse();
    answers.push(() => s);
    await userEvent.click(within(lastAnswer()).getByRole("button", { name: "Retry" }));
    // Retry goes once the answer starts: the focus is in the question field, not lost.
    expect(screen.getByLabelText("Question")).toHaveFocus();
    expect(calls[2].body.messages).toEqual([
      { role: "user", content: `${ON_PAGE}Who owns billing?` },
      { role: "assistant", content: "Ada does." },
      { role: "user", content: `${ON_PAGE}Since when?` },
    ]);
    await act(async () => {
      s.text("Since June.");
      s.done();
    });
    await waitFor(() => expect(lastAnswer()).toHaveTextContent("Since June."));
    const turns = useChat.getState().turns;
    expect(turns.map((t) => t.answer)).toEqual(["Ada does.", "Since June."]);
    expect(lastAnswer()).not.toHaveTextContent("Since May.");
  });

  it.each([
    ["a lost connection", (s: ReturnType<typeof sse>) => s.close(), true],
    [
      "a retryable error",
      (s: ReturnType<typeof sse>) => {
        s.error({ code: "ASSISTANT_UPSTREAM_FAILED", message: "failed", retryable: true });
        s.close();
      },
      true,
    ],
    [
      "an error that resending cannot fix",
      (s: ReturnType<typeof sse>) => {
        s.error({ code: "INTERNAL_ERROR", message: "boom", retryable: false });
        s.close();
      },
      false,
    ],
  ])("offers Retry after %s only where it may pass", async (_, end, offered) => {
    const s = sse();
    answers.push(() => s);
    render(<AssistantChat namespace="team" blocked={null} />);
    await ask("Who owns billing?");
    await act(async () => {
      s.text("Ada");
      end(s);
    });
    await screen.findByRole("alert");
    expect(screen.queryByRole("button", { name: "Retry" }) !== null).toBe(offered);
    // Copy is for a finished answer.
    expect(screen.queryByRole("button", { name: "Copy answer 1" })).toBeNull();
  });

  it("offers Retry on a stopped answer", async () => {
    const s = sse();
    answers.push(() => s);
    render(<AssistantChat namespace="team" blocked={null} />);
    await ask("Who owns billing?");
    await act(async () => s.text("Ada"));
    await userEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(await screen.findByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("edits the last question from an empty field: its answer is replaced, and Cancel empties the field", async () => {
    render(<AssistantChat namespace="team" blocked={null} />);
    await answered("Who owns billing?", "Ada does.", citationsEvent([], "none"));
    const field = screen.getByLabelText("Question");
    const user = userEvent.setup();
    // With text in the field there is no Edit: nothing typed can be lost to one.
    await user.type(field, "half typed");
    expect(screen.queryByRole("button", { name: "Edit your question" })).toBeNull();
    await user.clear(field);
    await user.click(screen.getByRole("button", { name: "Edit your question" }));
    expect(field).toHaveValue("Who owns billing?");
    expect(field).toHaveFocus();
    expect(screen.getByTestId("assistant-editing")).toHaveTextContent("Ask replaces its answer.");
    await user.click(screen.getByRole("button", { name: "Cancel edit" }));
    expect(field).toHaveValue("");
    expect(screen.queryByTestId("assistant-editing")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Edit your question" }));
    await user.clear(field);
    const s = sse();
    answers.push(() => s);
    await ask("Who owns invoices?");
    expect(calls[1].body.messages).toEqual([{ role: "user", content: `${ON_PAGE}Who owns invoices?` }]);
    await act(async () => {
      s.text("Grace does.");
      s.done();
    });
    await waitFor(() => expect(lastAnswer()).toHaveTextContent("Grace does."));
    const turns = useChat.getState().turns;
    expect(turns.map((t) => [t.question, t.answer])).toEqual([["Who owns invoices?", "Grace does."]]);
    expect(field).toHaveValue("");
    expect(screen.queryByTestId("assistant-editing")).toBeNull();
  });

  it("ends an edit when another tab's turn becomes the last one: Ask then asks a new question", async () => {
    render(<AssistantChat namespace="team" blocked={null} />);
    await answered("Who owns billing?", "Ada does.", citationsEvent([], "none"));
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Edit your question" }));
    expect(screen.getByTestId("assistant-editing")).toBeInTheDocument();
    // Another tab's finished turn merges in (storedChanged).
    act(() =>
      useChat.setState((st) => ({
        turns: [...st.turns, { id: "theirs", question: "Since when?", answer: "May.", state: "done", at: Date.now() }],
      })),
    );
    expect(screen.queryByTestId("assistant-editing")).toBeNull();
    expect(screen.queryByRole("button", { name: "Cancel edit" })).toBeNull();
    const s = sse();
    answers.push(() => s);
    await user.click(screen.getByRole("button", { name: "Ask" }));
    await act(async () => {
      s.text("Grace does.");
      s.done();
    });
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    expect(useChat.getState().turns.map((t) => [t.question, t.answer])).toEqual([
      ["Who owns billing?", "Ada does."],
      ["Since when?", "May."],
      ["Who owns billing?", "Grace does."],
    ]);
  });

  it("the up arrow in an empty field edits the last question, and in a field with text does not", async () => {
    render(<AssistantChat namespace="team" blocked={null} />);
    await answered("Who owns billing?", "Ada does.", citationsEvent([], "none"));
    const field = screen.getByLabelText("Question");
    const user = userEvent.setup();
    await user.type(field, "x");
    await user.keyboard("{ArrowUp}");
    expect(field).toHaveValue("x");
    await user.clear(field);
    await user.keyboard("{ArrowUp}");
    expect(field).toHaveValue("Who owns billing?");
    expect(useChat.getState().editing?.id).toBe(useChat.getState().turns[0].id);
  });

  it("puts a turn asked again back as it was when the route refuses, the question waiting as an edit", async () => {
    render(<AssistantChat namespace="team" blocked={null} />);
    await answered("Who owns billing?", "Ada does.", citationsEvent([], "none"));
    answers.push(refusal(503, { code: "ASSISTANT_UNAVAILABLE", message: "not yet", reason: "starting" }));
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(useChat.getState().hint).toEqual({ kind: "starting" }));
    expect(useChat.getState().turns.map((t) => t.answer)).toEqual(["Ada does."]);
    expect(lastAnswer()).toHaveTextContent("Ada does.");
    expect(screen.getByLabelText("Question")).toHaveValue("Who owns billing?");
    expect(useChat.getState().editing?.id).toBe(useChat.getState().turns[0].id);
    expect(useChat.getState().refused).toEqual({
      question: "Who owns billing?",
      asked: { context: useChat.getState().turns[0].context, item: null },
      replace: useChat.getState().turns[0].id,
    });

    // A refusal the panel has no reason for is said in its notice.
    act(() => {
      useChat.setState({ hint: null, refused: null, editing: null });
      useDock.setState({ draft: "" });
    });
    answers.push(refusal(429, { code: "RATE_LIMITED", message: "spent", retryable: true }));
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(useChat.getState().notice).toMatch(/^Not asked again: /));
    expect(useChat.getState().turns.map((t) => t.answer)).toEqual(["Ada does."]);
  });

  it("asks an unsupported answer again with sources, tagged, and offers it once", async () => {
    render(<AssistantChat namespace="team" blocked={null} />);
    await answered("When do invoices go out?", "Fridays.", citationsEvent([], "unsupported"));
    const s = sse();
    answers.push(() => s);
    await userEvent.click(screen.getByRole("button", { name: "Ask again with sources" }));
    expect(screen.getByLabelText("Question")).toHaveFocus();
    expect(calls[1].body.messages).toEqual([
      { role: "user", content: `${ON_PAGE}When do invoices go out? Cite your sources.` },
    ]);
    await act(async () => {
      s.text("Fridays [memory:billing/invoices].");
      s.raw(citationsEvent([], "unsupported"));
      s.done();
    });
    await waitFor(() => expect(lastAnswer()).toHaveTextContent("Fridays [memory:billing/invoices]."));
    expect(within(lastAnswer()).getByText("With sources")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Ask again with sources" })).toBeNull();
    expect(useChat.getState().turns).toHaveLength(1);
    // A later question sends it as it was asked.
    const next = sse();
    answers.push(() => next);
    await ask("And refunds?");
    expect(calls[2].body.messages[0].content).toBe(`${ON_PAGE}When do invoices go out? Cite your sources.`);
  });

  it("ends the answer at text_done: Stop goes, Ask waits for the check, and a cited one says it checks", async () => {
    const s = sse();
    answers.push(() => s);
    render(
      <>
        <AssistantChat namespace="team" blocked={null} />
        <p id="loom-assistant-status" role="status" />
      </>,
    );
    await ask("When?");
    await act(async () => {
      s.text("Fridays [memory:billing/invoices].");
      s.raw(textDone);
    });
    await waitFor(() => expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument());
    expect(screen.getByText("Checking sources…")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/^The answer is complete\.$/));
    // Ask waits for the checks, which hold the namespace's answer slot.
    await userEvent.type(screen.getByLabelText("Question"), "Next?");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    expect(calls).toHaveLength(1);
    await act(async () => {
      s.raw(citationsEvent([], "unsupported"));
      s.done();
    });
    await waitFor(() => expect(screen.queryByText("Checking sources…")).toBeNull());
    expect(screen.getByTestId("assistant-support")).toHaveTextContent("Couldn't confirm: it gives no sources");
    // A label that warns is said when it lands.
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(/^Couldn't confirm: it gives no sources\.$/),
    );
  });

  it.each([
    ["opened", false, true, "The answer is complete. Couldn't confirm: it gives no sources."],
    ["closed", true, false, "Couldn't confirm: it gives no sources."],
  ])("says the end once when the panel is %s while the sources are checked", async (_, before, after, said) => {
    const s = sse();
    answers.push(() => s);
    render(
      <>
        <AssistantChat namespace="team" blocked={null} />
        <p id="loom-assistant-status" role="status" />
      </>,
    );
    await ask("When?");
    act(() => useDock.setState({ open: before }));
    await act(async () => {
      s.text("Fridays [memory:billing/invoices].");
      s.raw(textDone);
    });
    await waitFor(() => expect(useChat.getState().checkingSources).not.toBeNull());
    act(() => useDock.setState({ open: after }));
    await act(async () => {
      s.raw(citationsEvent([], "unsupported"));
      s.done();
    });
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(said));
    expect(screen.getByRole("status").textContent).toBe(said);
    expect(useDock.getState().unread).toBe(false);
  });

  it("says a label that warns when the stream breaks after it landed", async () => {
    const s = sse();
    answers.push(() => s);
    render(
      <>
        <AssistantChat namespace="team" blocked={null} />
        <p id="loom-assistant-status" role="status" />
      </>,
    );
    await ask("When?");
    await act(async () => {
      s.text("Fridays [memory:billing/invoices].");
      s.raw(textDone);
      s.raw(citationsEvent([], "unsupported"));
    });
    await waitFor(() => expect(useChat.getState().turns[0].support).toBe("unsupported"));
    await act(async () => s.fail(new TypeError("network error")));
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(/^Couldn't confirm: it gives no sources\.$/),
    );
  });

  it("labels a finished turn with no support only when its check was cut off", () => {
    const turn = { id: "a", question: "When?", answer: "Fridays.", state: "done" as const, at: 1 };
    useChat.setState({ turns: [turn] });
    const { unmount } = render(<AssistantChat namespace="team" blocked={null} />);
    // A server that sends no support: no label.
    expect(screen.queryByTestId("assistant-support")).toBeNull();
    unmount();
    useChat.setState({ turns: [{ ...turn, checkLost: true }] });
    render(<AssistantChat namespace="team" blocked={null} />);
    expect(screen.getByTestId("assistant-support")).toHaveTextContent("Check didn't finish");
  });

  it("gives an edit's words back to the field when it is stopped before any text", async () => {
    render(<AssistantChat namespace="team" blocked={null} />);
    await answered("Who owns billing?", "Ada does.", citationsEvent([], "none"));
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Edit your question" }));
    const field = screen.getByLabelText("Question");
    await user.clear(field);
    const s = sse();
    answers.push(() => s);
    await ask("Who owns invoices?");
    expect(field).toHaveValue("");
    await user.click(screen.getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    expect(useChat.getState().turns.map((t) => [t.question, t.answer])).toEqual([["Who owns billing?", "Ada does."]]);
    expect(field).toHaveValue("Who owns invoices?");
    expect(screen.getByTestId("assistant-editing")).toBeInTheDocument();
  });

  it("keeps the sources on a failed Ask again with sources that waits as an edit", async () => {
    render(<AssistantChat namespace="team" blocked={null} />);
    await answered("When do invoices go out?", "Fridays.", citationsEvent([], "unsupported"));
    answers.push(() => {
      throw new TypeError("Failed to fetch");
    });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Ask again with sources" }));
    await waitFor(() => expect(useChat.getState().notice).toMatch(/^Not asked again: /));
    expect(screen.getByLabelText("Question")).toHaveValue("When do invoices go out?");
    expect(screen.getByTestId("assistant-editing")).toHaveTextContent("asking with sources");
    const s = sse();
    answers.push(() => s);
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(calls[2].body.messages.at(-1)?.content).toBe(`${ON_PAGE}When do invoices go out? Cite your sources.`);
    await act(async () => {
      s.text("Fridays [memory:billing/invoices].");
      s.done();
    });
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    expect(useChat.getState().turns).toHaveLength(1);
    expect(useChat.getState().turns[0].withSources).toBe(true);
  });

  it("edits a question asked with sources with its sources, the field's limit counting them", async () => {
    render(<AssistantChat namespace="team" blocked={null} />);
    await answered("When?", "Fridays.", citationsEvent([], "unsupported"));
    const again = sse();
    answers.push(() => again);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Ask again with sources" }));
    await act(async () => {
      again.text("Fridays [memory:billing/invoices].");
      again.done();
    });
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    await user.click(screen.getByRole("button", { name: "Edit your question" }));
    expect(screen.getByTestId("assistant-editing")).toHaveTextContent("asking with sources");
    // Past the length once " Cite your sources." follows it, the field says so.
    const limit =
      MAX_MESSAGE_CHARS - contextLength(pageContext("memories", null, false)) - " Cite your sources.".length;
    act(() => useDock.setState({ draft: "x".repeat(limit + 1) }));
    expect(screen.getByLabelText("Question")).toHaveAttribute("aria-invalid", "true");
    act(() => useDock.setState({ draft: "When exactly?" }));
    const s = sse();
    answers.push(() => s);
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(calls[2].body.messages.at(-1)?.content).toBe(`${ON_PAGE}When exactly? Cite your sources.`);
    await act(async () => {
      s.text("Fridays.");
      s.done();
    });
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    expect(useChat.getState().turns[0]).toMatchObject({ question: "When exactly?", withSources: true });
  });

  it("offers no Retry nor Ask again with sources while the last question is edited", async () => {
    render(<AssistantChat namespace="team" blocked={null} />);
    await answered("When do invoices go out?", "Fridays.", citationsEvent([], "unsupported"));
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Edit your question" }));
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Ask again with sources" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Cancel edit" }));
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ask again with sources" })).toBeInTheDocument();
  });

  it.each([
    ["partly confirmed", false, /^Partly confirmed: 1 of 2 sources found in Loom\.$/],
    ["fully confirmed", true, /^The answer is complete\.$/],
  ])("after text_done, a %s answer's label is said only when not every source was found", async (_, both, said) => {
    const s = sse();
    answers.push(() => s);
    render(
      <>
        <AssistantChat namespace="team" blocked={null} />
        <p id="loom-assistant-status" role="status" />
      </>,
    );
    await ask("When?");
    await act(async () => {
      s.text("Fridays [memory:billing/invoices@v3] [memory:billing/runs@v1].");
      s.raw(textDone);
    });
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/^The answer is complete\.$/));
    const cited = [
      { kind: "memory", text: "[memory:billing/invoices@v3]", verified: true, reason: "", ref: "billing/invoices" },
      { kind: "memory", text: "[memory:billing/runs@v1]", verified: both, reason: both ? "" : "not found", ref: "" },
    ];
    await act(async () => {
      s.raw(citationsEvent(cited, "supported"));
      s.done();
    });
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(said));
  });

  it("says it checks an uncited answer too, and shows nothing once its check finds no claim", async () => {
    const s = sse();
    answers.push(() => s);
    render(<AssistantChat namespace="team" blocked={null} />);
    await ask("Hi");
    await act(async () => {
      s.text("Hello.");
      s.raw(textDone);
    });
    await waitFor(() => expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument());
    expect(screen.getByText("Checking sources…")).toBeInTheDocument();
    expect(screen.queryByTestId("assistant-sources")).toBeNull();
    await act(async () => {
      s.raw(citationsEvent([], "none"));
      s.done();
    });
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    expect(screen.queryByText("Checking sources…")).toBeNull();
    expect(screen.queryByTestId("assistant-sources")).toBeNull();
  });

  it("keeps the answer done, its sources not checked, when the connection drops after text_done", async () => {
    const s = sse();
    answers.push(() => s);
    render(<AssistantChat namespace="team" blocked={null} />);
    await ask("When?");
    await act(async () => {
      s.text("Fridays [memory:billing/invoices].");
      s.raw(textDone);
    });
    await waitFor(() => expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument());
    await act(async () => s.fail(new TypeError("network error")));
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(useChat.getState().turns[0].state).toBe("done");
    expect(screen.getByTestId("assistant-support")).toHaveTextContent("Check didn't finish");
    expect(screen.getByRole("button", { name: "Copy answer 1" })).toBeInTheDocument();
  });
});
