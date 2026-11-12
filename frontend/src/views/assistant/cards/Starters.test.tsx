// The starters in the conversation: a starter reads its card with the person's token and,
// with "Also ask the assistant" on and the assistant able to answer, asks its question as the
// person's turn; the answer's steps show while it streams and under it once it ends; a card
// restored from History applies nothing until read again; a draft's action is the person's
// own click.
import { act, cleanup, render, screen, waitFor, within } from "../../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  MyNamespaceEntry,
  NamespaceAuditResult,
  NamespaceGuide,
  NamespaceTune,
  TuneProposal,
  WhoAmI,
} from "../../../api";
import { ConfirmViewport } from "../../../components/ConfirmDialog";
import { useDock } from "../../../lib/assistantDock";
import { resetNamespaceRoleCache } from "../../../lib/namespaceRole";
import { useStore } from "../../../lib/state";
import { axeViolations } from "../../../test/axe";
import { resetInsights } from "../../../test/insights";
import { AssistantChat } from "../Chat";
import { resetChat, useChat, type Turn } from "../chatStore";
import { DockBody } from "../Dock";
import { supportLabel } from "../Sources";
import { Starters } from "./Starters";
import { CARD_SNAPSHOT_MAX, storedCard } from "./types";
import { forgetCards, isFresh } from "./cardStore";

const AUDIT: NamespaceAuditResult = {
  org: "dev",
  namespace: "team",
  computed_at: "2026-09-19T08:00:00Z",
  window_seconds: 7 * 86_400,
  summary: {
    live_rows: 1,
    live_bytes: 1,
    excerpt_bytes: 0,
    version_rows: 1,
    version_bytes: 1,
    unembedded_rows: 0,
    gets: 0,
    searches: 0,
    sets: 0,
    get_search_ratio: 0,
    inspector_calls: 0,
  },
  findings: [
    {
      code: "exact_key_store",
      severity: "critical",
      count: 3,
      share: 0.5,
      bytes: 0,
      samples: [],
      detail: "",
      remedy: "",
    },
  ],
  skipped: [],
  healthy: false,
  criticals: 1,
  warnings: 2,
  infos: 0,
};

const draft = (over: Partial<TuneProposal>): TuneProposal => ({
  id: "p",
  target: "namespace.category",
  scope: "namespace dev/team",
  title: "A draft",
  rationale: "Why.",
  current: "",
  current_revision: null,
  proposed: "billing",
  evidence: { finding_codes: [], supporting_count: 1, supporting: [], counter_count: null },
  affected_agents: [],
  affected_agents_total: 0,
  effect: "future",
  historical: "",
  limits: "",
  apply: {
    op: "namespace.register_category",
    via: "op",
    args: { org: "dev", namespace: "team", name: "billing", description: "Memories tagged billing." },
    args_complete: true,
    required_role: "writer",
    guard: "",
  },
  ...over,
});
const CATEGORY = draft({ id: "cat", title: "Register billing" });
const GUIDE = draft({
  id: "guide",
  target: "namespace.guide",
  title: "Name the prefixes",
  current: "old",
  current_revision: 2,
  proposed: "new guide",
  apply: {
    op: "namespace.guide_set",
    via: "op",
    args: { org: "dev", namespace: "team", content: "new guide", expected_revision: 2, note: "n" },
    args_complete: true,
    required_role: "writer",
    guard: "",
  },
});
const ENV = draft({
  id: "env",
  target: "settings.knowledge_chunking",
  title: "Chunk smaller",
  apply: {
    op: "LOOM_KNOWLEDGE_CHUNK_SIZE",
    via: "environment",
    args: { LOOM_KNOWLEDGE_CHUNK_SIZE: "800" },
    args_complete: true,
    required_role: "platform_admin",
    guard: "",
  },
});
const TUNE: NamespaceTune = {
  org: "dev",
  namespace: "team",
  computed_at: "2026-09-19T08:00:00Z",
  proposals: [CATEGORY, GUIDE, ENV],
  guide_revision: 2,
  categories_registered: 0,
  extraction_policy: null,
  extraction_policy_unavailable: "",
  skipped: [],
  model_used: false,
};
const guide = (revision: number): NamespaceGuide => ({
  found: true,
  revision,
  content: `guide at ${revision}`,
  summary: "",
  note: "",
  updated_by: "ada",
  updated_at: null,
});

const auditNamespace = vi.fn(async () => AUDIT);
const tuneNamespace = vi.fn(async () => TUNE);
const getNamespaceGuide = vi.fn(async () => guide(2));
vi.mock("../../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../api")>();
  return {
    ...actual,
    listMine: vi.fn(async () => []),
    isAuthDisabled: () => false,
    auditNamespace: () => auditNamespace(),
    tuneNamespace: () => tuneNamespace(),
    getNamespaceGuide: () => getNamespaceGuide(),
  };
});
const registerCategory = vi.fn(async () => ({ category_id: "c", embedded: true }));
vi.mock("../../../api/structure", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../api/structure")>()),
  registerCategory: (...a: unknown[]) => registerCategory(...(a as [])),
}));

