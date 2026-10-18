import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ConversationHit, ConversationThreadDetail, ConversationThreadSummary, MemoryListResponse } from "../api";
import { useStore } from "../lib/state";
import { Conversations } from "./Conversations";
import { must } from "../test/must";

// Conversations stays mounted while threads open and close and while the namespace
// switches, so every async answer must land only on the thread or namespace it was
// asked for: thread A's detail and memories never render under thread B, and
// namespace A's hits and recording switch never survive into namespace B.

const listConversationThreads = vi.fn();
const getConversationThread = vi.fn<typeof import("../api").getConversationThread>();
const listMemories = vi.fn();
const searchConversations = vi.fn();
const getNamespace = vi.fn();
const setConversationLogging = vi.fn();
const listConversationMessages = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listConversationThreads: (...args: unknown[]) => listConversationThreads(...args),
    getConversationThread: (...args: Parameters<typeof actual.getConversationThread>) => getConversationThread(...args),
    listMemories: (...args: unknown[]) => listMemories(...args),
    searchConversations: (...args: unknown[]) => searchConversations(...args),
    getNamespace: (...args: unknown[]) => getNamespace(...args),
    setConversationLogging: (...args: unknown[]) => setConversationLogging(...args),
    listConversationMessages: (...args: unknown[]) => listConversationMessages(...args),
  };
});
vi.mock("../lib/namespaceRole", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/namespaceRole")>()),
  useNamespaceRole: () => "admin",
}));
vi.mock("../components/SessionBriefsSection", () => ({ SessionBriefsSection: () => null }));
vi.mock("../components/SessionBriefPanel", () => ({ SessionBriefPanel: () => null }));

