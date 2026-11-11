// The assistant panel's body: why it cannot answer, in words for who reads them, Try again,
// History, and how the stored conversations show (expiry hides them, one answer runs in a
// conversation across tabs, a namespace switch asks how to go on).
import { act, cleanup, render, screen, waitFor, within } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { mode } = vi.hoisted(() => ({ mode: { authOff: false, proxy: false } }));
vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, isAuthDisabled: () => mode.authOff, isProxyMode: () => mode.proxy };
});

import { useStore } from "../../lib/state";
import { loadDockRuntime, useDock, type AskItem } from "../../lib/assistantDock";
import { askAbout } from "../../lib/assistantDockRuntime";
import { PANEL_DEFAULT } from "../../lib/assistantLayout";
import { INDEX_KEY } from "../../lib/assistantStorage";
import { axeViolations } from "../../test/axe";
import { ConfirmViewport } from "../../components/ConfirmDialog";
import { READINESS_POLL_MS, DockBody } from "./Dock";
import { READINESS_TIMEOUT_MS } from "./readiness";
import { OTHER_TAB, resetChat, useChat } from "./chatStore";
import {
  ANSWER_MAX_MS,
  ConversationDeleted,
  adoptEpoch,
  deleteConversation,
  readConversation,
  saveConversation,
  type Turn,
} from "./conversations";