const enc = new TextEncoder();
/** The chat route's answer, fed event by event. */
function sse() {
  let ctl!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({ start: (c) => void (ctl = c) });
  const send = (o: object) => ctl.enqueue(enc.encode(`data: ${JSON.stringify(o)}\n\n`));
  return {
    response: new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
    send,
    done: () => {
      ctl.enqueue(enc.encode("data: [DONE]\n\n"));
      ctl.close();
    },
  };
}
let sent: Array<{ messages: { role: string; content: string }[] }> = [];
let answer: ReturnType<typeof sse> | null = null;

const row = (role: MyNamespaceEntry["role"]): MyNamespaceEntry => ({
  namespace: "team",
  role,
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
});
const writer: WhoAmI = { authenticated: true, subject: "ada", org: "dev", roles: ["USER"], namespaces: ["team"] };

beforeEach(() => {
  resetInsights();
  vi.clearAllMocks();
  resetNamespaceRoleCache();
  sent = [];
  answer = null;
  useStore.setState({
    authed: true,
    whoami: writer,
    myNamespaces: [row("writer")],
    identity: { org: "dev", namespace: "team", agent: "loom-ui" },
    view: "memories",
  });
  useDock.setState({ draft: "", working: false, open: true, item: null, pageOff: true });
  localStorage.setItem("loom_token_v1", "session-token");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: string, init: RequestInit) => {
      sent.push(JSON.parse(String(init.body)));
      answer = sse();
      return answer.response;
    }),
  );
});
afterEach(() => {
  cleanup();
  resetInsights();
  vi.unstubAllGlobals();
  localStorage.clear();
});

const AUDIT_STARTER = "How is this namespace being used?";
const TUNE_STARTER = "What could be improved here?";

/** The panel's conversation with the starters above it, and the assistant's live region. */
function Panel({ cannotAnswer = null, phone = false }: { cannotAnswer?: string | null; phone?: boolean }) {
  const turns = useChat((s) => s.turns);
  return (
    <>
      {turns.length === 0 && <Starters namespace="team" phone={phone} cannotAnswer={cannotAnswer} />}
      <AssistantChat namespace="team" blocked={cannotAnswer} />
      <p id="loom-assistant-status" role="status" />
      <ConfirmViewport />
    </>
  );
}

const status = () => document.getElementById("loom-assistant-status")?.textContent ?? "";

