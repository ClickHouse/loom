import { cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ConversationHit, ConversationThreadSummary } from "../api";
import { ApiError } from "../lib/errors";
import { useStore } from "../lib/state";
import { Conversations } from "./Conversations";

// Conversations against the list rules: a thread is named by its title, else by what the
// user said, else "Untitled thread" — never its id; a search shows one row per thread with
// up to three of its matching turns, the matched words in 600 weight; the Record
// conversations switch sits with the page title; a failed read says so with a Retry and
// no count.

const api = vi.hoisted(() => ({
  listConversationThreads: vi.fn(),
  searchConversations: vi.fn(),
  getNamespace: vi.fn(async () => ({ conversation_logging_enabled: true })),
  getConversationThread: vi.fn(async () => ({
    found: true,
    thread_id: "t-incident",
    title: "",
    agent: "oncall",
    msg_count: 1,
    created_at: null,
    updated_at: null,
    messages: [
      {
        msg_id: "a2",
        agent: "claude-code",
        role: "user",
        content: "Are refunds still failing?",
        ts: new Date().toISOString(),
      },
    ],
  })),
  listMemories: vi.fn(async () => ({ entries: [], total: 0, today: 0, truncated: false })),
}));
vi.mock("../api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api")>()), ...api }));
vi.mock("../lib/namespaceRole", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/namespaceRole")>()),
  useNamespaceRole: () => "admin",
}));
vi.mock("../components/SessionBriefsSection", () => ({ SessionBriefsSection: () => null }));
vi.mock("../components/SessionBriefPanel", () => ({ SessionBriefPanel: () => null }));

const now = new Date().toISOString();
const thread = (over: Partial<ConversationThreadSummary>): ConversationThreadSummary => ({
  thread_id: "t",
  agent: "oncall",
  msg_count: 42,
  first_seen: now,
  last_seen: now,
  last_role: "assistant",
  last_content_excerpt: "Rolled back payments-api.",
  title: "",
  ...over,
});
const UUID = "9f1c2d7e-55aa-4c1b-9d0e-6a7b8c9d0e1f";
const THREADS = [
  thread({ thread_id: "t-incident", title: "Payments API 5xx spike" }),
  thread({ thread_id: "t-release", last_role: "user", last_content_excerpt: "Can you draft the release notes?" }),
  thread({ thread_id: UUID }),
];
const hit = (thread_id: string, msg_id: string, role: ConversationHit["role"], content_excerpt: string) => ({
  thread_id,
  msg_id,
  role,
  content_excerpt,
  ts: now,
});

const toolbar = () => screen.getByLabelText("Search and filters");
async function search(q: string) {
  const box = screen.getByLabelText("Search conversation transcripts");
  fireEvent.change(box, { target: { value: q } });
  fireEvent.keyDown(box, { key: "Enter" });
}

