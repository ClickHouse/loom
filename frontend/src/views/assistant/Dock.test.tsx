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
import { useDock } from "../../lib/assistantDock";
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
let chat: Array<() => Response> = [];
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
  useDock.setState({ open: true, mounted: true, draft: "", reason: null, focusField: false, prefWidth: PANEL_DEFAULT });
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
        return next();
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

  it("keeps Ask off while the token's own claims still refuse", async () => {
    signIn("viewer", { read_scope: "own_session" });
    show();
    await unavailable();
    useChat.setState({ refused: "Who?" });
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
    expect(sent).toEqual([["Question 0", "Question 1", "Who?"]]);
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
    expect(sent).toEqual([["Question 0", "Who?"]]);
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