describe("a starter that asks the assistant", () => {
  it("reads its card and asks its question, naming the card's arguments, as the person's turn", async () => {
    const { container } = render(<Panel />);
    const box = screen.getByRole("checkbox", { name: "Also ask the assistant" });
    expect(box).toBeChecked();
    expect(box).toHaveAccessibleDescription(/calls a model/);
    // Off, a starter reads only Loom, and a typed question still calls a model.
    await userEvent.click(box);
    expect(box).toHaveAccessibleDescription(
      "A starter then reads only Loom: no model is called. A question typed below still calls one.",
    );
    await userEvent.click(box);
    await userEvent.click(await screen.findByRole("button", { name: AUDIT_STARTER }));
    await waitFor(() => expect(sent).toHaveLength(1));
    const question = sent[0].messages.at(-1)?.content;
    expect(question).toBe("How is this namespace being used over the last 7 days?");
    expect(auditNamespace).toHaveBeenCalledTimes(1);
    // The card lands above the answer, and its line is said; the focus stays where it was.
    const card = await screen.findByTestId("assistant-card");
    expect(await within(card).findByTestId("audit-summary")).toBeInTheDocument();
    await waitFor(() => expect(status()).toBe("Audit: 1 critical, 2 warnings, 0 info."));
    expect(card).not.toContainElement(document.activeElement as HTMLElement);
    // The working line, then the latest step; steps are not said.
    expect(screen.getByText("Answering…")).toBeInTheDocument();
    await act(async () =>
      answer?.send({
        object: "loom.step",
        id: "",
        choices: [],
        steps: [{ op: "namespace.audit", text: "Read the audit", ok: true }],
      }),
    );
    expect(await screen.findByText("Read the audit")).toBeInTheDocument();
    expect(status()).toBe("Audit: 1 critical, 2 warnings, 0 info.");
    await act(async () => {
      answer?.send({ id: "msg-1", choices: [{ delta: { content: "Three exact-key reads." } }] });
      answer?.send({
        object: "loom.citations",
        id: "msg-1",
        choices: [],
        citations: [],
        support: "unchecked",
        steps: [
          { op: "namespace.audit", text: "Read the audit", ok: true },
          { op: "memory.search", text: "Couldn't search memories", ok: false },
        ],
        steps_cut: true,
      });
      answer?.done();
    });
    // The label is the one every answer gets, for the support the event carries.
    const label = supportLabel("unchecked", [])?.text ?? "";
    expect(await screen.findByText(label)).toBeInTheDocument();
    const steps = screen.getByRole("button", { name: "What it read (2+)" });
    expect(steps).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(steps);
    const list = screen.getByTestId("assistant-steps");
    expect(
      within(list)
        .getAllByRole("listitem")
        .map((li) => li.textContent),
    ).toEqual(["Read the audit", "Couldn't search memories", "and more"]);
    // A refused step is muted, not an error.
    expect(within(list).getByText("Couldn't search memories")).toHaveClass("text-text-muted");
    expect(await axeViolations(container)).toEqual([]);
  });

  it("reads its card again on Retry and on an edit of its question, answering the same turn", async () => {
    render(<Panel />);
    await userEvent.click(await screen.findByRole("button", { name: AUDIT_STARTER }));
    await waitFor(() => expect(sent).toHaveLength(1));
    await screen.findByTestId("audit-summary");
    await act(async () => {
      answer?.send({ id: "msg-1", choices: [{ delta: { content: "Three exact-key reads." } }] });
      answer?.done();
    });
    await userEvent.click(await screen.findByRole("button", { name: "Retry" }));
    await waitFor(() => expect(sent).toHaveLength(2));
    expect(sent[1].messages).toEqual([
      { role: "user", content: "How is this namespace being used over the last 7 days?" },
    ]);
    await waitFor(() => expect(auditNamespace).toHaveBeenCalledTimes(2));
    await act(async () => {
      answer?.send({ id: "msg-2", choices: [{ delta: { content: "Two exact-key reads." } }] });
      answer?.done();
    });
    await waitFor(() => expect(useChat.getState().answering).toBe(false));

    await userEvent.click(screen.getByRole("button", { name: "Edit your question" }));
    const field = screen.getByLabelText("Question");
    await userEvent.clear(field);
    await userEvent.type(field, "Which reads are exact-key?");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    await waitFor(() => expect(sent).toHaveLength(3));
    await waitFor(() => expect(auditNamespace).toHaveBeenCalledTimes(3));
    const turns = useChat.getState().turns;
    expect(turns).toHaveLength(1);
    expect(turns[0].question).toBe("Which reads are exact-key?");
    expect(turns[0].card?.op).toBe("namespace.audit");
    expect(await screen.findByTestId("audit-summary")).toBeInTheDocument();
  });

  it("puts a starter's turn back as it was when its Retry is refused, and Try again reads its card again", async () => {
    // The panel's body, which says why the assistant can't answer and offers Try again.
    const refusals: Response[] = [];
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const json = (body: unknown, status = 200) =>
        new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
      if (url.startsWith("/v1/health/ready")) return json({ ready: true, checks: { assistant: { ok: true } } });
      if (url.startsWith("/v1/org.model_calls")) return json({ enabled: false, locked: false });
      if (!url.startsWith("/v1/assistant/chat")) return json({});
      sent.push(JSON.parse(String(init?.body)));
      const refused = refusals.shift();
      if (refused) return refused;
      answer = sse();
      return answer.response;
    });
    render(
      <>
        <DockBody />
        <p id="loom-assistant-status" role="status" />
      </>,
    );
    await userEvent.click(await screen.findByRole("button", { name: AUDIT_STARTER }));
    await waitFor(() => expect(sent).toHaveLength(1));
    await screen.findByTestId("audit-summary");
    await act(async () => {
      answer?.send({ id: "msg-1", choices: [{ delta: { content: "Three exact-key reads." } }] });
      answer?.done();
    });
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    refusals.push(
      new Response(
        JSON.stringify({ detail: { code: "ASSISTANT_UNAVAILABLE", message: "not yet", reason: "starting" } }),
        { status: 503, headers: { "Content-Type": "application/json" } },
      ),
    );
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    const tryAgain = await screen.findByRole("button", { name: "Try again" });
    const turn = useChat.getState().turns[0];
    expect(turn.answer).toBe("Three exact-key reads.");
    expect(turn.card?.op).toBe("namespace.audit");
    expect(turn.card?.again).toBeUndefined();
    // The card read itself again as the Retry started, before the refusal.
    expect(auditNamespace).toHaveBeenCalledTimes(2);

    await userEvent.click(tryAgain);
    await waitFor(() => expect(sent).toHaveLength(3));
    await waitFor(() => expect(auditNamespace).toHaveBeenCalledTimes(3));
    expect(useChat.getState().turns).toHaveLength(1);
    expect(useChat.getState().turns[0].id).toBe(turn.id);
  });

  it("drops a card read a refused Retry started, so the turn put back keeps its card", async () => {
    render(<Panel />);
    await userEvent.click(await screen.findByRole("button", { name: AUDIT_STARTER }));
    await waitFor(() => expect(sent).toHaveLength(1));
    await screen.findByTestId("audit-summary");
    await act(async () => {
      answer?.send({ id: "msg-1", choices: [{ delta: { content: "Three exact-key reads." } }] });
      answer?.done();
    });
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    // The read the Retry starts lands only after the refusal.
    let land!: (r: NamespaceAuditResult) => void;
    auditNamespace.mockImplementationOnce(() => new Promise<NamespaceAuditResult>((r) => (land = r)));
    vi.mocked(fetch).mockImplementationOnce(
      async () =>
        new Response(
          JSON.stringify({ detail: { code: "ASSISTANT_UNAVAILABLE", message: "not yet", reason: "starting" } }),
          { status: 503, headers: { "Content-Type": "application/json" } },
        ),
    );
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(auditNamespace).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(useChat.getState().hint).toEqual({ kind: "starting" }));
    await act(async () => land({ ...AUDIT, criticals: 9 }));
    const card = useChat.getState().turns[0].card;
    expect((card?.data as NamespaceAuditResult | undefined)?.criticals).toBe(1);
    expect(card?.busy).toBeFalsy();
  });

  it("keeps a card read that landed before the route refused the Retry, its data and its freshness one", async () => {
    render(<Panel />);
    await userEvent.click(await screen.findByRole("button", { name: AUDIT_STARTER }));
    await waitFor(() => expect(sent).toHaveLength(1));
    await screen.findByTestId("audit-summary");
    await act(async () => {
      answer?.send({ id: "msg-1", choices: [{ delta: { content: "Three exact-key reads." } }] });
      answer?.done();
    });
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    const turnId = useChat.getState().turns[0].id;
    // As after a reload: the card shows its stored snapshot, not a read of this visit.
    forgetCards();
    expect(isFresh(turnId)).toBe(false);
    auditNamespace.mockImplementationOnce(async () => ({ ...AUDIT, criticals: 9 }));
    // The route's refusal comes only once the card's read landed.
    let refuse!: () => void;
    vi.mocked(fetch).mockImplementationOnce(
      () =>
        new Promise<Response>((r) => {
          refuse = () =>
            r(
              new Response(
                JSON.stringify({ detail: { code: "ASSISTANT_UNAVAILABLE", message: "not yet", reason: "starting" } }),
                { status: 503, headers: { "Content-Type": "application/json" } },
              ),
            );
        }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(isFresh(turnId)).toBe(true));
    await act(async () => refuse());
    await waitFor(() => expect(useChat.getState().hint).toEqual({ kind: "starting" }));
    const card = useChat.getState().turns[0].card;
    expect((card?.data as NamespaceAuditResult | undefined)?.criticals).toBe(9);
    expect(card?.again).toBeUndefined();
    expect(isFresh(turnId)).toBe(true);
  });

  it("keeps a card's failed read when a Retry's read is still in flight as the route refuses", async () => {
    auditNamespace.mockImplementationOnce(async () => {
      throw new Error("boom");
    });
    render(<Panel />);
    await userEvent.click(await screen.findByRole("button", { name: AUDIT_STARTER }));
    await waitFor(() => expect(sent).toHaveLength(1));
    await waitFor(() => expect(useChat.getState().turns[0].card?.failed).toBe(true));
    const error = useChat.getState().turns[0].card?.error;
    await act(async () => {
      answer?.send({ id: "msg-1", choices: [{ delta: { content: "Three exact-key reads." } }] });
      answer?.done();
    });
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    auditNamespace.mockImplementationOnce(() => new Promise<NamespaceAuditResult>(() => {}));
    vi.mocked(fetch).mockImplementationOnce(
      async () =>
        new Response(
          JSON.stringify({ detail: { code: "ASSISTANT_UNAVAILABLE", message: "not yet", reason: "starting" } }),
          { status: 503, headers: { "Content-Type": "application/json" } },
        ),
    );
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(auditNamespace).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(useChat.getState().hint).toEqual({ kind: "starting" }));
    const card = useChat.getState().turns[0].card;
    expect(card?.error).toBe(error);
    expect(card?.failed).toBe(true);
    expect(card?.busy).toBeFalsy();
  });

  it("leaves no card busy when its first read was still running as a refused Retry put the turn back", async () => {
    // The starter's own read and the Retry's read never land.
    auditNamespace.mockImplementationOnce(() => new Promise<NamespaceAuditResult>(() => {}));
    auditNamespace.mockImplementationOnce(() => new Promise<NamespaceAuditResult>(() => {}));
    render(<Panel />);
    await userEvent.click(await screen.findByRole("button", { name: AUDIT_STARTER }));
    await waitFor(() => expect(sent).toHaveLength(1));
    await waitFor(() => expect(useChat.getState().turns[0].card?.busy).toBe(true));
    await act(async () => {
      answer?.send({ id: "msg-1", choices: [{ delta: { content: "Three exact-key reads." } }] });
      answer?.done();
    });
    await waitFor(() => expect(useChat.getState().answering).toBe(false));
    vi.mocked(fetch).mockImplementationOnce(
      async () =>
        new Response(
          JSON.stringify({ detail: { code: "ASSISTANT_UNAVAILABLE", message: "not yet", reason: "starting" } }),
          { status: 503, headers: { "Content-Type": "application/json" } },
        ),
    );
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(auditNamespace).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(useChat.getState().hint).toEqual({ kind: "starting" }));
    expect(useChat.getState().turns[0].card?.busy).toBeFalsy();
    // Read again reads it, and the card shows the read.
    await userEvent.click(screen.getByRole("button", { name: "Read again" }));
    expect(await screen.findByTestId("audit-summary")).toBeInTheDocument();
    expect(useChat.getState().turns[0].card?.busy).toBeFalsy();
  });

  it("remembers the box per viewer, and with it off reads the card alone", async () => {
    render(<Panel />);
    await userEvent.click(screen.getByRole("checkbox", { name: "Also ask the assistant" }));
    expect(localStorage.getItem("loom_assistant_also_ask")).toBe("false");
    await userEvent.click(await screen.findByRole("button", { name: AUDIT_STARTER }));
    expect(await screen.findByTestId("audit-summary")).toBeInTheDocument();
    expect(sent).toHaveLength(0);
    // The turn is the card alone: no answer slot, and it is not sent with a later question.
    expect(screen.queryByText("Answering…")).toBeNull();
    expect(useChat.getState().turns[0].state).toBe("card");
  });

  it("when the assistant cannot answer, the box is off and says why, and the card says so", async () => {
    render(<Panel cannotAnswer="model calls are off" />);
    const box = screen.getByRole("checkbox", { name: "Also ask the assistant" });
    expect(box).toBeDisabled();
    // Nothing can be typed and sent then, so the line says only what a starter does.
    expect(box).toHaveAccessibleDescription(
      "The assistant can't answer: model calls are off. A starter reads only Loom: no model is called.",
    );
    await userEvent.click(await screen.findByRole("button", { name: AUDIT_STARTER }));
    expect(
      await screen.findByText("The assistant can't answer: model calls are off. This is Loom's own read."),
    ).toBeInTheDocument();
    expect(sent).toHaveLength(0);
  });
});

