import { act, cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
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
    fireEvent.click(await screen.findByText("said in Z"));
    expect(await screen.findByText("body of thread-z")).toBeInTheDocument();
    const [scope, agent, threadId] = getConversationThread.mock.calls[0];
    expect(scope).toMatchObject({ namespace: "ns-z" });
    expect([agent, threadId]).toEqual(["*", "thread-z"]);
    expect(listMemories.mock.calls[0][0]).toMatchObject({ namespace: "ns-z" });
    expect(await screen.findByRole("dialog")).toHaveTextContent("ns-z");
  });

  it("pages the rest of a thread under the agent conversation.get found", async () => {
    listConversationThreads.mockResolvedValue({ threads: [] });
    getNamespace.mockResolvedValue({ conversation_logging_enabled: true });
    searchConversations.mockResolvedValueOnce({ hits: [hit("thread-y", "said in Y")] });
    getConversationThread.mockResolvedValue({ ...detail("thread-y", "Title Y"), msg_count: 2 });
    listMemories.mockResolvedValue(noMemories);
    listConversationMessages.mockResolvedValue({ rows: [], next_cursor: null });

    render(<Conversations />);
    const box = screen.getByLabelText("Search conversation transcripts");
    fireEvent.change(box, { target: { value: "said" } });
    fireEvent.keyDown(box, { key: "Enter" });
    fireEvent.click(await screen.findByText("said in Y"));
    fireEvent.click(await screen.findByRole("button", { name: /Load more/ }));
    await waitFor(() => expect(listConversationMessages).toHaveBeenCalledTimes(1));
    expect(getConversationThread.mock.calls[0][1]).toBe("*");
    expect(listConversationMessages.mock.calls[0][1]).toBe("claude-code");
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