function jwt(payload: Record<string, unknown>): string {
  const b64 = (s: string) => btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64(JSON.stringify({ alg: "HS256" }))}.${b64(JSON.stringify(payload))}.sig`;
}
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const enc = new TextEncoder();
function answer(text: string) {
  return new Response(
    new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(enc.encode(`data: ${JSON.stringify({ choices: [{ delta: { content: text } }] })}\n\n`));
        c.enqueue(enc.encode("data: [DONE]\n\n"));
        c.close();
      },
    }),
    { status: 200, headers: { "Content-Type": "text/event-stream" } },
  );
}

let check: Record<string, unknown> | null = { ok: true, required: false };
let chat: Array<(init?: RequestInit) => Response> = [];
let modelCalls: () => Response = () => json({ enabled: false, locked: false });
let paths: string[] = [];
/** The namespace each chat request was sent to, and the questions it carried. */
let asked: string[] = [];
let sent: string[][] = [];
/** The readiness read never answers (it ends only by its signal). */
let hangReady = false;
const alice = { subject: "alice", org: "acme", scope: "-", user: "-" };

type Who = "viewer" | "nsAdmin" | "orgAdmin" | "platform";
function signIn(who: Who, claims: Record<string, unknown> = {}) {
  const roles = who === "platform" ? ["PLATFORM_ADMIN"] : who === "orgAdmin" ? ["ORG_ADMIN"] : ["VIEWER"];
  localStorage.setItem("loom_token_v1", jwt({ sub: "alice", org: "acme", namespaces: ["team"], roles, ...claims }));
  useStore.setState({
    whoami: { authenticated: true, subject: "alice", org: "acme", namespaces: ["team"], roles },
    identity: { org: "acme", namespace: "team", agent: "loom-ui" },
    myNamespaces: [
      {
        namespace: "team",
        role: who === "nsAdmin" ? "admin" : "reader",
        status: "active",
        visibility: "private",
        kind: "memory",
      } as never,
    ],
    sessionExpired: false,
  });
  adoptEpoch(null);
}

function show() {
  return render(
    <>
      <DockBody />
      <p id="loom-assistant-status" role="status" />
    </>,
  );
}

const unavailable = () => screen.findByTestId("assistant-unavailable", {}, { timeout: 3000 });

async function askIt(question: string) {
  const user = userEvent.setup();
  await waitFor(() => expect(screen.queryByText("Getting the assistant ready…")).not.toBeInTheDocument());
  await user.type(screen.getByLabelText("Question"), question);
  await user.click(screen.getByRole("button", { name: "Ask" }));
  return user;
}

beforeEach(() => {
  localStorage.clear();
  mode.authOff = false;
  mode.proxy = false;
  check = { ok: true, required: false };
  chat = [];
  paths = [];
  asked = [];
  sent = [];
  hangReady = false;
  modelCalls = () => json({ enabled: false, locked: false });
  resetChat(null);
  useChat.setState({ hint: null, readiness: null, storageOff: false });
  // A test that opens another page leaves it for the next one's question context otherwise.
  useStore.setState({ view: "memories" });
  useDock.setState({
    open: true,
    mounted: true,
    draft: "",
    reason: null,
    focusField: false,
    prefWidth: PANEL_DEFAULT,
    item: null,
    pageOff: false,
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      const path = url.split("?")[0];
      paths.push(path);
      if (path === "/v1/health/ready" && hangReady)
        return new Promise<Response>((_, reject) =>
          init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))),
        );
      if (path === "/v1/health/ready") return json({ ready: true, checks: check ? { assistant: check } : {} });
      if (path === "/v1/org.model_calls") return modelCalls();
      if (path === "/v1/assistant/chat/completions") {
        asked.push(new Headers(init?.headers).get("X-Loom-Namespace") ?? "");
        const body = JSON.parse(String(init?.body ?? "{}")) as { messages?: { role: string; content: string }[] };
        sent.push((body.messages ?? []).filter((m) => m.role === "user").map((m) => m.content));
        const next = chat.shift();
        if (!next) throw new Error("no answer queued");
        return next(init);
      }
      return json({});
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const refusal = (status: number, detail: object) => () => json({ detail }, status);

describe("reasons, worded per reader", () => {
  it.each<[Who, string, boolean]>([
    ["platform", "Set up the assistant: see Health", true],
    ["nsAdmin", "An admin of this deployment sets up the assistant.", false],
    ["viewer", "An admin of this deployment sets up the assistant.", false],
  ])("not configured, for a %s", async (who, text, link) => {
    check = null;
    signIn(who);
    show();
    expect(await unavailable()).toHaveTextContent(text);
    expect(!!screen.queryByRole("button", { name: "see Health" })).toBe(link);
    expect(screen.getByRole("button", { name: "Ask" })).toHaveAttribute("aria-disabled", "true");
    await waitFor(() => expect(useDock.getState().reason).toEqual({ kind: "not_configured" }));
  });

  it.each<[Who, string, boolean]>([
    [
      "platform",
      "The assistant's setup failed: see Health. Loom retries the setup on its own; this updates when it works.",
      true,
    ],
    [
      "nsAdmin",
      "An admin of this deployment fixes the assistant's setup. Loom retries the setup on its own; this updates when it works. Details: see Health",
      true,
    ],
    [
      "viewer",
      "An admin of this deployment fixes the assistant's setup. Loom retries the setup on its own; this updates when it works.",
      false,
    ],
  ])("failing, for a %s", async (who, text, link) => {
    check = { ok: false, reason: "failing", required: false };
    signIn(who);
    show();
    const body = await unavailable();
    expect(body).toHaveTextContent(text);
    expect(!!within(body).queryByRole("button", { name: "see Health" })).toBe(link);
  });

  it("starting, and readiness is read again while it sets up", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    check = { ok: false, reason: "starting", required: false };
    signIn("viewer");
    show();
    expect(await unavailable()).toHaveTextContent("Setting up — usually under a minute. This updates on its own.");
    const reads = paths.filter((p) => p === "/v1/health/ready").length;
    check = { ok: true, required: false };
    await act(async () => {
      vi.advanceTimersByTime(READINESS_POLL_MS + 10);
    });
    await waitFor(() => expect(paths.filter((p) => p === "/v1/health/ready").length).toBeGreaterThan(reads));
    await waitFor(() => expect(screen.queryByTestId("assistant-unavailable")).not.toBeInTheDocument());
  });

  it("a route refusal that says setting up is read again, and goes once readiness reads ready", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    signIn("viewer");
    show();
    chat.push(refusal(503, { code: "ASSISTANT_UNAVAILABLE", message: "not set up yet", reason: "starting" }));
    await askIt("Who owns billing?");
    expect(await unavailable()).toHaveTextContent("Setting up — usually under a minute. This updates on its own.");
    const reads = paths.filter((p) => p === "/v1/health/ready").length;
    await act(async () => {
      vi.advanceTimersByTime(READINESS_POLL_MS + 10);
    });
    await waitFor(() => expect(paths.filter((p) => p === "/v1/health/ready").length).toBeGreaterThan(reads));
    await waitFor(() => expect(screen.queryByTestId("assistant-unavailable")).not.toBeInTheDocument());
    expect(useChat.getState().hint).toBeNull();
  });

  it("a token-bound refusal's reason goes when the owner changes", async () => {
    signIn("viewer");
    show();
    chat.push(refusal(403, { code: "AUTH_INSUFFICIENT", message: "narrowed", reason: "narrowed" }));
    await askIt("Who owns billing?");
    expect(await unavailable()).toHaveAttribute("data-reason", "narrowed");
    act(() =>
      useStore.setState({
        whoami: { authenticated: true, subject: "bob", org: "acme", namespaces: ["team"], roles: ["VIEWER"] },
      }),
    );
    await waitFor(() => expect(screen.queryByTestId("assistant-unavailable")).not.toBeInTheDocument());
    expect(useChat.getState().hint).toBeNull();
  });

  it("auth off, decided without a request, and nothing stored", async () => {
    mode.authOff = true;
    signIn("platform");
    show();
    expect(await unavailable()).toHaveTextContent(
      "The assistant answers as the signed-in person, so it needs auth on. An operator turns auth on for this deployment.",
    );
    expect(paths).toEqual([]);
    expect(localStorage.getItem(INDEX_KEY)).toBeNull();
  });

  it("an org-wide token, from its claims", async () => {
    signIn("viewer", { org: "*" });
    show();
    expect(await unavailable()).toHaveTextContent(
      "This token works across every org; the assistant answers in one. Sign in with a token for one org.",
    );
  });

  it("opened to a reason known at once, leaves the focus in the question field", async () => {
    useDock.setState({ focusField: true });
    signIn("viewer", { org: "*" });
    show();
    await unavailable();
    await waitFor(() => expect(screen.getByLabelText("Question")).toHaveFocus());
  });

  it("a narrowed token, from its claims", async () => {
    signIn("viewer", { read_user_id: "u1" });
    show();
    expect(await unavailable()).toHaveTextContent(
      "This token can't use the assistant: it is narrowed to user u1. Sign in with a token that reads the whole namespace.",
    );
  });

  it("a token that expires too soon, from the route's refusal only", async () => {
    signIn("viewer", { exp: Math.floor(Date.now() / 1000) + 120, sid: "s1", fps: true });
    show();
    chat.push(
      refusal(403, {
        code: "AUTH_INSUFFICIENT",
        message: "the token expires in 120 s",
        reason: "token_expires_soon",
        left_s: 120,
      }),
    );
    // The browser does not block it on its claims: the question is sent.
    await askIt("Who owns billing?");
    expect(await unavailable()).toHaveTextContent(
      "This token expires in 120 s, sooner than an answer can take. Sign in again or use a longer-lived token.",
    );
    expect(paths.filter((p) => p === "/v1/assistant/chat/completions")).toHaveLength(1);
  });

  describe("model calls off", () => {
    const off = refusal(403, { code: "MODEL_CALLS_DISABLED", message: "model calls are switched off" });

    it("a viewer gets the sentence true either way, with no read of the lock", async () => {
      signIn("viewer");
      show();
      chat.push(off);
      await askIt("Who?");
      expect(await unavailable()).toHaveTextContent(
        "Model calls are off for this org. An org admin can switch them on in Admin › Guardrails, unless a platform admin locked them.",
      );
      expect(paths).not.toContain("/v1/org.model_calls");
    });

    it("an org admin is sent to Guardrails", async () => {
      signIn("orgAdmin");
      show();
      chat.push(off);
      await askIt("Who?");
      await waitFor(async () =>
        expect(await unavailable()).toHaveTextContent("Switch on model calls in Admin › Guardrails"),
      );
      expect(screen.getByRole("button", { name: "Admin › Guardrails" })).toBeInTheDocument();
    });

    it("an org admin under a platform admin's lock is told only they can switch them on", async () => {
      modelCalls = () => json({ enabled: false, locked: true, locked_by: "root" });
      signIn("orgAdmin");
      show();
      chat.push(off);
      await askIt("Who?");
      await waitFor(async () =>
        expect(await unavailable()).toHaveTextContent(
          "A platform admin locked model calls off for this org; only they can switch them on.",
        ),
      );
    });

    it("a platform admin under the lock is sent to Guardrails", async () => {
      modelCalls = () => json({ enabled: false, locked: true, locked_by: "root" });
      signIn("platform");
      show();
      chat.push(off);
      await askIt("Who?");
      await waitFor(async () =>
        expect(await unavailable()).toHaveTextContent("Switch on model calls in Admin › Guardrails"),
      );
    });

    it("says model calls were just switched on when the switch reads on", async () => {
      modelCalls = () => json({ enabled: true, locked: false });
      signIn("orgAdmin");
      show();
      chat.push(off);
      await askIt("Who?");
      await waitFor(async () =>
        expect(await unavailable()).toHaveTextContent(
          "Model calls were just switched on; this takes a few seconds to reach every server.",
        ),
      );
      expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    });

    it("falls back to the sentence true either way when the read fails", async () => {
      modelCalls = () => json({ detail: { code: "AUTH_INSUFFICIENT", message: "no" } }, 403);
      signIn("orgAdmin");
      show();
      chat.push(off);
      await askIt("Who?");
      await waitFor(async () =>
        expect(await unavailable()).toHaveTextContent(
          "An org admin can switch them on in Admin › Guardrails, unless a platform admin locked them.",
        ),
      );
    });
  });
});

describe("Try again", () => {
  it("with no question refused, reads readiness again and sends nothing", async () => {
    check = { ok: false, reason: "failing", required: false };
    signIn("viewer");
    show();
    await unavailable();
    const reads = paths.filter((p) => p === "/v1/health/ready").length;
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(paths.filter((p) => p === "/v1/health/ready").length).toBe(reads + 1));
    expect(paths).not.toContain("/v1/assistant/chat/completions");
  });

  it("asks a refused question again once nothing stands in the way", async () => {
    signIn("viewer");
    show();
    chat.push(refusal(503, { code: "ASSISTANT_UNAVAILABLE", message: "not set up yet", reason: "starting" }));
    await askIt("Who owns billing?");
    await unavailable();
    chat.push(() => answer("Ada."));
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Ada.")).toBeInTheDocument();
    expect(paths.filter((p) => p === "/v1/assistant/chat/completions")).toHaveLength(2);
  });

  it("answers a refused Retry in the turn it asked again, not as a new turn", async () => {
    signIn("viewer");
    show();
    chat.push(() => answer("Ada."));
    await askIt("Who owns billing?");
    expect(await screen.findByText("Ada.")).toBeInTheDocument();
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    chat.push(refusal(503, { code: "ASSISTANT_UNAVAILABLE", message: "not set up yet", reason: "starting" }));
    await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }));
    await unavailable();
    // The turn is back as it was, its question waiting in the field as an edit.
    expect(screen.getByText("Ada.")).toBeInTheDocument();
    chat.push(() => answer("Grace."));
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Grace.")).toBeInTheDocument();
    expect(useChat.getState().turns.map((t) => t.answer)).toEqual(["Grace."]);
    expect(screen.queryByText("Ada.")).toBeNull();
  });

  const starting = () => refusal(503, { code: "ASSISTANT_UNAVAILABLE", message: "not set up yet", reason: "starting" });
  async function answered(question: string, text: string) {
    chat.push(() => answer(text));
    await askIt(question);
    expect(await screen.findByText(text)).toBeInTheDocument();
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
  }
  const pairs = () => useChat.getState().turns.map((t) => [t.question, t.answer]);

  it("asks a refused new question as a new turn, the one before staying, and offers no Edit over it", async () => {
    signIn("viewer");
    show();
    await answered("Who owns billing?", "Ada.");
    chat.push(starting());
    await askIt("Since when?");
    await unavailable();
    const user = userEvent.setup();
    // The refused question waits in the field: an Edit would replace it.
    expect(screen.queryByRole("button", { name: "Edit your question" })).toBeNull();
    chat.push(() => answer("Since May."));
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Since May.")).toBeInTheDocument();
    expect(pairs()).toEqual([
      ["Who owns billing?", "Ada."],
      ["Since when?", "Since May."],
    ]);
  });

  it("offers no Edit, up arrow or Retry while an edit put back waits for Try again", async () => {
    signIn("viewer");
    show();
    await answered("Who owns billing?", "Ada.");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Edit your question" }));
    const field = screen.getByLabelText("Question");
    await user.clear(field);
    await user.type(field, "Who owns invoices?");
    // The attempt fails with no reason the panel shows, while the person types the next question.
    chat.push(() => {
      throw new TypeError("Failed to fetch");
    });
    await user.click(screen.getByRole("button", { name: "Ask" }));
    await user.type(field, "next");
    await waitFor(() => expect(useChat.getState().refused?.question).toBe("Who owns invoices?"));
    await user.clear(field);
    expect(screen.queryByRole("button", { name: "Edit your question" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    await user.keyboard("{ArrowUp}");
    expect(field).toHaveValue("");
    // Try again still asks the edited words in place.
    chat.push(() => answer("Grace."));
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Grace.")).toBeInTheDocument();
    expect(pairs()).toEqual([["Who owns invoices?", "Grace."]]);
  });

  it("edits nothing with the up arrow while nothing can be asked", async () => {
    signIn("viewer");
    show();
    await answered("Who owns billing?", "Ada.");
    chat.push(starting());
    await askIt("Since when?");
    await unavailable();
    const user = userEvent.setup();
    const field = screen.getByLabelText("Question");
    await user.clear(field);
    await user.click(field);
    await user.keyboard("{ArrowUp}");
    expect(field).toHaveValue("");
    expect(useChat.getState().editing).toBeNull();
  });

  it("offers no Edit while a refused question waits, even with the field cleared", async () => {
    signIn("viewer");
    show();
    await answered("Who owns billing?", "Ada.");
    chat.push(starting());
    await askIt("Since when?");
    await unavailable();
    const user = userEvent.setup();
    // Nothing can be asked until the reason goes: an edit opened now would end with the
    // refused question's Try again.
    await user.clear(screen.getByLabelText("Question"));
    expect(screen.queryByRole("button", { name: "Edit your question" })).toBeNull();
  });

  it("answers a Retry refused while the field held other text in its turn, and keeps that text", async () => {
    signIn("viewer");
    show();
    await answered("Who owns billing?", "Ada.");
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Question"), "half typed");
    chat.push(starting());
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await unavailable();
    expect(screen.getByLabelText("Question")).toHaveValue("half typed");
    chat.push(() => answer("Grace."));
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Grace.")).toBeInTheDocument();
    expect(pairs()).toEqual([["Who owns billing?", "Grace."]]);
    expect(screen.getByLabelText("Question")).toHaveValue("half typed");
  });

  it("asks a refused Retry no more after Cancel edit: Try again then only reads readiness", async () => {
    signIn("viewer");
    show();
    await answered("Who owns billing?", "Ada.");
    chat.push(starting());
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await unavailable();
    await user.click(screen.getByRole("button", { name: "Cancel edit" }));
    expect(useChat.getState().editing).toBeNull();
    expect(useChat.getState().refused).toBeNull();
    const reads = paths.filter((p) => p === "/v1/health/ready").length;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(paths.filter((p) => p === "/v1/health/ready").length).toBe(reads + 1));
    expect(paths.filter((p) => p === "/v1/assistant/chat/completions")).toHaveLength(2);
    expect(pairs()).toEqual([["Who owns billing?", "Ada."]]);
  });

  it("asks a refused Ask again with sources with its sources, and keeps the button off while blocked", async () => {
    signIn("viewer");
    show();
    const citations = `data: ${JSON.stringify({ object: "loom.citations", id: "m", choices: [], citations: [], support: "unsupported" })}\n\n`;
    chat.push(
      () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(c) {
              c.enqueue(enc.encode(`data: ${JSON.stringify({ choices: [{ delta: { content: "Fridays." } }] })}\n\n`));
              c.enqueue(enc.encode(citations));
              c.enqueue(enc.encode("data: [DONE]\n\n"));
              c.close();
            },
          }),
          { status: 200, headers: { "Content-Type": "text/event-stream" } },
        ),
    );
    await askIt("When do invoices go out?");
    const user = userEvent.setup();
    // Text in the field: the refused question waits for Try again, not as an edit.
    await user.type(screen.getByLabelText("Question"), "half typed");
    chat.push(starting());
    await user.click(await screen.findByRole("button", { name: "Ask again with sources" }));
    await unavailable();
    // The turn put back waits for Try again: no second attempt can replace its words.
    expect(screen.queryByRole("button", { name: "Ask again with sources" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(sent).toHaveLength(2);
    chat.push(() => answer("On Fridays."));
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("On Fridays.")).toBeInTheDocument();
    expect(sent[2].at(-1)).toBe("Context: Memories page\n\nWhen do invoices go out? Cite your sources.");
    expect(useChat.getState().turns).toHaveLength(1);
    expect(useChat.getState().turns[0].withSources).toBe(true);
  });

  it("sends a refused Retry on Try again with the page line the turn was asked with", async () => {
    signIn("viewer");
    show();
    await answered("Who owns billing?", "Ada.");
    act(() => useStore.setState({ view: "recall" }));
    chat.push(starting());
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await unavailable();
    chat.push(() => answer("Grace."));
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Grace.")).toBeInTheDocument();
    expect(sent[2]).toEqual(["Context: Memories page\n\nWho owns billing?"]);
  });

  /** A streamed answer of these events, closed after them; with none, it stays open until
   *  the request's abort errors it, as a browser's fetch does. */
  const stream = (events: string[], init?: RequestInit) =>
    new Response(
      new ReadableStream<Uint8Array>({
        start(c) {
          for (const e of events) c.enqueue(enc.encode(e));
          if (events.length > 0) c.close();
          else init?.signal?.addEventListener("abort", () => c.error(new DOMException("aborted", "AbortError")));
        },
      }),
      { status: 200, headers: { "Content-Type": "text/event-stream" } },
    );
  it.each([
    [
      "a lost connection before the request",
      () => () => {
        throw new TypeError("Failed to fetch");
      },
      false,
    ],
    [
      "an error event before any text",
      () => () =>
        stream([
          `data: ${JSON.stringify({ error: { code: "ASSISTANT_UPSTREAM_FAILED", message: "unreachable", retryable: true } })}\n\n`,
        ]),
      false,
    ],
    ["Stop before the first token", () => (init?: RequestInit) => stream([], init), true],
  ])("keeps the answer before a Retry that ends with %s, here and stored", async (_, next, stopIt) => {
    signIn("viewer");
    show();
    await answered("Who owns billing?", "Ada.");
    const id = useChat.getState().id as string;
    chat.push(next());
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    if (stopIt) await user.click(await screen.findByRole("button", { name: "Stop" }));
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    expect(pairs()).toEqual([["Who owns billing?", "Ada."]]);
    expect(useChat.getState().turns[0].state).toBe("done");
    expect(screen.getByText("Ada.")).toBeInTheDocument();
    expect(readConversation(id, alice)?.turns.map((t) => [t.answer, t.state])).toEqual([["Ada.", "done"]]);
    expect(useChat.getState().notice).toMatch(stopIt ? /^Stopped before the answer began/ : /^Not asked again: /);
  });

  it("keeps the answer before a Retry stored until its text begins: a reload and the next tab find it", async () => {
    signIn("viewer");
    fakeLocks();
    show();
    await answered("Who owns billing?", "Ada.");
    const id = useChat.getState().id as string;
    chat.push((init?: RequestInit) => stream([], init));
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(useChat.getState().answering).toBe(true));
    // The page hides before any text: the stored turn is still the answer before.
    window.dispatchEvent(new Event("pagehide"));
    const stored = () => readConversation(id, alice, { answering: "stopped" })?.turns.map((t) => [t.answer, t.state]);
    expect(stored()).toEqual([["Ada.", "done"]]);
    // The tab goes; the next one opens the conversation and asks in it.
    act(() => resetChat(alice));
    await user.click(screen.getByRole("button", { name: "History" }));
    await user.click(screen.getByRole("button", { name: "Who owns billing?" }));
    expect(await screen.findByText("Ada.")).toBeInTheDocument();
    chat.push(() => answer("Since May."));
    await user.type(screen.getByLabelText("Question"), "Since when?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText("Since May.")).toBeInTheDocument();
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    expect(stored()).toEqual([
      ["Ada.", "done"],
      ["Since May.", "done"],
    ]);
  });

  it.each([
    [
      "text_done, its check and its end",
      [
        `data: ${JSON.stringify({ object: "loom.text_done", id: "m", choices: [] })}\n\n`,
        `data: ${JSON.stringify({ object: "loom.citations", id: "m", choices: [], citations: [], support: "none" })}\n\n`,
        "data: [DONE]\n\n",
      ],
    ],
    ["only its end", ["data: [DONE]\n\n"]],
  ])("keeps the answer before a Retry whose new answer is empty, with %s, here and stored", async (_, events) => {
    signIn("viewer");
    show();
    await answered("Who owns billing?", "Ada.");
    const id = useChat.getState().id as string;
    chat.push((init?: RequestInit) => stream(events, init));
    await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    expect(pairs()).toEqual([["Who owns billing?", "Ada."]]);
    expect(screen.getByText("Ada.")).toBeInTheDocument();
    expect(readConversation(id, alice)?.turns.map((t) => [t.answer, t.state])).toEqual([["Ada.", "done"]]);
    expect(useChat.getState().notice).toBe("No new answer came: the answer before it stays.");
  });

  it.each([
    ["Stop", true],
    ["a lost connection", false],
  ])("keeps an edit's words for Try again when %s ends it after the field got other text", async (_, stopIt) => {
    signIn("viewer");
    show();
    await answered("Who owns billing?", "Ada.");
    let fail!: () => void;
    chat.push(
      (init?: RequestInit) =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(c) {
              fail = () => c.error(new TypeError("network error"));
              init?.signal?.addEventListener("abort", () => c.error(new DOMException("aborted", "AbortError")));
            },
          }),
          { status: 200, headers: { "Content-Type": "text/event-stream" } },
        ),
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Edit your question" }));
    const field = screen.getByLabelText("Question");
    await user.clear(field);
    await user.type(field, "Who owns invoices?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    await waitFor(() => expect(useChat.getState().answering).toBe(true));
    await user.type(field, "half typed");
    if (stopIt) await user.click(screen.getByRole("button", { name: "Stop" }));
    else await act(async () => fail());
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    expect(pairs()).toEqual([["Who owns billing?", "Ada."]]);
    expect(field).toHaveValue("half typed");
    chat.push(() => answer("Grace."));
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Grace.")).toBeInTheDocument();
    expect(pairs()).toEqual([["Who owns invoices?", "Grace."]]);
    expect(field).toHaveValue("half typed");
  });

  /** An edit of the last question stopped before any text: its words wait for Try again. */
  async function stoppedEdit() {
    await answered("Who owns billing?", "Ada.");
    chat.push((init?: RequestInit) => stream([], init));
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Edit your question" }));
    const field = screen.getByLabelText("Question");
    await user.clear(field);
    await user.type(field, "Who owns invoices?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    await user.click(await screen.findByRole("button", { name: "Stop" }));
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    return user;
  }
  const chats = () => paths.filter((p) => p === "/v1/assistant/chat/completions").length;

  it("asks a stopped edit in no other conversation: New conversation and a History open drop it", async () => {
    signIn("viewer");
    show();
    const user = await stoppedEdit();
    await user.click(screen.getByRole("button", { name: "New conversation" }));
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
    expect(useChat.getState().refused).toBeNull();

    // Again, then the conversation opened from History.
    await stoppedEdit();
    await user.click(screen.getByRole("button", { name: "History" }));
    await user.click(screen.getAllByRole("button", { name: "Who owns billing?" })[0]);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Try again" })).toBeNull());
    expect(useChat.getState().refused).toBeNull();
    expect(chats()).toBe(4);
  });

  it("offers no Try again for a refused new question once its reason went: the question is in the field", async () => {
    signIn("viewer");
    show();
    chat.push(starting());
    await askIt("Who owns billing?");
    await unavailable();
    act(() => useChat.setState({ hint: null }));
    await waitFor(() => expect(screen.queryByTestId("assistant-unavailable")).toBeNull());
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
    expect(screen.getByLabelText("Question")).toHaveValue("Who owns billing?");
  });

  it("offers no Try again for a stopped edit while the namespace shown differs", async () => {
    signIn("viewer");
    show();
    await stoppedEdit();
    act(() =>
      useStore.setState({
        identity: { org: "acme", namespace: "ops", agent: "loom-ui" },
        myNamespaces: [
          { namespace: "team", role: "reader", status: "active", kind: "memory" } as never,
          { namespace: "ops", role: "reader", status: "active", kind: "memory" } as never,
        ],
      }),
    );
    await waitFor(() => expect(screen.queryByRole("button", { name: "Try again" })).toBeNull());
  });

  it("Cancel edit drops a stopped edit's Try again too", async () => {
    signIn("viewer");
    show();
    const user = await stoppedEdit();
    await user.click(screen.getByRole("button", { name: "Cancel edit" }));
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
    expect(useChat.getState().refused).toBeNull();
    expect(screen.getByLabelText("Question")).toHaveValue("");
  });

  it("says why beside Try again once readiness clears a setting-up refusal of a Retry", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    signIn("viewer");
    show();
    await answered("Who owns billing?", "Ada.");
    chat.push(starting());
    await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }));
    await unavailable();
    await act(async () => {
      vi.advanceTimersByTime(READINESS_POLL_MS + 10);
    });
    await waitFor(() => expect(screen.queryByTestId("assistant-unavailable")).toBeNull());
    // The notice line, beside what the live region said.
    const line = "The assistant can't answer: setting up. The answer before it stays.";
    expect(screen.getAllByText(line).filter((el) => el.id !== "loom-assistant-status")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.getByText("Ada.")).toBeInTheDocument();
  });

  it("offers no Try again for a Retry refused with no reason the panel shows: the question waits as an edit", async () => {
    signIn("viewer");
    show();
    await answered("Who owns billing?", "Ada.");
    chat.push(refusal(429, { code: "RATE_LIMITED", message: "over the cap", retry_after_s: 60 }));
    await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(useChat.getState().notice).toMatch(/^Not asked again: /));
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
    expect(useChat.getState().refused).toBeNull();
    expect(screen.getByLabelText("Question")).toHaveValue("Who owns billing?");
    expect(screen.getByTestId("assistant-editing")).toBeInTheDocument();
    expect(pairs()).toEqual([["Who owns billing?", "Ada."]]);
  });

  it("keeps Ask off while the token's own claims still refuse", async () => {
    signIn("viewer", { read_scope: "own_session" });
    show();
    await unavailable();
    useChat.setState({ refused: { question: "Who?" } });
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(paths).toContain("/v1/health/ready"));
    expect(paths).not.toContain("/v1/assistant/chat/completions");
    expect(screen.getByRole("button", { name: "Ask" })).toHaveAttribute("aria-disabled", "true");
  });
});

const done = (i: number): Turn => ({
  id: `t${i}`,
  question: `Question ${i}`,
  answer: `Answer ${i}`,
  state: "done",
  at: i,
});

describe("History", () => {
  it("lists this person's conversations, with the browser-only note, Delete and Delete all", async () => {
    signIn("viewer");
    saveConversation({
      id: "c1",
      owner: alice,
      namespace: "team",
      title: "Invoices",
      turns: [done(0)],
      trimmed: false,
      updated: Date.now(),
    });
    saveConversation({
      id: "c2",
      owner: { ...alice, scope: "own_user", user: "u1" },
      namespace: "team",
      title: "Narrow",
      turns: [done(0)],
      trimmed: false,
      updated: Date.now(),
    });
    saveConversation({
      id: "c3",
      owner: alice,
      namespace: "team",
      title: "Refunds",
      turns: [done(0)],
      trimmed: false,
      updated: Date.now() - 1000,
    });
    const { container } = show();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "History" }));
    const list = screen.getByRole("list", { name: "Saved conversations" });
    expect(
      within(list)
        .getAllByRole("listitem")
        .map((li) => li.textContent),
    ).toEqual([expect.stringContaining("Invoices"), expect.stringContaining("Refunds")]);
    expect(screen.getByText("Saved in this browser only, for 30 days. Signing out deletes them.")).toBeInTheDocument();
    expect(screen.getByText("Deleting a memory doesn't change answers already saved here.")).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
    await user.click(screen.getByRole("button", { name: "Delete Refunds" }));
    expect(screen.queryByText("Refunds")).not.toBeInTheDocument();
    vi.stubGlobal("confirm", () => true);
    await user.click(screen.getByRole("button", { name: "Delete all" }));
    await waitFor(() => expect(screen.getByText("No conversations saved here yet.")).toBeInTheDocument());
  });

  it("lists no conversation kept in a collection: the header never holds one", async () => {
    signIn("viewer");
    useStore.setState((st) => ({
      myNamespaces: [
        ...(st.myNamespaces ?? []),
        { namespace: "col-research", role: "viewer", status: "active", kind: "collection" } as never,
      ],
    }));
    for (const [id, namespace, title] of [
      ["c1", "team", "Invoices"],
      ["c2", "col-research", "Research notes"],
    ])
      saveConversation({ id, owner: alice, namespace, title, turns: [done(0)], trimmed: false, updated: Date.now() });
    show();
    await userEvent.setup().click(screen.getByRole("button", { name: "History" }));
    const list = screen.getByRole("list", { name: "Saved conversations" });
    expect(within(list).getByText("Invoices")).toBeInTheDocument();
    expect(within(list).queryByText(/Research notes/)).toBeNull();
  });

  it("opens a saved conversation, its older turns said not kept", async () => {
    signIn("viewer");
    const turns = Array.from({ length: 30 }, (_, i) => done(i));
    saveConversation({
      id: "c1",
      owner: alice,
      namespace: "team",
      title: "Long",
      turns,
      trimmed: false,
      updated: Date.now(),
    });
    show();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "History" }));
    await user.click(screen.getByRole("button", { name: "Long" }));
    expect(screen.getByText("Earlier turns were not kept.")).toBeInTheDocument();
    expect(screen.getByText("Answer 29")).toBeInTheDocument();
    expect(screen.queryByText("Answer 4")).not.toBeInTheDocument();
  });

  it("behind a proxy, says only how long", async () => {
    mode.proxy = true;
    signIn("viewer");
    show();
    await userEvent.setup().click(screen.getByRole("button", { name: "History" }));
    expect(screen.getByText("Saved in this browser only, for 30 days.")).toBeInTheDocument();
  });

  it("hides the conversations while the session is expired, and shows them again on the same person's sign-in", async () => {
    signIn("viewer");
    saveConversation({
      id: "c1",
      owner: alice,
      namespace: "team",
      title: "Invoices",
      turns: [done(0)],
      trimmed: false,
      updated: Date.now(),
    });
    show();
    act(() => useStore.setState({ sessionExpired: true }));
    expect(screen.getByText("Your session ended. Sign in again to see your conversations.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "History" })).not.toBeInTheDocument();
    act(() => useStore.setState({ sessionExpired: false }));
    await userEvent.setup().click(screen.getByRole("button", { name: "History" }));
    expect(screen.getByRole("button", { name: "Invoices" })).toBeInTheDocument();
  });

  it("reopens on the chat, with the focus in the question field, after History was left showing", async () => {
    signIn("viewer");
    saved([done(0)]);
    show();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "History" }));
    expect(await screen.findByRole("heading", { name: "History" })).toBeInTheDocument();
    act(() => useDock.setState({ open: false }));
    act(() => useDock.setState({ open: true, focusField: true }));
    await waitFor(() => expect(screen.getByLabelText("Question")).toHaveFocus());
  });
});

describe("the conversation", () => {
  it("is saved when its turn starts, so a reload mid-answer shows it as stopped", async () => {
    signIn("viewer");
    fakeLocks();
    show();
    let finish!: () => void;
    chat.push(
      () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(c) {
              c.enqueue(enc.encode(`data: ${JSON.stringify({ choices: [{ delta: { content: "Ada" } }] })}\n\n`));
              finish = () => c.close();
            },
          }),
          { status: 200 },
        ),
    );
    await askIt("Who owns billing?");
    await screen.findByText("Ada");
    const id = useChat.getState().id as string;
    const stored = JSON.parse(localStorage.getItem(`loom_assistant_c_${id}`) || "null");
    expect(stored.turns[0]).toMatchObject({ question: "Who owns billing?", state: "answering" });
    // The page hides mid-answer: what came in is saved, and reads back as stopped.
    window.dispatchEvent(new Event("pagehide"));
    // The page goes: its answer ends and its lock goes with it.
    act(() => resetChat(alice));
    await act(async () => finish());
    await userEvent.setup().click(screen.getByRole("button", { name: "History" }));
    await userEvent.setup().click(screen.getByRole("button", { name: "Who owns billing?" }));
    expect(await screen.findByText("Stopped.")).toBeInTheDocument();
    expect(screen.getByText("Ada")).toBeInTheDocument();
  });

  it("is stored as answering while its sources are checked, so no other tab shows it, and a reload reads it done", async () => {
    signIn("viewer");
    show();
    let finish!: () => void;
    chat.push(
      () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(c) {
              c.enqueue(enc.encode(`data: ${JSON.stringify({ choices: [{ delta: { content: "Ada." } }] })}\n\n`));
              c.enqueue(enc.encode(`data: ${JSON.stringify({ object: "loom.text_done", id: "m", choices: [] })}\n\n`));
              finish = () => c.close();
            },
          }),
          { status: 200 },
        ),
    );
    await askIt("Who owns billing?");
    await waitFor(() => expect(useChat.getState().checkingSources).not.toBeNull());
    const id = useChat.getState().id as string;
    // The page hides while the sources are checked.
    window.dispatchEvent(new Event("pagehide"));
    expect(readConversation(id, alice)?.turns).toEqual([]);
    expect(readConversation(id, alice, { answering: "stopped" })?.turns[0]).toMatchObject({
      answer: "Ada.",
      state: "done",
    });
    expect(readConversation(id, alice, { answering: "stopped" })?.turns[0].textDone).toBeUndefined();
    await act(async () => finish());
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    expect(readConversation(id, alice)?.turns[0]).toMatchObject({ answer: "Ada.", state: "done" });
  });

  it("asks how to go on when the header switches namespace under it", async () => {
    signIn("viewer");
    show();
    chat.push(() => answer("Ada."));
    await askIt("Who owns billing?");
    await screen.findByText("Ada.");
    act(() =>
      useStore.setState({
        identity: { org: "acme", namespace: "ops", agent: "loom-ui" },
        myNamespaces: [
          { namespace: "team", role: "reader", status: "active", kind: "memory" } as never,
          { namespace: "ops", role: "reader", status: "active", kind: "memory" } as never,
        ],
      }),
    );
    expect(screen.getByText("This conversation is about team.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ask" })).toHaveAttribute("aria-disabled", "true");
    await userEvent.setup().click(screen.getByRole("button", { name: "Start a new one in ops" }));
    expect(screen.queryByText("Ada.")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(document.getElementById("loom-assistant-status")).toHaveTextContent(
        "New conversation in ops; the one about team is in History.",
      ),
    );
  });

  it("links a conversation kept in a collection to the Library", async () => {
    signIn("viewer");
    show();
    act(() => useChat.setState({ namespace: "col-research", turns: [done(0)] }));
    await userEvent.setup().click(await screen.findByRole("button", { name: "Library" }));
    expect(useStore.getState().view).toBe("knowledge");
    // It closes the panel too, even one beside the page, which a navigation leaves open.
    expect(useDock.getState().open).toBe(false);
  });

  it("offers no way back into a collection a kept conversation is about", async () => {
    signIn("viewer");
    show();
    act(() => useChat.setState({ namespace: "col-research", turns: [done(0)] }));
    expect(
      await screen.findByText(/^This conversation is about a collection; collections are in the/),
    ).toHaveTextContent("This conversation is about a collection; collections are in the Library.");
    expect(screen.queryByText(/col-research/)).toBeNull();
    expect(screen.queryByRole("button", { name: /^Switch back to/ })).toBeNull();
    await userEvent.setup().click(screen.getByRole("button", { name: "Start a new one in team" }));
    // History lists it no more, so the announcement does not send anyone there.
    await waitFor(() =>
      expect(document.getElementById("loom-assistant-status")).toHaveTextContent(/^New conversation in team\.$/),
    );
  });

  it("a card's pick in a conversation about another namespace reads its card and asks nothing", async () => {
    signIn("viewer");
    show();
    chat.push(() => answer("Ada."));
    await askIt("Who owns billing?");
    await screen.findByText("Ada.");
    const inventory = {
      scope: { org: "acme", namespace: "team", read_scope: "namespace", user_id: null, session_id: null },
      sections: [],
      populations: [
        { population: "agents", total: 1, top: [{ name: "claude-code", rows: 40 }], complete: true, reason: "" },
      ],
      capabilities: [],
      computed_at: "2026-11-01T10:00:00",
    };
    const card = {
      id: "inv",
      question: "What does this namespace hold, and who appears in it?",
      answer: "",
      state: "card",
      card: { op: "namespace.inventory", args: {}, data: inventory, readAt: Date.now() },
      at: Date.now(),
    };
    act(() => useChat.setState((c) => ({ turns: [...c.turns, card as never] })));
    act(() =>
      useStore.setState({
        identity: { org: "acme", namespace: "ops", agent: "loom-ui" },
        myNamespaces: [
          { namespace: "team", role: "viewer", status: "active", kind: "memory" } as never,
          { namespace: "ops", role: "viewer", status: "active", kind: "memory" } as never,
        ],
      }),
    );
    expect(screen.getByText("This conversation is about team.")).toBeInTheDocument();
    const pick = await screen.findByTestId("pick-agent");
    // The pick itself is open: the person's own read of the card's namespace.
    expect(pick).not.toHaveAttribute("aria-disabled");
    const before = asked.length;
    await userEvent.setup().click(pick);
    await waitFor(() => expect(paths).toContain("/v1/memory.inspect"));
    expect(asked).toHaveLength(before);
    // With "Also ask" on, the pick's card says why nothing was asked, as a starter's does.
    expect(
      await screen.findByText(
        "The assistant can't answer: pick how to go on with this conversation first. This is Loom's own read.",
      ),
    ).toBeInTheDocument();
  });

  it("the Insights button pressed while an answer comes in says on screen that the starters wait", async () => {
    signIn("viewer");
    show();
    chat.push(() => answer("Ada."));
    await askIt("Who owns billing?");
    await screen.findByText("Ada.");
    act(() => useChat.setState({ answering: true }));
    act(() => useDock.setState((d) => ({ startersSeq: (d.startersSeq ?? 0) + 1, startersAudit: true })));
    expect(await screen.findByText("Starters show once this answer ends: choose New conversation.")).toBeVisible();
    expect(useDock.getState().startersAudit).toBe(false);
    act(() => useChat.setState({ answering: false }));
  });

  it("a card's pick waits while an answer is coming in, and says so", async () => {
    signIn("viewer");
    // A viewer of the namespace may inspect it: the pick is open once nothing comes in.
    useStore.setState({
      myNamespaces: [{ namespace: "team", role: "viewer", status: "active", kind: "memory" } as never],
    });
    show();
    chat.push(() => answer("Ada."));
    await askIt("Who owns billing?");
    await screen.findByText("Ada.");
    const card = {
      id: "inv",
      question: "What does this namespace hold, and who appears in it?",
      answer: "",
      state: "card",
      card: {
        op: "namespace.inventory",
        args: {},
        data: {
          scope: { org: "acme", namespace: "team", read_scope: "namespace", user_id: null, session_id: null },
          sections: [],
          populations: [
            { population: "agents", total: 1, top: [{ name: "claude-code", rows: 40 }], complete: true, reason: "" },
          ],
          capabilities: [],
          computed_at: "2026-11-01T10:00:00",
        },
        readAt: Date.now(),
      },
      at: Date.now(),
    };
    act(() => useChat.setState((c) => ({ turns: [...c.turns, card as never], answering: true })));
    const pick = await screen.findByTestId("pick-agent");
    expect(pick).toHaveAttribute("aria-disabled", "true");
    expect(pick).toHaveAccessibleDescription("An answer is still coming in.");
    act(() => useChat.setState({ answering: false }));
    await waitFor(() => expect(screen.getByTestId("pick-agent")).not.toHaveAttribute("aria-disabled"));
  });

  it("on a namespace that holds no memories, says to pick one and keeps Ask off", async () => {
    signIn("viewer");
    useStore.setState({
      identity: { org: "acme", namespace: "repo", agent: "loom-ui" },
      myNamespaces: [{ namespace: "repo", role: "reader", status: "active", kind: "code" } as never],
    });
    show();
    expect(
      await screen.findByText("The assistant answers about a memory namespace. Pick one in the namespace menu."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ask" })).toHaveAttribute("aria-disabled", "true");
  });

  it("marks the launcher when an answer ends while the panel is closed, and says so once", async () => {
    signIn("viewer");
    show();
    chat.push(() => answer("Ada."));
    const user = userEvent.setup();
    await waitFor(() => expect(screen.queryByText("Getting the assistant ready…")).not.toBeInTheDocument());
    await user.type(screen.getByLabelText("Question"), "Who?");
    act(() => useDock.setState({ open: false }));
    await user.click(screen.getByRole("button", { name: "Ask" }));
    await waitFor(() => expect(useDock.getState().unread).toBe(true));
    await waitFor(() =>
      expect(document.getElementById("loom-assistant-status")).toHaveTextContent("Assistant answer ready"),
    );
  });
});

/** A Web Locks stand-in shared with "another tab": `hold` and `release` are that tab's. */
function fakeLocks() {
  const held = new Set<string>();
  const waiters: Array<() => void> = [];
  const wake = () => {
    for (const w of waiters.splice(0)) w();
  };
  let gate: Promise<void> | null = null;
  const api = {
    held,
    hold: (name: string) => held.add(name),
    release: (name: string) => {
      held.delete(name);
      wake();
    },
    /** Hold every grant until the returned function runs. */
    delayGrants: () => {
      let open!: () => void;
      gate = new Promise<void>((r) => (open = r));
      return () => {
        gate = null;
        open();
      };
    },
    query: async () => ({ held: [...held].map((name) => ({ name })), pending: [] }),
    request: async (name: string, a: unknown, b?: unknown) => {
      const opts = (typeof a === "function" ? {} : a) as { ifAvailable?: boolean };
      const cb = (typeof a === "function" ? a : b) as (lock: unknown) => unknown;
      if (gate) await gate;
      if (opts.ifAvailable && held.has(name)) return cb(null);
      while (held.has(name)) await new Promise<void>((r) => waiters.push(r));
      held.add(name);
      try {
        return await cb({ name });
      } finally {
        held.delete(name);
        wake();
      }
    },
  };
  vi.stubGlobal("navigator", { ...navigator, locks: api });
  return api;
}

const saved = (turns: Turn[]) =>
  saveConversation({
    id: "c1",
    owner: alice,
    namespace: "team",
    title: "Invoices",
    turns,
    trimmed: false,
    updated: Date.now(),
  });

async function openSaved(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "History" }));
  await user.click(screen.getByRole("button", { name: "Invoices" }));
  await waitFor(() => expect(screen.queryByText("Getting the assistant ready…")).not.toBeInTheDocument());
}

describe("two tabs", () => {
  it("lets go of a lease stopped before it was granted, and the next question takes it", async () => {
    signIn("viewer");
    const locks = fakeLocks();
    show();
    const grant = locks.delayGrants();
    const user = await askIt("Who?");
    await user.click(screen.getByRole("button", { name: "Stop" }));
    await act(async () => grant());
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    expect(locks.held.size).toBe(0);
    expect(paths).not.toContain("/v1/assistant/chat/completions");
    chat.push(() => answer("Ada."));
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText("Ada.")).toBeInTheDocument();
    await waitFor(() => expect(locks.held.size).toBe(0));
  });

  it("keeps a turn another tab saved when this one saves after it", async () => {
    signIn("viewer");
    saved([done(0)]);
    show();
    const user = userEvent.setup();
    await openSaved(user);
    // The other tab's turn lands without this tab hearing of it.
    saved([done(0), done(1)]);
    chat.push(() => answer("Ada."));
    await user.type(screen.getByLabelText("Question"), "Who?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    await screen.findByText("Ada.");
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    const stored = JSON.parse(localStorage.getItem("loom_assistant_c_c1") || "null");
    expect(stored.turns.map((t: Turn) => t.question)).toEqual(["Question 0", "Question 1", "Who?"]);
    // The question went with the other tab's turn, read once this tab held the lock.
    expect(sent).toEqual([["Question 0", "Question 1", "Context: Memories page\n\nWho?"]]);
  });

  it("says when another tab deleted the conversation, which then is not saved", async () => {
    signIn("viewer");
    saved([done(0)]);
    show();
    const user = userEvent.setup();
    await openSaved(user);
    deleteConversation("c1");
    chat.push(() => answer("Ada."));
    await user.type(screen.getByLabelText("Question"), "Who?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText("This conversation was deleted: it is no longer saved.")).toBeInTheDocument();
    expect(localStorage.getItem("loom_assistant_c_c1")).toBeNull();
  });
});

/** A stream that stays open until `close` (or `text`) is called. */
function held() {
  let ctl!: ReadableStreamDefaultController<Uint8Array>;
  const response = new Response(
    new ReadableStream<Uint8Array>({
      start(c) {
        ctl = c;
      },
    }),
    { status: 200 },
  );
  return {
    response: () => response,
    text: (t: string) =>
      ctl.enqueue(enc.encode(`data: ${JSON.stringify({ choices: [{ delta: { content: t } }] })}\n\n`)),
    close: () => ctl.close(),
  };
}

const startingRefusal = refusal(503, { code: "ASSISTANT_UNAVAILABLE", message: "not set up yet", reason: "starting" });
// Saved now: a turn still answering an hour on reads as stopped (ANSWER_MAX_MS).
const answering = (i: number): Turn => ({ ...done(i), answer: "", state: "answering", at: Date.now() });
const status = () => document.getElementById("loom-assistant-status");

describe("one answer per conversation across tabs", () => {
  it("sends nothing while another tab holds the conversation's lock, and Ask stays on", async () => {
    signIn("viewer");
    saved([done(0)]);
    const locks = fakeLocks();
    show();
    const user = userEvent.setup();
    await openSaved(user);
    // The other tab holds the lock and has saved its turn's start.
    locks.hold("loom_assistant_c_c1");
    saved([done(0), answering(1)]);
    await user.type(screen.getByLabelText("Question"), "Who?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText(OTHER_TAB)).toBeInTheDocument();
    expect(OTHER_TAB).toBe("Another tab was answering in this conversation. Ask again once it ends.");
    await waitFor(() => expect(status()).toHaveTextContent(OTHER_TAB));
    expect(paths).not.toContain("/v1/assistant/chat/completions");
    expect(screen.getByLabelText("Question")).toHaveValue("Who?");
    expect(screen.getByRole("button", { name: "Ask" })).not.toHaveAttribute("aria-disabled");
    locks.release("loom_assistant_c_c1");
    chat.push(() => answer("Ada."));
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText("Ada.")).toBeInTheDocument();
    expect(screen.queryByText(OTHER_TAB, { selector: "p:not(#loom-assistant-status)" })).toBeNull();
  });

  it("an edit of the question clears the note about the refused Ask", async () => {
    signIn("viewer");
    saved([done(0)]);
    const locks = fakeLocks();
    show();
    const user = userEvent.setup();
    await openSaved(user);
    locks.hold("loom_assistant_c_c1");
    saved([done(0), answering(1)]);
    await user.type(screen.getByLabelText("Question"), "Who?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText(OTHER_TAB)).toBeInTheDocument();
    await user.type(screen.getByLabelText("Question"), " now");
    expect(useChat.getState().notice).toBeNull();
    expect(screen.queryByText(OTHER_TAB, { selector: "p:not(#loom-assistant-status)" })).toBeNull();
  });

  it("shows the other tab's finished turn when its final save landed during the refused Ask", async () => {
    signIn("viewer");
    saved([done(0)]);
    const locks = fakeLocks();
    show();
    const user = userEvent.setup();
    await openSaved(user);
    locks.hold("loom_assistant_c_c1");
    // Its last save lands with no storage event heard here (this tab was asking for the lock).
    saved([done(0), done(1)]);
    await user.type(screen.getByLabelText("Question"), "Who?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText("Answer 1")).toBeInTheDocument();
    expect(paths).not.toContain("/v1/assistant/chat/completions");
    // The refusal stays said on screen until the next Ask or an edit of the question.
    expect(useChat.getState().notice).toBe(OTHER_TAB);
    expect(screen.getByText(OTHER_TAB, { selector: "p:not(#loom-assistant-status)" })).toBeInTheDocument();
  });

  it("asks again in the tab that just answered while another tab has the conversation open", async () => {
    signIn("viewer");
    const locks = fakeLocks();
    show();
    chat.push(() => answer("Ada."));
    const user = await askIt("Who?");
    await screen.findByText("Ada.");
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    // The other tab reads the conversation and takes no lock to do so.
    const id = useChat.getState().id as string;
    expect(readConversation(id, alice)?.turns).toHaveLength(1);
    chat.push(() => answer("Since May."));
    await user.type(screen.getByLabelText("Question"), "Since when?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText("Since May.")).toBeInTheDocument();
    expect(paths.filter((p) => p === "/v1/assistant/chat/completions")).toHaveLength(2);
    await waitFor(() => expect(locks.held.size).toBe(0));
  });

  it("shows another tab's turn only once it ends", async () => {
    signIn("viewer");
    saved([done(0)]);
    show();
    const user = userEvent.setup();
    await openSaved(user);
    saved([done(0), answering(1)]);
    act(() => window.dispatchEvent(new StorageEvent("storage", { key: "loom_assistant_c_c1" })));
    expect(screen.queryByText("Question 1")).toBeNull();
    expect(screen.queryByText("Stopped.")).toBeNull();
    saved([done(0), { ...done(1), at: 2 }]);
    act(() => window.dispatchEvent(new StorageEvent("storage", { key: "loom_assistant_c_c1" })));
    expect(await screen.findByText("Answer 1")).toBeInTheDocument();
  });

  it("a turn another tab took back after a refusal never shows here, nor comes back from here", async () => {
    signIn("viewer");
    saved([done(0)]);
    show();
    const user = userEvent.setup();
    await openSaved(user);
    // The other tab starts a turn, then the route refuses it and that tab saves without it.
    saved([done(0), answering(1)]);
    act(() => window.dispatchEvent(new StorageEvent("storage", { key: "loom_assistant_c_c1" })));
    saved([done(0)]);
    act(() => window.dispatchEvent(new StorageEvent("storage", { key: "loom_assistant_c_c1" })));
    window.dispatchEvent(new Event("pagehide"));
    expect(readConversation("c1", alice, { answering: "kept" })?.turns.map((t) => t.id)).toEqual(["t0"]);
    chat.push(() => answer("Ada."));
    await user.type(screen.getByLabelText("Question"), "Who?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    await screen.findByText("Ada.");
    expect(sent).toEqual([["Question 0", "Context: Memories page\n\nWho?"]]);
  });

  it("a turn this tab took back after a refusal is never stored as finished", async () => {
    signIn("viewer");
    saved([done(0)]);
    show();
    const user = userEvent.setup();
    await openSaved(user);
    chat.push(startingRefusal);
    await user.type(screen.getByLabelText("Question"), "Who?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    await unavailable();
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    const stored = JSON.parse(localStorage.getItem("loom_assistant_c_c1") || "null");
    expect(stored.turns.map((t: Turn) => t.id)).toEqual(["t0"]);
  });

  it("a refused only question deletes its conversation, so no tab writes it back", async () => {
    signIn("viewer");
    show();
    chat.push(startingRefusal);
    let id = "";
    const off = useChat.subscribe((s) => {
      if (s.id) id = s.id;
    });
    await askIt("Who?");
    await unavailable();
    off();
    expect(useChat.getState().id).toBeNull();
    expect(localStorage.getItem(`loom_assistant_c_${id}`)).toBeNull();
    // A tab that read its start writes it back on hiding: refused.
    expect(() =>
      saveConversation({
        id,
        owner: alice,
        namespace: "team",
        title: "Who?",
        turns: [{ ...done(0), state: "stopped" }],
        trimmed: false,
        updated: Date.now(),
      }),
    ).toThrow(ConversationDeleted);
  });

  it("opens a conversation whose stored answer no tab holds as stopped, and leaves one a tab holds out", async () => {
    signIn("viewer");
    saved([done(0), { ...answering(1), answer: "Ada" }]);
    const locks = fakeLocks();
    locks.hold("loom_assistant_c_c1");
    show();
    const user = userEvent.setup();
    await openSaved(user);
    await waitFor(() => expect(useChat.getState().id).toBe("c1"));
    expect(screen.queryByText("Ada")).toBeNull();
    locks.release("loom_assistant_c_c1");
    await act(async () => resetChat(alice));
    await openSaved(user);
    expect(await screen.findByText("Stopped.")).toBeInTheDocument();
    expect(screen.getByText("Ada")).toBeInTheDocument();
  });

  it("Delete of the conversation answering here stops the answer first, and nothing writes it back", async () => {
    signIn("viewer");
    show();
    const s = held();
    chat.push(s.response);
    const user = await askIt("Who?");
    await waitFor(() => expect(useChat.getState().id).not.toBeNull());
    const id = useChat.getState().id as string;
    await user.click(screen.getByRole("button", { name: "History" }));
    await user.click(screen.getByRole("button", { name: "Delete Who?" }));
    expect(useChat.getState()).toMatchObject({ answering: false, id: null, notice: null });
    expect(useDock.getState().working).toBe(false);
    await act(async () => {
      s.text("Ada");
      s.close();
    });
    expect(localStorage.getItem(`loom_assistant_c_${id}`)).toBeNull();
    await user.click(screen.getByRole("button", { name: "History" }));
    expect(screen.queryByText("This conversation was deleted: it is no longer saved.")).toBeNull();
  });

  it("a late callback of a reset answer leaves a newer answer alone", async () => {
    signIn("viewer");
    show();
    const first = held();
    const second = held();
    chat.push(first.response, second.response);
    const user = await askIt("Who?");
    await waitFor(() => expect(useChat.getState().turns).toHaveLength(1));
    act(() => resetChat(alice));
    await user.type(screen.getByLabelText("Question"), "Since when?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    await waitFor(() => expect(useChat.getState().turns.map((t) => t.question)).toEqual(["Since when?"]));
    await act(async () => {
      first.text("Old");
      first.close();
    });
    expect(useChat.getState().answering).toBe(true);
    expect(useDock.getState().working).toBe(true);
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
    await act(async () => {
      second.text("May.");
      second.close();
    });
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    expect(screen.queryByText("Old")).toBeNull();
  });
});

describe("a turn stored while it answered", () => {
  const storedTurns = () =>
    (JSON.parse(localStorage.getItem("loom_assistant_c_c1") || "null").turns as Turn[]).map((t) => [t.id, t.state]);

  it("left by a tab that closed mid-answer is kept as stopped by the next tab to ask", async () => {
    signIn("viewer");
    saved([done(0), { ...answering(1), answer: "Ada" }]);
    const locks = fakeLocks();
    locks.hold("loom_assistant_c_c1");
    show();
    const user = userEvent.setup();
    await openSaved(user);
    expect(screen.queryByText("Ada")).toBeNull();
    // That tab closes mid-answer: its lock goes, its turn stays stored as answering.
    locks.release("loom_assistant_c_c1");
    chat.push(() => answer("Since May."));
    await user.type(screen.getByLabelText("Question"), "Since when?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText("Since May.")).toBeInTheDocument();
    expect(screen.getByText("Ada")).toBeInTheDocument();
    expect(screen.getByText("Stopped.")).toBeInTheDocument();
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    expect(storedTurns().slice(0, 2)).toEqual([
      ["t0", "done"],
      ["t1", "stopped"],
    ]);
  });

  it("left while its sources were checked is kept as done by the next tab to ask", async () => {
    signIn("viewer");
    saved([done(0), { ...answering(1), answer: "Ada.", textDone: true }]);
    const locks = fakeLocks();
    locks.hold("loom_assistant_c_c1");
    show();
    const user = userEvent.setup();
    await openSaved(user);
    expect(screen.queryByText("Ada.")).toBeNull();
    locks.release("loom_assistant_c_c1");
    chat.push(() => answer("Since May."));
    await user.type(screen.getByLabelText("Question"), "Since when?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText("Since May.")).toBeInTheDocument();
    expect(screen.getByText("Ada.")).toBeInTheDocument();
    expect(screen.getByText("Check didn't finish")).toBeInTheDocument();
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    expect(storedTurns().slice(0, 2)).toEqual([
      ["t0", "done"],
      ["t1", "done"],
    ]);
  });

  it("of another tab still answering stays stored when this tab saves on hiding", async () => {
    signIn("viewer");
    saved([done(0)]);
    const locks = fakeLocks();
    show();
    const user = userEvent.setup();
    await openSaved(user);
    locks.hold("loom_assistant_c_c1");
    saved([done(0), answering(1)]);
    act(() => window.dispatchEvent(new StorageEvent("storage", { key: "loom_assistant_c_c1" })));
    window.dispatchEvent(new Event("pagehide"));
    expect(storedTurns()).toEqual([
      ["t0", "done"],
      ["t1", "answering"],
    ]);
    expect(screen.queryByText("Question 1")).toBeNull();
  });

  it("without Web Locks reads as stopped once an hour old", async () => {
    signIn("viewer");
    saved([done(0), { ...answering(1), answer: "Ada", at: Date.now() - ANSWER_MAX_MS - 1000 }]);
    vi.stubGlobal("navigator", { ...navigator, locks: undefined });
    show();
    await openSaved(userEvent.setup());
    expect(await screen.findByText("Stopped.")).toBeInTheDocument();
    expect(screen.getByText("Ada")).toBeInTheDocument();
  });

  it("without Web Locks, a refused only question keeps a conversation that holds another tab's turn", async () => {
    signIn("viewer");
    saveConversation({
      id: "c1",
      owner: alice,
      namespace: "team",
      title: "Invoices",
      turns: [answering(0)],
      trimmed: false,
      updated: Date.now(),
    });
    vi.stubGlobal("navigator", { ...navigator, locks: undefined });
    show();
    const user = userEvent.setup();
    await openSaved(user);
    await waitFor(() => expect(useChat.getState().id).toBe("c1"));
    expect(screen.queryByText("Question 0")).toBeNull();
    chat.push(startingRefusal);
    await user.type(screen.getByLabelText("Question"), "Who?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    await unavailable();
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    expect(useChat.getState().id).toBe("c1");
    const stored = readConversation("c1", alice, { answering: "kept" });
    expect(stored?.turns.map((t) => [t.id, t.state])).toEqual([["t0", "answering"]]);
  });

  it("without Web Locks is left out while young, never shown as stopped", async () => {
    signIn("viewer");
    saved([done(0), { ...answering(1), answer: "Ada" }]);
    vi.stubGlobal("navigator", { ...navigator, locks: undefined });
    show();
    await openSaved(userEvent.setup());
    await waitFor(() => expect(useChat.getState().id).toBe("c1"));
    expect(screen.getByText("Answer 0")).toBeInTheDocument();
    expect(screen.queryByText("Ada")).toBeNull();
    expect(screen.queryByText("Stopped.")).toBeNull();
  });

  it("the note that another tab answers goes once that tab's turn arrives finished", async () => {
    signIn("viewer");
    saved([done(0)]);
    const locks = fakeLocks();
    show();
    const user = userEvent.setup();
    await openSaved(user);
    locks.hold("loom_assistant_c_c1");
    saved([done(0), answering(1)]);
    await user.type(screen.getByLabelText("Question"), "Who?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText(OTHER_TAB)).toBeInTheDocument();
    // Still answering there: the note stays.
    act(() => window.dispatchEvent(new StorageEvent("storage", { key: "loom_assistant_c_c1" })));
    expect(screen.getByText(OTHER_TAB)).toBeInTheDocument();
    saved([done(0), { ...done(1), at: 2 }]);
    locks.release("loom_assistant_c_c1");
    act(() => window.dispatchEvent(new StorageEvent("storage", { key: "loom_assistant_c_c1" })));
    expect(await screen.findByText("Answer 1")).toBeInTheDocument();
    expect(screen.queryByText(OTHER_TAB, { selector: "p:not(#loom-assistant-status)" })).toBeNull();
  });
});

describe("an answer's unknown names", () => {
  it("are kept with the turn: the label shows again when the conversation opens after a reload", async () => {
    signIn("viewer");
    show();
    const cited = [{ kind: "memory", text: "[memory:a/b/c@v3]", verified: true, reason: "", ref: "a/b/c", anchor: "" }];
    chat.push(
      () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(c) {
              const send = (o: unknown) => c.enqueue(enc.encode(`data: ${JSON.stringify(o)}\n\n`));
              send({ choices: [{ delta: { content: "Call `memory.frobnicate` [memory:a/b/c@v3]." } }] });
              send({
                object: "loom.citations",
                choices: [],
                citations: cited,
                support: "unsupported",
                unknown_names: ["memory.frobnicate"],
              });
              c.enqueue(enc.encode("data: [DONE]\n\n"));
              c.close();
            },
          }),
          { status: 200, headers: { "Content-Type": "text/event-stream" } },
        ),
    );
    const label = "Couldn't confirm: it names memory.frobnicate, which this Loom neither serves nor documents";
    const user = await askIt("How do I pin a memory?");
    expect(await screen.findByTestId("assistant-support")).toHaveTextContent(label);
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    // A reload: the page's state goes, the stored conversation stays.
    act(() => resetChat(alice));
    expect(screen.queryByTestId("assistant-support")).toBeNull();
    await user.click(screen.getByRole("button", { name: "History" }));
    await user.click(screen.getByRole("button", { name: "How do I pin a memory?" }));
    expect(await screen.findByTestId("assistant-support")).toHaveTextContent(label);
  });
});

describe("a refused first question", () => {
  it("starts nothing: after a namespace switch, Ask and Try again both go to the namespace shown", async () => {
    signIn("viewer");
    show();
    chat.push(refusal(503, { code: "ASSISTANT_UNAVAILABLE", message: "not set up yet", reason: "starting" }));
    await askIt("Who owns billing?");
    await unavailable();
    expect(useChat.getState()).toMatchObject({ id: null, namespace: null, turns: [] });
    await waitFor(() =>
      expect(document.getElementById("loom-assistant-status")).toHaveTextContent(
        "The assistant can't answer: setting up.",
      ),
    );
    act(() =>
      useStore.setState({
        identity: { org: "acme", namespace: "ops", agent: "loom-ui" },
        myNamespaces: [
          { namespace: "team", role: "reader", status: "active", kind: "memory" } as never,
          { namespace: "ops", role: "reader", status: "active", kind: "memory" } as never,
        ],
      }),
    );
    chat.push(() => answer("Ada."));
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Ada.")).toBeInTheDocument();
    expect(useChat.getState().namespace).toBe("ops");
    chat.push(() => answer("Since May."));
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    await user.type(screen.getByLabelText("Question"), "Since when?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText("Since May.")).toBeInTheDocument();
    expect(asked).toEqual(["team", "ops", "ops"]);
  });

  it("Try again keeps Ask's checks: on a namespace with no memories it sends nothing", async () => {
    signIn("viewer");
    show();
    chat.push(refusal(503, { code: "ASSISTANT_UNAVAILABLE", message: "not set up yet", reason: "starting" }));
    await askIt("Who owns billing?");
    await unavailable();
    act(() =>
      useStore.setState({
        identity: { org: "acme", namespace: "repo", agent: "loom-ui" },
        myNamespaces: [{ namespace: "repo", role: "reader", status: "active", kind: "code" } as never],
      }),
    );
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    await screen.findByText("The assistant answers about a memory namespace. Pick one in the namespace menu.");
    expect(asked).toEqual(["team"]);
  });

  it("a token that expires too soon, with no time given, names no number", async () => {
    signIn("viewer");
    show();
    chat.push(refusal(403, { code: "AUTH_INSUFFICIENT", message: "expires soon", reason: "token_expires_soon" }));
    await askIt("Who?");
    expect(await unavailable()).toHaveTextContent(
      "This token expires sooner than an answer can take. Sign in again or use a longer-lived token.",
    );
  });
});

describe("readiness", () => {
  it("a read that never answers counts as failed once its time is up, and Ask is on", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    hangReady = true;
    signIn("viewer");
    show();
    expect(screen.getByText("Getting the assistant ready…")).toBeInTheDocument();
    await act(async () => {
      vi.advanceTimersByTime(READINESS_TIMEOUT_MS + 10);
    });
    await waitFor(() => expect(screen.queryByText("Getting the assistant ready…")).not.toBeInTheDocument());
    expect(screen.queryByTestId("assistant-unavailable")).not.toBeInTheDocument();
    await userEvent.setup({ advanceTimers: vi.advanceTimersByTime }).type(screen.getByLabelText("Question"), "Who?");
    expect(screen.getByRole("button", { name: "Ask" })).not.toHaveAttribute("aria-disabled");
  });
});

describe("the focus, beside the page", () => {
  beforeEach(() => useDock.setState({ viewport: 1440, collapsed: false }));

  const two = () => {
    saved([done(0)]);
    saveConversation({
      id: "c2",
      owner: alice,
      namespace: "team",
      title: "Refunds",
      turns: [done(0)],
      trimmed: false,
      updated: Date.now() - 1000,
    });
  };

  it("a row's Delete hands it to the next row, then the last one's to the History heading", async () => {
    signIn("viewer");
    two();
    show();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "History" }));
    await user.click(screen.getByRole("button", { name: "Delete Invoices" }));
    expect(screen.getByRole("button", { name: "Refunds" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Delete Refunds" }));
    expect(screen.getByRole("heading", { name: "History" })).toHaveFocus();
  });

  it("Delete all hands it to the History heading", async () => {
    signIn("viewer");
    two();
    render(
      <>
        <DockBody />
        <ConfirmViewport />
        <p id="loom-assistant-status" role="status" />
      </>,
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "History" }));
    await user.click(screen.getByRole("button", { name: "Delete all" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete all" }));
    await waitFor(() => expect(screen.getByText("No conversations saved here yet.")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("heading", { name: "History" })).toHaveFocus());
  });

  it("Try again that clears the reason hands it to the question field", async () => {
    check = { ok: false, reason: "failing", required: false };
    signIn("viewer");
    show();
    await unavailable();
    check = { ok: true, required: false };
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.getByLabelText("Question")).toHaveFocus());
  });
});

describe("the page a question is asked from", () => {
  const item = (over: Partial<AskItem> = {}): AskItem => ({
    view: "memories",
    org: "acme",
    namespace: "team",
    chip: "key user/pref/lang",
    line: 'memory "user/pref/lang"',
    ...over,
  });
  const chipText = () => screen.getByTestId("assistant-page-chip").textContent;
  const HINT = "Your question names this page. Remove it here.";
  beforeEach(() => useStore.setState({ view: "memories" }));

  it("each new question carries the page, shown as a chip, sent as a leading line the transcript leaves out", async () => {
    signIn("viewer");
    show();
    chat.push(() => answer("Ada."));
    await waitFor(() => expect(chipText()).toBe("Includes: Memories"));
    expect(screen.getByRole("button", { name: "Don't include this page" })).toBeInTheDocument();
    expect(screen.getByText(HINT)).toBeInTheDocument();
    await askIt("Who owns billing?");
    await screen.findByText("Ada.");
    expect(sent).toEqual([["Context: Memories page\n\nWho owns billing?"]]);
    const turn = screen.getByRole("list", { name: "Conversation" });
    expect(turn).toHaveTextContent("Who owns billing?");
    expect(turn).toHaveTextContent("Includes: Memories");
    expect(turn).not.toHaveTextContent("Context:");
    // The hint is shown until the first question.
    expect(screen.queryByText(HINT)).not.toBeInTheDocument();
    expect(localStorage.getItem("loom_assistant_page_hint")).toBe("1");
  });

  it("removing the chip sends nothing about the page; the next question carries it again", async () => {
    signIn("viewer");
    show();
    chat.push(
      () => answer("Ada."),
      () => answer("Since May."),
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Don't include this page" }));
    expect(screen.queryByTestId("assistant-page-chip")).not.toBeInTheDocument();
    expect(screen.queryByText(HINT)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Question")).toHaveFocus();
    await askIt("Who owns billing?");
    await screen.findByText("Ada.");
    expect(within(screen.getByRole("list", { name: "Conversation" })).queryByText(/Includes:/)).toBeNull();
    expect(chipText()).toBe("Includes: Memories");
    await askIt("Since when?");
    await screen.findByText("Since May.");
    expect(sent[1]).toEqual(["Who owns billing?", "Context: Memories page\n\nSince when?"]);
  });

  it("Ask about this adds the item to the chip, for one question, and opens the panel on the field", async () => {
    signIn("viewer");
    useDock.setState({ open: false });
    show();
    chat.push(() => answer("Ada."));
    act(() => askAbout(item()));
    expect(useDock.getState().open).toBe(true);
    await waitFor(() => expect(chipText()).toBe("Includes: Memories · key user/pref/lang"));
    await waitFor(() => expect(screen.getByLabelText("Question")).toHaveFocus());
    expect(chat).toHaveLength(1);
    await askIt("Who wrote it?");
    await screen.findByText("Ada.");
    expect(sent).toEqual([['Context: Memories page, memory "user/pref/lang"\n\nWho wrote it?']]);
    expect(screen.getByRole("list", { name: "Conversation" })).toHaveTextContent(
      "Includes: Memories · key user/pref/lang",
    );
    expect(chipText()).toBe("Includes: Memories");
  });

  it("the item goes with the chip it was added to", async () => {
    signIn("viewer");
    show();
    chat.push(() => answer("Ada."));
    act(() => askAbout(item()));
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Don't include this page" }));
    await askIt("Who?");
    await screen.findByText("Ada.");
    expect(sent).toEqual([["Who?"]]);
  });

  it("an item from another page or namespace rides along with nothing", async () => {
    signIn("viewer");
    await loadDockRuntime();
    show();
    act(() => askAbout(item()));
    await waitFor(() => expect(chipText()).toBe("Includes: Memories · key user/pref/lang"));
    act(() => useStore.setState({ view: "knowledge" }));
    expect(chipText()).toBe("Includes: Library");
    expect(useDock.getState().item).toBeNull();
    // A chip removed on one page comes back on the next.
    await userEvent.setup().click(screen.getByRole("button", { name: "Don't include this page" }));
    expect(screen.queryByTestId("assistant-page-chip")).not.toBeInTheDocument();
    act(() => useStore.setState({ view: "recall" }));
    expect(chipText()).toBe("Includes: Recall");
  });

  it("keeps the question being typed", async () => {
    signIn("viewer");
    useDock.setState({ draft: "Who wrote" });
    show();
    act(() => askAbout(item()));
    await waitFor(() => expect(chipText()).toBe("Includes: Memories · key user/pref/lang"));
    expect(screen.getByLabelText("Question")).toHaveValue("Who wrote");
  });

  it("on a conversation about another namespace, starts one in the header's, with the draft and the chip", async () => {
    signIn("viewer");
    show();
    chat.push(() => answer("Ada."));
    await askIt("Who owns billing?");
    await screen.findByText("Ada.");
    act(() =>
      useStore.setState({
        identity: { org: "acme", namespace: "ops", agent: "loom-ui" },
        myNamespaces: [
          { namespace: "team", role: "reader", status: "active", kind: "memory" } as never,
          { namespace: "ops", role: "reader", status: "active", kind: "memory" } as never,
        ],
      }),
    );
    expect(screen.getByText("This conversation is about team.")).toBeInTheDocument();
    act(() => useDock.setState({ draft: "and the refunds?" }));
    act(() => askAbout(item({ namespace: "ops" })));
    const line = "New conversation in ops; the one about team is in History.";
    expect(await screen.findByText(line)).toBeInTheDocument();
    expect(screen.queryByText("Ada.")).not.toBeInTheDocument();
    expect(screen.queryByText("This conversation is about team.")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Question")).toHaveValue("and the refunds?");
    expect(chipText()).toBe("Includes: Memories · key user/pref/lang");
    await waitFor(() => expect(document.getElementById("loom-assistant-status")).toHaveTextContent(line));
    await userEvent.setup().click(screen.getByRole("button", { name: "History" }));
    expect(screen.getByRole("button", { name: "Who owns billing?" })).toBeInTheDocument();
  });

  it("on a conversation kept in a collection, points to no History entry and names no address", async () => {
    signIn("viewer");
    show();
    act(() => useChat.setState({ namespace: "col-research", turns: [done(0)] }));
    act(() => askAbout(item({ namespace: "team" })));
    const line = "New conversation in team.";
    expect(await screen.findByText(line)).toBeInTheDocument();
    expect(screen.queryByText(/col-research/)).toBeNull();
    await waitFor(() => expect(document.getElementById("loom-assistant-status")).toHaveTextContent(line));
  });
});

describe("the page chip on a question that is not answered", () => {
  const item = (over: Partial<AskItem> = {}): AskItem => ({
    view: "memories",
    org: "acme",
    namespace: "team",
    chip: "key user/pref/lang",
    line: 'memory "user/pref/lang"',
    ...over,
  });
  const chip = () => screen.queryByTestId("assistant-page-chip")?.textContent ?? null;
  beforeEach(() => useStore.setState({ view: "memories" }));

  it("a refused question comes back with its item in the chip", async () => {
    signIn("viewer");
    show();
    chat.push(refusal(503, { code: "ASSISTANT_UNAVAILABLE", message: "not set up yet", reason: "starting" }));
    act(() => askAbout(item()));
    await askIt("Who wrote it?");
    await unavailable();
    expect(screen.getByLabelText("Question")).toHaveValue("Who wrote it?");
    expect(chip()).toBe("Includes: Memories · key user/pref/lang");
  });

  it("Try again sends what the chip shows now: removed since, nothing about the page", async () => {
    signIn("viewer");
    show();
    chat.push(refusal(503, { code: "ASSISTANT_UNAVAILABLE", message: "not set up yet", reason: "starting" }));
    act(() => askAbout(item()));
    await askIt("Who wrote it?");
    await unavailable();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Don't include this page" }));
    chat.push(() => answer("Ada."));
    await user.click(screen.getByRole("button", { name: "Try again" }));
    await screen.findByText("Ada.");
    expect(sent[0]).toEqual(['Context: Memories page, memory "user/pref/lang"\n\nWho wrote it?']);
    expect(sent[1]).toEqual(["Who wrote it?"]);
  });

  it("a refusal that names no reason gives back the question and its item", async () => {
    signIn("viewer");
    show();
    chat.push(refusal(429, { code: "RATE_LIMITED", message: "over the cap", retry_after_s: 60 }));
    act(() => askAbout(item()));
    await askIt("Who wrote it?");
    await screen.findByRole("alert");
    expect(screen.getByLabelText("Question")).toHaveValue("Who wrote it?");
    expect(chip()).toBe("Includes: Memories · key user/pref/lang");
  });

  it("a question asked without the chip comes back without it", async () => {
    signIn("viewer");
    show();
    chat.push(refusal(429, { code: "RATE_LIMITED", message: "over the cap", retry_after_s: 60 }));
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Don't include this page" }));
    await askIt("Who?");
    await screen.findByRole("alert");
    expect(chip()).toBeNull();
  });

  it("the item goes when the namespace, the org or the person changes", async () => {
    signIn("viewer");
    await loadDockRuntime();
    show();
    act(() => askAbout(item()));
    await waitFor(() => expect(chip()).toBe("Includes: Memories · key user/pref/lang"));
    act(() =>
      useStore.setState({
        identity: { org: "acme", namespace: "ops", agent: "loom-ui" },
        myNamespaces: [
          { namespace: "team", role: "reader", status: "active", kind: "memory" } as never,
          { namespace: "ops", role: "reader", status: "active", kind: "memory" } as never,
        ],
      }),
    );
    expect(useDock.getState().item).toBeNull();
    expect(chip()).toBe("Includes: Memories");

    act(() => useStore.setState({ identity: { org: "acme", namespace: "team", agent: "loom-ui" } }));
    act(() => askAbout(item()));
    act(() => useStore.setState({ identity: { org: "other", namespace: "team", agent: "loom-ui" } }));
    expect(useDock.getState().item).toBeNull();

    act(() => useStore.setState({ identity: { org: "acme", namespace: "team", agent: "loom-ui" } }));
    act(() => askAbout(item()));
    act(() =>
      useStore.setState({
        whoami: { authenticated: true, subject: "bob", org: "acme", namespaces: ["team"], roles: ["VIEWER"] },
      }),
    );
    expect(useDock.getState().item).toBeNull();
  });
});

describe("Ask about this, the edge cases", () => {
  const item = (over: Partial<AskItem> = {}): AskItem => ({
    view: "memories",
    org: "acme",
    namespace: "team",
    chip: "key user/pref/lang",
    line: 'memory "user/pref/lang"',
    ...over,
  });
  beforeEach(() => useStore.setState({ view: "memories" }));

  it("Try again sends nothing when the chip's line now pushes the question past the route's length", async () => {
    signIn("viewer");
    show();
    chat.push(refusal(503, { code: "ASSISTANT_UNAVAILABLE", message: "not set up yet", reason: "starting" }));
    await waitFor(() => expect(screen.queryByText("Getting the assistant ready…")).not.toBeInTheDocument());
    // Fits beside "Context: Memories page", not beside the item's longer line.
    const question = "x".repeat(19_970);
    act(() => useDock.setState({ draft: question }));
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Ask" }));
    await unavailable();
    expect(sent).toHaveLength(1);
    act(() => askAbout(item()));
    await user.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.queryByText("Getting the assistant ready…")).not.toBeInTheDocument());
    await new Promise((r) => setTimeout(r, 50));
    expect(sent).toHaveLength(1);
    const field = screen.getByLabelText("Question");
    expect(field).toHaveValue(question);
    expect(field).toHaveAttribute("aria-invalid", "true");
    // The field holds the refused question itself: it says why, and the panel adds nothing.
    expect(screen.queryByText(/^Not sent:/)).not.toBeInTheDocument();
  });

  it("with other text in the field, Try again over the length keeps it and says why nothing was sent", async () => {
    signIn("viewer");
    show();
    chat.push(refusal(503, { code: "ASSISTANT_UNAVAILABLE", message: "not set up yet", reason: "starting" }));
    await waitFor(() => expect(screen.queryByText("Getting the assistant ready…")).not.toBeInTheDocument());
    act(() => useDock.setState({ draft: "x".repeat(19_970) }));
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Ask" }));
    await unavailable();
    act(() => askAbout(item()));
    act(() => useDock.setState({ draft: "a newer question" }));
    await user.click(screen.getByRole("button", { name: "Try again" }));
    // "Context: Memories page, memory "user/pref/lang"" is 47 characters, and two line breaks.
    const line = "Not sent: with this page's line the question is longer than 19,951 characters.";
    expect(await screen.findByText(line)).toBeInTheDocument();
    await waitFor(() => expect(document.getElementById("loom-assistant-status")).toHaveTextContent(line));
    expect(sent).toHaveLength(1);
    expect(screen.getByLabelText("Question")).toHaveValue("a newer question");
  });

  it("a question waiting with sources counts their words in the length it says", async () => {
    signIn("viewer");
    show();
    chat.push(refusal(503, { code: "ASSISTANT_UNAVAILABLE", message: "not set up yet", reason: "starting" }));
    await waitFor(() => expect(screen.queryByText("Getting the assistant ready…")).not.toBeInTheDocument());
    act(() => useDock.setState({ draft: "x".repeat(19_970) }));
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Ask" }));
    await unavailable();
    const refused = useChat.getState().refused;
    if (!refused) throw new Error("the question should wait for Try again");
    act(() => useChat.setState({ refused: { ...refused, withSources: true } }));
    act(() => askAbout(item()));
    act(() => useDock.setState({ draft: "a newer question" }));
    await user.click(screen.getByRole("button", { name: "Try again" }));
    // 19,951 less " Cite your sources." (19 characters).
    const line = "Not sent: with this page's line the question is longer than 19,932 characters.";
    expect(await screen.findByText(line)).toBeInTheDocument();
    expect(sent).toHaveLength(1);
  });

  it("mid-answer after a namespace switch starts no conversation: the prompt shows and the item stays", async () => {
    signIn("viewer");
    show();
    // An answer that never ends while the test runs (the next test's reset stops it).
    chat.push(
      () =>
        new Response(new ReadableStream<Uint8Array>({ start() {} }), {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        }),
    );
    await askIt("Who owns billing?");
    await waitFor(() => expect(useChat.getState().answering).toBe(true));
    act(() =>
      useStore.setState({
        identity: { org: "acme", namespace: "ops", agent: "loom-ui" },
        myNamespaces: [
          { namespace: "team", role: "reader", status: "active", kind: "memory" } as never,
          { namespace: "ops", role: "reader", status: "active", kind: "memory" } as never,
        ],
      }),
    );
    act(() => askAbout(item({ namespace: "ops" })));
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.getByText("This conversation is about team.")).toBeInTheDocument();
    expect(screen.queryByText(/^New conversation in ops/)).not.toBeInTheDocument();
    expect(useDock.getState().item).toMatchObject({ namespace: "ops", chip: "key user/pref/lang" });
    expect(useChat.getState().namespace).toBe("team");
    act(() => resetChat(null));
  });
});

describe("the starters", () => {
  it("list in an empty conversation, and in the body that says why the assistant can't answer", async () => {
    signIn("viewer");
    show();
    expect(await screen.findByRole("list", { name: "Starters" })).toBeInTheDocument();
    cleanup();
    check = { ok: false, reason: "failing", required: false };
    show();
    await unavailable();
    expect(await screen.findByRole("list", { name: "Starters" })).toBeInTheDocument();
    // The assistant can't answer: the box is off and says why.
    expect(screen.getByRole("checkbox", { name: "Also ask the assistant" })).toBeDisabled();
  });

  it("the Insights button starts a new conversation over a finished one, so the starters show", async () => {
    signIn("viewer");
    saved([done(0)]);
    show();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "History" }));
    await user.click(screen.getByRole("button", { name: "Invoices" }));
    await screen.findByText("Answer 0");
    expect(screen.queryByRole("list", { name: "Starters" })).toBeNull();
    act(() => useDock.setState((s) => ({ startersSeq: (s.startersSeq ?? 0) + 1 })));
    expect(await screen.findByRole("list", { name: "Starters" })).toBeInTheDocument();
    expect(useChat.getState().turns).toEqual([]);
  });

  it("an old Insights link reads the audit card, nothing asked", async () => {
    signIn("nsAdmin");
    act(() => useDock.setState((s) => ({ startersSeq: (s.startersSeq ?? 0) + 1, startersAudit: true })));
    show();
    await waitFor(() => expect(paths).toContain("/v1/namespace.audit"));
    expect(paths).not.toContain("/v1/assistant/chat/completions");
    expect(useDock.getState().startersAudit).toBe(false);
    await waitFor(() => expect(useChat.getState().turns[0]?.state).toBe("card"));
    act(() => resetChat(null));
  });
});