describe("the phone sheet", () => {
  it("puts the focus on the first new starter even with the subject form open", async () => {
    render(<Panel phone />);
    await userEvent.click(await screen.findByRole("button", { name: "What does a user, agent or session remember?" }));
    await userEvent.click(screen.getByRole("button", { name: "More starters" }));
    expect(screen.getByRole("button", { name: "What would Loom advise an agent writing here?" })).toHaveFocus();
  });

  it("shows four starters, and More starters shows the rest with the focus on the first of them", async () => {
    render(<Panel phone />);
    const list = await screen.findByRole("list", { name: "Starters" });
    expect(
      within(list)
        .getAllByRole("button")
        .map((b) => b.textContent),
    ).toEqual([
      "What does this namespace hold, and who appears in it?",
      AUDIT_STARTER,
      "What does a user, agent or session remember?",
      "What documents does it hold?",
    ]);
    await userEvent.click(screen.getByRole("button", { name: "More starters" }));
    expect(within(list).getAllByRole("button")).toHaveLength(7);
    expect(screen.getByRole("button", { name: "What would Loom advise an agent writing here?" })).toHaveFocus();
    expect(screen.queryByRole("button", { name: "More starters" })).toBeNull();
  });
});

describe("a starter beside a question being typed", () => {
  it("leaves the field's text and its chip as they were", async () => {
    const item = { kind: "memory", label: "billing/invoices", line: "Memory: billing/invoices" } as never;
    useDock.setState({ draft: "Why is this stale?", item, pageOff: false });
    render(<Panel />);
    await userEvent.click(await screen.findByRole("button", { name: AUDIT_STARTER }));
    await waitFor(() => expect(sent).toHaveLength(1));
    // The starter asked its own question, and the person's draft and chip stay for theirs.
    expect(sent[0].messages.at(-1)?.content).toBe("How is this namespace being used over the last 7 days?");
    expect(useDock.getState()).toMatchObject({ draft: "Why is this stale?", item });
  });
});