function deferred<T>() {
  let resolve: (v: T) => void = () => {};
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

const summary = (thread_id: string, title: string): ConversationThreadSummary => ({
  thread_id,
  agent: "claude-code",
  msg_count: 1,
  first_seen: new Date().toISOString(),
  last_seen: new Date().toISOString(),
  last_role: "user",
  last_content_excerpt: `excerpt of ${thread_id}`,
  title,
});

const detail = (thread_id: string, title: string): ConversationThreadDetail => ({
  found: true,
  thread_id,
  title,
  agent: "claude-code",
  msg_count: 1,
  created_at: null,
  updated_at: null,
  messages: [
    { msg_id: `${thread_id}-m1`, role: "user", content: `body of ${thread_id}`, ts: new Date().toISOString() },
  ],
});

const memories = (key: string): MemoryListResponse => ({
  entries: [
    {
      memory_key: key,
      memory_type: "semantic",
      confidence: 1,
      version: 1,
      created_at: new Date().toISOString(),
      expires_at: null,
      pinned: false,
      tags: [],
      value: "v",
    },
  ],
  truncated: false,
  total: 1,
  today: 1,
});

const noMemories: MemoryListResponse = { entries: [], truncated: false, total: 0, today: 0 };

const hit = (thread_id: string, content_excerpt: string): ConversationHit => ({
  thread_id,
  msg_id: `${thread_id}-m1`,
  role: "user",
  content_excerpt,
  ts: new Date().toISOString(),
});

const setNamespace = (namespace: string) =>
  useStore.setState({ identity: { org: "dev", namespace, agent: "loom-ui" } });

describe("Conversations: answers follow the current thread and namespace", () => {
  beforeEach(() => {
    vi.clearAllMocks();
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

  it("drops thread A's detail and memories when they land after thread B opened", async () => {
    listConversationThreads.mockResolvedValue({
      threads: [summary("thread-a", "Title A"), summary("thread-b", "Title B")],
    });
    getNamespace.mockResolvedValue({ conversation_logging_enabled: true });
    const detailA = deferred<ConversationThreadDetail>();
    const memA = deferred<MemoryListResponse>();
    getConversationThread.mockReturnValueOnce(detailA.promise).mockResolvedValueOnce(detail("thread-b", "Title B"));
    listMemories.mockReturnValueOnce(memA.promise).mockResolvedValueOnce(memories("from/b"));

    render(<Conversations />);
    fireEvent.click(await screen.findByText("Title A"));
    await waitFor(() => expect(getConversationThread).toHaveBeenCalledTimes(1));
    const signalA = must(getConversationThread.mock.calls[0][3]?.signal, "thread A signal");
    fireEvent.keyDown(await screen.findByRole("dialog"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(signalA.aborted).toBe(true);

    fireEvent.click(screen.getByText("Title B"));
    const dialog = await screen.findByRole("dialog");
    expect(await screen.findByText("body of thread-b")).toBeInTheDocument();
    expect(await screen.findByText("from/b")).toBeInTheDocument();
    // A produced memory is a label, not a tab stop: twenty of them stood before the transcript.
    expect(screen.getByText("from/b").closest("[tabindex]")).not.toHaveAttribute("role", "note");

    await act(async () => {
      detailA.resolve(detail("thread-a", "Title A"));
      memA.resolve(memories("from/a"));
    });
    expect(screen.queryByText("body of thread-a")).toBeNull();
    expect(screen.queryByText("from/a")).toBeNull();
    expect(dialog).toHaveTextContent("thread-b");
    expect(screen.getByText("body of thread-b")).toBeInTheDocument();
  });

  it("clears namespace A's hits and recording switch on a switch to B", async () => {
    listConversationThreads.mockResolvedValue({ threads: [summary("thread-a", "Title A")] });
    const nsB = deferred<{ conversation_logging_enabled: boolean }>();
    getNamespace.mockResolvedValueOnce({ conversation_logging_enabled: true }).mockReturnValueOnce(nsB.promise);
    searchConversations.mockResolvedValueOnce({ hits: [hit("thread-a", "said in A")] });
    setConversationLogging.mockResolvedValue({});

    render(<Conversations />);
    expect(await screen.findByRole("switch", { name: "Record conversations" })).toBeChecked();
    const box = screen.getByLabelText("Search conversation transcripts");
    fireEvent.change(box, { target: { value: "hello" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(await screen.findByText("said in A")).toBeInTheDocument();

    const lateA = deferred<{ hits: ConversationHit[] }>();
    searchConversations.mockReturnValueOnce(lateA.promise);
    fireEvent.keyDown(box, { key: "Enter" });
    act(() => setNamespace("ns-b"));
    await waitFor(() => expect(screen.queryByText("said in A")).toBeNull());
    // B's recording value is not loaded yet, so there is nothing to toggle.
    expect(screen.queryByRole("switch", { name: "Record conversations" })).toBeNull();
    await act(async () => lateA.resolve({ hits: [hit("thread-a", "late from A")] }));
    expect(screen.queryByText("late from A")).toBeNull();

    await act(async () => nsB.resolve({ conversation_logging_enabled: false }));
    const recording = await screen.findByRole("switch", { name: "Record conversations" });
    expect(recording).not.toBeChecked();
    fireEvent.click(recording);
    await waitFor(() => expect(setConversationLogging).toHaveBeenCalledTimes(1));
    expect(setConversationLogging.mock.calls[0][0]).toMatchObject({ namespace: "ns-b" });
    expect(setConversationLogging.mock.calls[0][1]).toBe(true);
  });

  it("opens a hit from another namespace in that namespace", async () => {
    listConversationThreads.mockResolvedValue({ threads: [] });
    getNamespace.mockResolvedValue({ conversation_logging_enabled: true });
    searchConversations.mockResolvedValueOnce({ hits: [{ ...hit("thread-z", "said in Z"), namespace: "ns-z" }] });
    getConversationThread.mockResolvedValue(detail("thread-z", "Title Z"));
    listMemories.mockResolvedValue(noMemories);

    render(<Conversations />);
    const box = screen.getByLabelText("Search conversation transcripts");
    fireEvent.change(box, { target: { value: "said" } });
    fireEvent.keyDown(box, { key: "Enter" });
    // The result count is announced: the toolbar's status region, mounted with the bar.
    const bar = screen.getByLabelText("Search and filters");
    await waitFor(() => expect(bar.querySelector("[aria-live]")).toHaveTextContent("1 match in 1 thread"));
    fireEvent.click(await screen.findByText("said in Z"));
    expect(await screen.findByText("body of thread-z")).toBeInTheDocument();
    const [scope, agent, threadId] = getConversationThread.mock.calls[0];
    expect(scope).toMatchObject({ namespace: "ns-z" });
    expect([agent, threadId]).toEqual(["*", "thread-z"]);
    expect(listMemories.mock.calls[0][0]).toMatchObject({ namespace: "ns-z" });
    expect(await screen.findByRole("dialog")).toHaveTextContent("ns-z");
  });

  it("opens a listed thread with every agent's turns, and pages it the same way", async () => {
    listConversationThreads.mockResolvedValue({ threads: [summary("thread-y", "Title Y")] });
    getNamespace.mockResolvedValue({ conversation_logging_enabled: true });
    getConversationThread.mockResolvedValue({ ...detail("thread-y", "Title Y"), msg_count: 2, next_cursor: "c-1" });
    listMemories.mockResolvedValue(noMemories);
    listConversationMessages.mockResolvedValue({ rows: [], next_cursor: null });

    render(<Conversations />);
    fireEvent.click(await screen.findByText("Title Y"));
    fireEvent.click(await screen.findByRole("button", { name: /Load more/ }));
    await waitFor(() => expect(listConversationMessages).toHaveBeenCalledTimes(1));
    expect(getConversationThread).toHaveBeenCalledTimes(1);
    expect(getConversationThread.mock.calls[0][1]).toBe("*");
    expect(listConversationMessages.mock.calls[0][1]).toBe("*");
    // The page cursor is the one conversation.get issued, never a message id.
    expect(listConversationMessages.mock.calls[0][3]).toMatchObject({ cursor: "c-1" });
  });

  it("falls back to the listed agent where a replica reads '*' literally", async () => {
    listConversationThreads.mockResolvedValue({ threads: [summary("thread-y", "Title Y")] });
    getNamespace.mockResolvedValue({ conversation_logging_enabled: true });
    getConversationThread
      .mockResolvedValueOnce({ ...detail("thread-y", ""), found: false, msg_count: 0, messages: [] })
      .mockResolvedValueOnce({ ...detail("thread-y", "Title Y"), msg_count: 2, next_cursor: "c-1" });
    listMemories.mockResolvedValue(noMemories);
    listConversationMessages.mockResolvedValue({ rows: [], next_cursor: null });

    render(<Conversations />);
    fireEvent.click(await screen.findByText("Title Y"));
    expect(await screen.findByText("body of thread-y")).toBeInTheDocument();
    expect(getConversationThread.mock.calls.map((c) => c[1])).toEqual(["*", "claude-code"]);
    fireEvent.click(screen.getByRole("button", { name: /Load more/ }));
    // Each page asks for every agent first; only an empty answer retries as the listed agent.
    await waitFor(() => expect(listConversationMessages).toHaveBeenCalledTimes(2));
    expect(listConversationMessages.mock.calls.map((c) => c[1])).toEqual(["*", "claude-code"]);
    expect(listConversationMessages.mock.calls[0][3]).toMatchObject({ cursor: "c-1" });
  });

  it("labels each assistant turn with the agent that wrote it", async () => {
    listConversationThreads.mockResolvedValue({ threads: [summary("thread-p", "Title P")] });
    getNamespace.mockResolvedValue({ conversation_logging_enabled: true });
    const now = new Date().toISOString();
    getConversationThread.mockResolvedValue({
      ...detail("thread-p", "Title P"),
      agent: "planner",
      msg_count: 2,
      messages: [
        { msg_id: "p1", agent: "planner", role: "assistant", content: "plan it", ts: now },
        { msg_id: "p2", agent: "deployer", role: "assistant", content: "shipped", ts: now },
      ],
    });
    listMemories.mockResolvedValue(noMemories);

    render(<Conversations />);
    fireEvent.click(await screen.findByText("Title P"));
    const shipped = await screen.findByText("shipped");
    expect(must(shipped.closest("[id^='msg-']"), "turn p2")).toHaveTextContent("deployer");
    expect(must(screen.getByText("plan it").closest("[id^='msg-']"), "turn p1")).toHaveTextContent("planner");
  });

  it("opens a memory's source thread under the memory's agent where '*' finds nothing", async () => {
    listConversationThreads.mockResolvedValue({ threads: [] });
    getNamespace.mockResolvedValue({ conversation_logging_enabled: true });
    getConversationThread
      .mockResolvedValueOnce({ ...detail("thread-s", ""), found: false, msg_count: 0, messages: [] })
      .mockResolvedValueOnce(detail("thread-s", "Title S"));
    listMemories.mockResolvedValue(noMemories);
    useStore.setState({ pendingConversationThread: { threadId: "thread-s", agent: "claude-code" } });

    render(<Conversations />);
    expect(await screen.findByText("body of thread-s")).toBeInTheDocument();
    expect(getConversationThread.mock.calls.map((c) => c[1])).toEqual(["*", "claude-code"]);
  });

  it("says a missing thread is not found, with no message count", async () => {
    listConversationThreads.mockResolvedValue({ threads: [] });
    getNamespace.mockResolvedValue({ conversation_logging_enabled: true });
    getConversationThread.mockResolvedValue({ ...detail("thread-x", ""), found: false, msg_count: 0, messages: [] });
    listMemories.mockResolvedValue(noMemories);
    useStore.setState({ pendingConversationThread: { threadId: "thread-x", agent: "" } });

    render(<Conversations />);
    expect(await screen.findByText(/Thread not found/)).toBeInTheDocument();
    expect(screen.queryByText("0 messages")).toBeNull();
  });

  it("says a thread read by its agent is cut short when no page cursor came back", async () => {
    listConversationThreads.mockResolvedValue({ threads: [summary("thread-y", "Title Y")] });
    getNamespace.mockResolvedValue({ conversation_logging_enabled: true });
    getConversationThread
      .mockResolvedValueOnce({ ...detail("thread-y", ""), found: false, msg_count: 0, messages: [] })
      .mockResolvedValueOnce({ ...detail("thread-y", "Title Y"), msg_count: 260 });
    listMemories.mockResolvedValue(noMemories);

    render(<Conversations />);
    fireEvent.click(await screen.findByText("Title Y"));
    expect(await screen.findByText("Showing 1 of 260 messages.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Load more/ })).toBeNull();
  });

  it("pages under the listed agent when a '*' page comes back empty", async () => {
    listConversationThreads.mockResolvedValue({ threads: [summary("thread-y", "Title Y")] });
    getNamespace.mockResolvedValue({ conversation_logging_enabled: true });
    getConversationThread.mockResolvedValue({ ...detail("thread-y", "Title Y"), msg_count: 3, next_cursor: "c-1" });
    listMemories.mockResolvedValue(noMemories);
    const now = new Date().toISOString();
    listConversationMessages.mockResolvedValueOnce({ rows: [], next_cursor: null }).mockResolvedValueOnce({
      rows: [{ msg_id: "y-2", role: "user", content: "second turn", ts: now }],
      next_cursor: "c-2",
    });

    render(<Conversations />);
    fireEvent.click(await screen.findByText("Title Y"));
    fireEvent.click(await screen.findByRole("button", { name: /Load more/ }));
    expect(await screen.findByText("second turn")).toBeInTheDocument();
    // That page was the listed agent's alone; its cursor would skip other agents' turns, so
    // the dialog stops and says how much it shows.
    expect(screen.queryByRole("button", { name: /Load more/ })).toBeNull();
    expect(screen.getByText("Showing 2 of 3 messages.")).toBeInTheDocument();
    expect(listConversationMessages.mock.calls.map((c) => c[1])).toEqual(["*", "claude-code"]);
  });

  it("keeps two agents' turns that share an id apart", async () => {
    listConversationThreads.mockResolvedValue({ threads: [summary("thread-d", "Title D")] });
    getNamespace.mockResolvedValue({ conversation_logging_enabled: true });
    const now = new Date().toISOString();
    getConversationThread.mockResolvedValue({
      ...detail("thread-d", "Title D"),
      msg_count: 2,
      messages: [
        { msg_id: "1", agent: "planner", role: "user", content: "plan it", ts: now },
        { msg_id: "1", agent: "deployer", role: "user", content: "ship it", ts: now },
      ],
    });
    listMemories.mockResolvedValue(noMemories);

    render(<Conversations />);
    fireEvent.click(await screen.findByText("Title D"));
    await screen.findByText("ship it");
    const ids = [...document.querySelectorAll("[data-msg-id]")].map((el) => el.id);
    expect(ids).toHaveLength(2);
    expect(new Set(ids).size).toBe(2);
  });

  it("moves focus to the transcript, at the first new turn, when Load more brings the last page", async () => {
    listConversationThreads.mockResolvedValue({ threads: [summary("thread-y", "Title Y")] });
    getNamespace.mockResolvedValue({ conversation_logging_enabled: true });
    getConversationThread.mockResolvedValue({ ...detail("thread-y", "Title Y"), msg_count: 2, next_cursor: "c-1" });
    listMemories.mockResolvedValue(noMemories);
    const now = new Date().toISOString();
    listConversationMessages.mockResolvedValue({
      rows: [{ msg_id: "y-2", agent: "claude-code", role: "user", content: "second turn", ts: now }],
      next_cursor: null,
    });

    render(<Conversations />);
    fireEvent.click(await screen.findByText("Title Y"));
    const more = await screen.findByRole("button", { name: /Load more/ });
    more.focus();
    fireEvent.click(more);
    const scrolled = vi.fn();
    const scrollIntoView = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scrolled;
    await screen.findByText("second turn");
    // A tabbable region, so Tab moves on from it inside the dialog.
    const transcript = screen.getByRole("region", { name: "Transcript" });
    await waitFor(() => expect(transcript).toHaveFocus());
    expect(transcript).toHaveAttribute("tabindex", "0");
    expect(scrolled).toHaveBeenCalled();
    Element.prototype.scrollIntoView = scrollIntoView;
  });

  it("rings only the turn a hit names when two agents' turns share its id", async () => {
    listConversationThreads.mockResolvedValue({ threads: [] });
    getNamespace.mockResolvedValue({ conversation_logging_enabled: true });
    const now = new Date().toISOString();
    searchConversations.mockResolvedValueOnce({
      hits: [{ thread_id: "thread-s", msg_id: "1", role: "user", content_excerpt: "ship the fix", ts: now }],
    });
    getConversationThread.mockResolvedValue({
      ...detail("thread-s", "Title S"),
      msg_count: 2,
      messages: [
        { msg_id: "1", agent: "planner", role: "user", content: "plan the fix", ts: now },
        { msg_id: "1", agent: "deployer", role: "user", content: "ship the fix", ts: now },
      ],
    });
    listMemories.mockResolvedValue(noMemories);

    render(<Conversations />);
    const box = screen.getByLabelText("Search conversation transcripts");
    fireEvent.change(box, { target: { value: "ship" } });
    fireEvent.keyDown(box, { key: "Enter" });
    // The hit line, not the row's title (an untitled thread is named by what the user said).
    fireEvent.click(await screen.findByRole("button", { name: /^user ship the fix$/ }));
    await screen.findAllByText("plan the fix");
    const ringed = [...document.querySelectorAll("[data-highlighted]")].map((el) => el.textContent);
    expect(ringed).toHaveLength(1);
    expect(ringed[0]).toContain("ship the fix");
  });

  it("ignores '/' pressed inside the thread dialog", async () => {
    listConversationThreads.mockResolvedValue({ threads: [summary("thread-a", "Title A")] });
    getNamespace.mockResolvedValue({ conversation_logging_enabled: true });
    getConversationThread.mockResolvedValue(detail("thread-a", "Title A"));
    listMemories.mockResolvedValue(noMemories);

    render(<Conversations />);
    const box = screen.getByLabelText("Search conversation transcripts");
    fireEvent.keyDown(document.body, { key: "/" });
    expect(box).toHaveFocus();

    fireEvent.click(await screen.findByText("Title A"));
    const dialog = await screen.findByRole("dialog");
    await screen.findByText("body of thread-a");
    fireEvent.keyDown(dialog, { key: "/" });
    expect(box).not.toHaveFocus();
  });
});

// Every thread in the namespace can be reached, a page of 30 at a time: the next page asks
// the server for the threads after the first 30, and a build that predates paging (it
// answers the first page again) ends the paging instead of repeating threads.
describe("Conversations: thread pages", () => {
  const page = (from: number, n: number) =>
    Array.from({ length: n }, (_, i) => summary(`t${from + i}`, `Thread ${from + i}`));
  beforeEach(() => {
    vi.clearAllMocks();
    getNamespace.mockResolvedValue({ conversation_logging_enabled: true });
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

  it("reads the next 30 threads on the next page", async () => {
    listConversationThreads.mockImplementation(async (_id: unknown, opts: { offset?: number }) => ({
      threads: (opts.offset ?? 0) === 0 ? page(0, 30) : page(30, 5),
    }));
    render(<Conversations />);
    expect(await screen.findByText("Thread 0")).toBeInTheDocument();
    const pager = screen.getByRole("navigation", { name: "Thread pages" });
    fireEvent.click(within(pager).getByRole("button", { name: "Next page" }));
    expect(await screen.findByText("Thread 30")).toBeInTheDocument();
    expect(listConversationThreads).toHaveBeenLastCalledWith(
      { org: "dev", namespace: "ns-a" },
      expect.objectContaining({ limit: 30, offset: 30, window_days: 365 }),
    );
    expect(
      within(screen.getByRole("navigation", { name: "Thread pages" }))
        .getAllByRole("button")
        .at(-1),
    ).toBeDisabled();
  });

  it("stops paging when the server answers the first page again", async () => {
    listConversationThreads.mockImplementation(async () => ({ threads: page(0, 30) }));
    render(<Conversations />);
    expect(await screen.findByText("Thread 0")).toBeInTheDocument();
    fireEvent.click(
      within(screen.getByRole("navigation", { name: "Thread pages" }))
        .getAllByRole("button")
        .at(-1)!,
    );
    await waitFor(() => expect(screen.queryByRole("navigation", { name: "Thread pages" })).toBeNull());
    expect(screen.getByText("Thread 0")).toBeInTheDocument();
  });

  it("with an exact multiple of 30 threads, the empty page after the last goes back to it", async () => {
    listConversationThreads.mockImplementation(async (_id: unknown, opts: { offset?: number }) => ({
      threads: (opts.offset ?? 0) >= 60 ? [] : page(opts.offset ?? 0, 30),
    }));
    render(<Conversations />);
    expect(await screen.findByText("Thread 0")).toBeInTheDocument();
    fireEvent.click(
      within(screen.getByRole("navigation", { name: "Thread pages" })).getByRole("button", { name: "Next page" }),
    );
    expect(await screen.findByText("Thread 30")).toBeInTheDocument();
    fireEvent.click(
      within(screen.getByRole("navigation", { name: "Thread pages" })).getByRole("button", { name: "Next page" }),
    );
    await waitFor(() =>
      expect(
        within(screen.getByRole("navigation", { name: "Thread pages" })).getByRole("button", { name: "Next page" }),
      ).toBeDisabled(),
    );
    expect(screen.getByText("Thread 30")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Thread pages" })).toHaveTextContent("31–60 of 60");
    expect(screen.queryByText("No conversations yet in this namespace")).toBeNull();
  });
});