describe("Conversations list", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listConversationThreads.mockResolvedValue({ threads: THREADS });
    useStore.setState({
      authed: true,
      whoami: { authenticated: false },
      identity: { org: "dev", namespace: "ns-a", agent: "loom-ui" },
      view: "sessions",
    });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ identity: null, whoami: null });
  });

  it("names an untitled thread by the user's message, else 'Untitled thread', never its id", async () => {
    render(<Conversations />);
    expect(await screen.findByText("Payments API 5xx spike")).toBeInTheDocument();
    expect(screen.getAllByText("Can you draft the release notes?").length).toBeGreaterThan(0);
    expect(screen.getByText("Untitled thread")).toHaveAttribute("data-untitled");
    expect(screen.getByText("Payments API 5xx spike")).not.toHaveAttribute("data-untitled");
    expect(screen.queryByText(UUID)).toBeNull();
    expect(toolbar()).toHaveTextContent("3 shown");
    const row = screen.getByText("Payments API 5xx spike").closest("[data-thread-row]") as HTMLElement;
    expect(row).toHaveTextContent("42 messages");
    expect(row.querySelector("time")).not.toBeNull();
  });

  it("puts the Record conversations switch with the page title, not under the toolbar", async () => {
    render(<Conversations />);
    const recording = await screen.findByRole("switch", { name: "Record conversations" });
    const header = screen.getByRole("heading", { level: 1, name: "Conversations" }).closest("header, div");
    expect(header?.parentElement?.parentElement).toContainElement(recording);
    expect(toolbar()).not.toContainElement(recording);
    expect(toolbar().compareDocumentPosition(recording) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy();
  });

  it("groups hits under their thread's name, three lines at most, the matched word in 600 weight", async () => {
    api.searchConversations.mockResolvedValue({
      hits: [
        hit("t-incident", "a1", "assistant", "the refund endpoint was returning 502"),
        hit("t-incident", "a2", "user", "Are refunds still failing?"),
        hit("t-incident", "a3", "assistant", "refund retried"),
        hit("t-incident", "a4", "assistant", "refund retried again"),
        hit(UUID, "b1", "assistant", "Refund requests landed in the wrong namespace."),
      ],
    });
    render(<Conversations />);
    await screen.findByText("Payments API 5xx spike");
    await search("refund");
    const bar = screen.getByLabelText("Search and filters");
    await waitFor(() => expect(bar.querySelector("[aria-live]")).toHaveTextContent("5 matches in 2 threads"));
    const rows = document.querySelectorAll<HTMLElement>("[data-thread-row]");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("Payments API 5xx spike");
    expect(rows[0]).toHaveTextContent("4 matches");
    // Three hit lines and the row's own button.
    expect(within(rows[0]).getAllByRole("button")).toHaveLength(4);
    expect(within(rows[1]).getByText("Untitled thread")).toBeInTheDocument();
    expect(rows[1]).not.toHaveTextContent(UUID);
    expect(within(rows[0]).getAllByText("refund", { selector: "strong" }).length).toBeGreaterThan(0);
    // No accent "· open" text.
    expect(screen.queryByText(/· open$/)).toBeNull();
  });

  it("opens the replay at the hit line's turn, named by the thread's first user turn", async () => {
    api.searchConversations.mockResolvedValue({
      hits: [hit("t-incident", "a2", "user", "Are refunds still failing?")],
    });
    render(<Conversations />);
    await screen.findByText("Payments API 5xx spike");
    await search("refund");
    await userEvent.click(await screen.findByRole("button", { name: /^User Are refunds still failing\?$/ }));
    // Named as the row names it until the detail lands; an untitled one then by its first
    // user turn.
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(dialog).toHaveAccessibleName("Are refunds still failing?"));
  });

  it("says a failed read with a Retry, and no count", async () => {
    api.listConversationThreads
      .mockRejectedValueOnce(new ApiError({ status: 503, message: "ClickHouse timed out", requestId: "rid-9" }))
      .mockResolvedValueOnce({ threads: THREADS });
    render(<Conversations />);
    expect(await screen.findByText("Couldn't load conversations")).toBeInTheDocument();
    expect(screen.getByText("rid-9").tagName).toBe("CODE");
    expect(toolbar()).not.toHaveTextContent(/\d/);
    expect(screen.queryByText(/No conversations yet/)).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Payments API 5xx spike")).toBeInTheDocument();
  });

  it("Clear filters on a search with no hits empties the box and brings the threads back", async () => {
    api.searchConversations.mockResolvedValueOnce({ hits: [] });
    render(<Conversations />);
    await screen.findByText("Payments API 5xx spike");
    await search("nothing");
    await userEvent.click(await screen.findByRole("button", { name: "Clear filters" }));
    expect(screen.getByLabelText("Search conversation transcripts")).toHaveValue("");
    expect(screen.queryByText(/No conversations match/)).toBeNull();
    expect(document.querySelectorAll("[data-thread-row]")).toHaveLength(3);
  });

  it("says a failed search with a Retry, not 'no matches'", async () => {
    api.searchConversations.mockRejectedValueOnce(new Error("search down")).mockResolvedValueOnce({
      hits: [hit("t-incident", "a1", "assistant", "refund")],
    });
    render(<Conversations />);
    await screen.findByText("Payments API 5xx spike");
    await search("refund");
    expect(await screen.findByText("Couldn't load search results")).toBeInTheDocument();
    expect(screen.queryByText(/No conversations match/)).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(document.querySelectorAll("[data-thread-row]")).toHaveLength(1));
  });
});