describe("a starter whose question is refused", () => {
  it("keeps its card as a turn of its own, saying why, and puts nothing in the field", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ detail: { code: "RATE_LIMITED", message: "slow down" } }, { status: 429 })),
    );
    render(<Panel />);
    await userEvent.click(await screen.findByRole("button", { name: AUDIT_STARTER }));
    const card = await screen.findByTestId("assistant-card");
    expect(await within(card).findByTestId("audit-summary")).toBeInTheDocument();
    expect(
      // The route's own words, so a wait and try again is said.
      within(card).getByText(/^The assistant can't answer: .*slow down.*\. This is Loom's own read\.$/),
    ).toBeInTheDocument();
    expect(useChat.getState().turns.map((t) => t.state)).toEqual(["card"]);
    expect(useChat.getState().id).not.toBeNull();
    expect(useDock.getState().draft).toBe("");
    // The turn became the card in place: its card was read once.
    expect(auditNamespace).toHaveBeenCalledTimes(1);
  });
});

describe("a starter while another tab answers in the conversation", () => {
  /** A Locks API where another tab holds every lock, granting nothing; `hold` delays the
   *  refusal until it is called. */
  function lockedElsewhere() {
    let release: () => void = () => {};
    const wait = new Promise<void>((r) => (release = r));
    let held = false;
    vi.stubGlobal("navigator", {
      ...navigator,
      locks: {
        request: async (_name: string, _opts: unknown, cb: (lock: unknown) => unknown) => {
          if (held) await wait;
          return cb(null);
        },
        query: async () => ({ held: [], pending: [] }),
      },
    });
    return {
      hold: () => {
        held = true;
        return release;
      },
    };
  }

  it("adds the card alone, saying why, read once, and asks nothing", async () => {
    lockedElsewhere();
    render(<Panel />);
    await userEvent.click(await screen.findByRole("button", { name: AUDIT_STARTER }));
    const card = await screen.findByTestId("assistant-card");
    expect(await within(card).findByTestId("audit-summary")).toBeInTheDocument();
    expect(
      within(card).getByText(
        "The assistant can't answer: another tab is answering in this conversation. This is Loom's own read.",
      ),
    ).toBeInTheDocument();
    expect(useChat.getState().turns.map((t) => t.state)).toEqual(["card"]);
    expect(auditNamespace).toHaveBeenCalledTimes(1);
    expect(sent).toHaveLength(0);
  });

  it("adds nothing when the chat was reset while the lock was asked for", async () => {
    const release = lockedElsewhere().hold();
    render(<Panel />);
    await userEvent.click(await screen.findByRole("button", { name: AUDIT_STARTER }));
    // A sign-out, say, lands before the refusal.
    act(() => resetChat(null));
    await act(async () => release());
    expect(useChat.getState().turns).toEqual([]);
    expect(auditNamespace).not.toHaveBeenCalled();
  });
});

describe("an answer's steps", () => {
  it("keeps the steps shown while it ran when the last read of the run gives none", async () => {
    render(<Panel />);
    await userEvent.click(await screen.findByRole("button", { name: AUDIT_STARTER }));
    await waitFor(() => expect(sent).toHaveLength(1));
    await act(async () =>
      answer?.send({
        object: "loom.step",
        id: "",
        choices: [],
        steps: [{ op: "namespace.audit", text: "Read the audit", ok: true }],
      }),
    );
    expect(await screen.findByText("Read the audit")).toBeInTheDocument();
    await act(async () => {
      answer?.send({ id: "msg-1", choices: [{ delta: { content: "Three exact-key reads." } }] });
      // The server's read of the run failed or ran past its bound: the event carries no steps.
      answer?.send({ object: "loom.citations", id: "msg-1", choices: [], citations: [], support: "unchecked" });
      answer?.done();
    });
    await userEvent.click(await screen.findByRole("button", { name: "What it read (1)" }));
    expect(within(screen.getByTestId("assistant-steps")).getByText("Read the audit")).toBeInTheDocument();
  });

  it("drops the live steps when the last read of the run finds no call", async () => {
    render(<Panel />);
    await userEvent.click(await screen.findByRole("button", { name: AUDIT_STARTER }));
    await waitFor(() => expect(sent).toHaveLength(1));
    await act(async () =>
      answer?.send({
        object: "loom.step",
        id: "",
        choices: [],
        // Another tab's answer, read before this one's run id was known.
        steps: [{ op: "memory.search", text: "Searched memories", ok: true }],
      }),
    );
    expect(await screen.findByText("Searched memories")).toBeInTheDocument();
    await act(async () => {
      answer?.send({ id: "msg-1", choices: [{ delta: { content: "Nothing to read." } }] });
      answer?.send({ object: "loom.citations", id: "msg-1", choices: [], citations: [], support: "none", steps: [] });
      answer?.done();
    });
    await screen.findByText("Nothing to read.");
    await waitFor(() => expect(screen.queryByRole("button", { name: /^What it read/ })).toBeNull());
  });
});

describe("a card of another namespace than the header's", () => {
  it("shows a tune draft's cited memories as text, opening no drawer", async () => {
    const readAt = Date.UTC(2026, 8, 19, 8, 0);
    const cited = draft({
      id: "cited",
      title: "Cite billing",
      evidence: {
        finding_codes: [],
        supporting_count: 1,
        supporting: [
          { memory_id: "m1", memory_key: "billing/invoices", agent: "a", user_id: "-", session_id: "-" } as never,
        ],
        counter_count: null,
      },
    });
    const turn: Turn = {
      id: "tune",
      question: TUNE_STARTER,
      answer: "",
      state: "card",
      card: { op: "namespace.tune", args: {}, data: { ...TUNE, proposals: [cited] }, readAt },
      at: readAt,
    };
    useChat.setState({ id: "c1", namespace: "team", turns: [turn] });
    useStore.setState({ identity: { org: "dev", namespace: "other", agent: "loom-ui" } });
    render(<Panel />);
    const card = await screen.findByTestId("assistant-card");
    expect(within(card).getByText("billing/invoices")).toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: "Open memory billing/invoices" })).toBeNull();
  });

  it("opens nothing, since every page and the drawer show the header's namespace", async () => {
    const readAt = Date.UTC(2026, 8, 19, 8, 0);
    const turn: Turn = {
      id: "audit",
      question: AUDIT_STARTER,
      answer: "",
      state: "card",
      card: { op: "namespace.audit", args: {}, data: AUDIT, readAt },
      at: readAt,
    };
    useChat.setState({ id: "c1", namespace: "team", turns: [turn] });
    useStore.setState({ identity: { org: "dev", namespace: "other", agent: "loom-ui" } });
    render(<Panel />);
    const card = await screen.findByTestId("assistant-card");
    expect(within(card).getByText("Read in team: switch to it to open what this card names.")).toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: "Open in Memories" })).toBeNull();
    // The same card in its own namespace links to the page.
    act(() => useStore.setState({ identity: { org: "dev", namespace: "team", agent: "loom-ui" } }));
    expect(await within(card).findByRole("button", { name: "Open in Memories" })).toBeInTheDocument();
  });
});

describe("the tune card's actions", () => {
  async function openTune() {
    render(<Panel cannotAnswer="model calls are off" />);
    await userEvent.click(await screen.findByRole("button", { name: TUNE_STARTER }));
    return screen.findByTestId("assistant-review");
  }

  it("registers a category on the person's click, after a confirmation", async () => {
    const review = await openTune();
    await userEvent.click(within(review).getByRole("button", { name: "Register this category" }));
    expect(await screen.findByText(/Register the category billing\?/)).toBeInTheDocument();
    expect(registerCategory).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Register" }));
    await waitFor(() =>
      expect(registerCategory).toHaveBeenCalledWith(
        { org: "dev", namespace: "team" },
        "billing",
        "Memories tagged billing.",
      ),
    );
  });

  it("puts each draft's heading one level under the card's", async () => {
    const review = await openTune();
    const card = screen.getByTestId("assistant-card");
    const cardHeading = within(card).getAllByRole("heading")[0];
    const cardLevel = Number(cardHeading.tagName.slice(1));
    const draftHeading = within(review).getByRole("heading", { name: "Register billing" });
    expect(Number(draftHeading.tagName.slice(1))).toBe(cardLevel + 1);
  });

  it("copies a deployment draft, and says who applies it", async () => {
    const review = await openTune();
    const env = within(review).getByRole("listitem", { name: "Chunk smaller" });
    expect(within(env).getByRole("button", { name: "Copy the change" })).toBeInTheDocument();
    expect(within(env).getByTestId("how-to-apply")).toHaveTextContent("A platform admin sets the variable below");
  });

  it("offers no apply to a viewer", async () => {
    useStore.setState({ myNamespaces: [row("viewer")] });
    resetNamespaceRoleCache();
    const review = await openTune();
    const register = within(review).getByRole("button", { name: "Register this category" });
    expect(register).toHaveAttribute("aria-disabled", "true");
    await userEvent.click(register);
    expect(registerCategory).not.toHaveBeenCalled();
    // The guide draft beside it says the same, from the same gate.
    const guide = within(review).getByRole("button", { name: "Review in the guide editor" });
    expect(guide).toHaveAttribute("aria-disabled", "true");
    expect(guide).toHaveAccessibleDescription(/^Needs writer on .+\. An admin of it can grant that\.$/);
  });

  it("follows the person's role in the conversation's namespace, not the header's", async () => {
    // A writer in the header's namespace and a viewer in the one the card was read in.
    useStore.setState({
      identity: { org: "dev", namespace: "other", agent: "loom-ui" },
      myNamespaces: [row("viewer"), { ...row("writer"), namespace: "other" }],
    });
    resetNamespaceRoleCache();
    const review = await openTune();
    const register = within(review).getByRole("button", { name: "Register this category" });
    expect(register).toHaveAttribute("aria-disabled", "true");
    await userEvent.click(register);
    expect(registerCategory).not.toHaveBeenCalled();
  });

  it("opens a guide draft in the guide editor, on the conflict view when the guide moved since the read", async () => {
    getNamespaceGuide.mockResolvedValueOnce(guide(3));
    const review = await openTune();
    await userEvent.click(within(review).getByRole("button", { name: "Review in the guide editor" }));
    expect(await screen.findByLabelText("Guide (Markdown)")).toHaveValue("new guide");
    expect(screen.getByRole("alert")).toHaveTextContent("Revision 3 by ada was saved while you edited");
    expect(screen.getByText("guide at 3")).toBeInTheDocument();
  });

  it("opens a guide draft on the editor with no conflict when the guide is the one it was drafted on", async () => {
    const review = await openTune();
    await userEvent.click(within(review).getByRole("button", { name: "Review in the guide editor" }));
    expect(await screen.findByLabelText("Guide (Markdown)")).toHaveValue("new guide");
    // The draft's own "What changed" line comes with it.
    expect(screen.getByLabelText("What changed")).toHaveValue("n");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("applies nothing from a card restored from History until it is read again", async () => {
    // A stored conversation's card: read in another visit.
    const readAt = Date.UTC(2026, 8, 19, 8, 0);
    const turn: Turn = {
      id: "restored",
      question: "What could be improved in this namespace?",
      answer: "",
      state: "card",
      card: { op: "namespace.tune", args: {}, data: TUNE, readAt },
      at: readAt,
    };
    useChat.setState({ id: "c1", namespace: "team", turns: [turn] });
    render(<Panel cannotAnswer="model calls are off" />);
    const card = await screen.findByTestId("assistant-card");
    expect(within(card).getByTestId("assistant-card-provenance")).toHaveTextContent(/^Read /);
    const register = within(card).getByRole("button", { name: "Register this category" });
    expect(register).toHaveAttribute("aria-disabled", "true");
    expect(register).toHaveAccessibleDescription(/^Read again to apply; this read is from /);
    await userEvent.click(within(card).getByRole("button", { name: "Read again" }));
    await waitFor(() => expect(tuneNamespace).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(within(card).getByRole("button", { name: "Register this category" })).not.toHaveAttribute("aria-disabled"),
    );
    expect(within(card).getByTestId("assistant-card-provenance")).toHaveTextContent("Loom's own read");
  });
});

describe("two cards of one op in a conversation", () => {
  it("keep their ids their own", async () => {
    const readAt = Date.UTC(2026, 8, 19, 8, 0);
    const cardTurn = (id: string, op: "namespace.audit" | "namespace.tune", data: unknown): Turn => ({
      id,
      question: op === "namespace.audit" ? AUDIT_STARTER : TUNE_STARTER,
      answer: "",
      state: "card",
      card: { op, args: {}, data, readAt },
      at: readAt,
    });
    useChat.setState({
      id: "c1",
      namespace: "team",
      turns: [
        cardTurn("a1", "namespace.audit", AUDIT),
        cardTurn("a2", "namespace.audit", AUDIT),
        cardTurn("t1", "namespace.tune", TUNE),
        cardTurn("t2", "namespace.tune", TUNE),
      ],
    });
    const { container } = render(<Panel />);
    await waitFor(() => expect(screen.getAllByTestId("assistant-card")).toHaveLength(4));
    const ids = [...container.querySelectorAll("[id]")].map((e) => e.id);
    expect(ids.filter((id, i) => ids.indexOf(id) !== i)).toEqual([]);
    // What each control points at is its own card's: a collapsed audit row's detail is not
    // drawn, so its name is checked, not the element.
    const controls = [...container.querySelectorAll("[aria-controls]")].map((e) => e.getAttribute("aria-controls"));
    expect(controls.filter((c, i) => controls.indexOf(c) !== i)).toEqual([]);
    for (const el of container.querySelectorAll("[aria-labelledby]")) {
      const card = el.closest("[data-testid=assistant-card]");
      for (const ref of (el.getAttribute("aria-labelledby") ?? "").split(" ").filter(Boolean))
        expect([ref, card ? card.contains(document.getElementById(ref)) : true]).toEqual([ref, true]);
    }
  });
});

describe("a stored card", () => {
  it("keeps a small snapshot and drops a large one, keeping what it says about the read", () => {
    const small = storedCard({ op: "namespace.audit", args: {}, data: AUDIT, readAt: 1, busy: true });
    expect(small).toEqual({ op: "namespace.audit", args: {}, data: AUDIT, readAt: 1 });
    const big = storedCard({
      op: "namespace.audit",
      args: {},
      data: "x".repeat(CARD_SNAPSHOT_MAX + 1),
      readAt: 1,
    });
    expect(big).toEqual({ op: "namespace.audit", args: {}, readAt: 1 });
  });
});
