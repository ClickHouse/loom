import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SearchHit } from "../api";

vi.mock("../api", () => ({
  listMemories: vi.fn(),
  storedToken: vi.fn(),
}));
import { listMemories, storedToken } from "../api";
import { resolveSearchWriter } from "./memoriesSearch";

const jwt = (claims: Record<string, unknown>) => `h.${btoa(JSON.stringify(claims))}.s`;
const identity = { org: "dev", namespace: "alice", agent: "loom-ui" };
const hit = { memory_key: "deploy_note", agent: "claude-desktop" } as SearchHit;
const ABORT = new AbortController().signal;

describe("resolveSearchWriter under a narrowed token", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listMemories).mockResolvedValue({
      entries: [{ memory_key: "deploy_note", agent: "claude-desktop", user_id: "u1", session_id: "s1" }],
      total: 1,
      truncated: false,
    } as never);
  });

  it("sends the pinned user and no user wildcard", async () => {
    vi.mocked(storedToken).mockReturnValue(jwt({ read_user_id: "u1" }));
    expect(await resolveSearchWriter(identity, hit, ABORT)).toMatchObject({ user_id: "u1", session_id: "s1" });
    // the session wildcard is still allowed under own_user
    expect(vi.mocked(listMemories).mock.calls[0][0]).toMatchObject({ user_id: "u1", session_id: "*" });
  });

  it("does not ask when the token narrows reads and no user is known", async () => {
    vi.mocked(storedToken).mockReturnValue(jwt({ read_scope: "own_user" }));
    expect(await resolveSearchWriter(identity, hit, ABORT)).toBeUndefined();
    expect(listMemories).not.toHaveBeenCalled();
  });

  it("does not send a session wildcard under an own-session token", async () => {
    vi.mocked(storedToken).mockReturnValue(jwt({ read_scope: "own_session", read_user_id: "u1" }));
    expect(await resolveSearchWriter(identity, hit, ABORT)).toBeUndefined();
    expect(listMemories).not.toHaveBeenCalled();
  });

  it("leaves the user out for an end-user token, which the server pins itself", async () => {
    vi.mocked(storedToken).mockReturnValue(jwt({ sub: "user:u1", iss: "idp" }));
    expect(await resolveSearchWriter(identity, hit, ABORT)).toMatchObject({ user_id: "u1" });
    const sent = vi.mocked(listMemories).mock.calls[0][0];
    expect(sent.user_id).toBeUndefined();
    expect(sent.session_id).toBe("*");
  });

  it("reads later pages for a unique key and names its writer", async () => {
    vi.mocked(storedToken).mockReturnValue(jwt({}));
    const other = { memory_key: "deploy_note_2", agent: "claude-desktop", user_id: "u2", session_id: "s2" };
    vi.mocked(listMemories)
      .mockResolvedValueOnce({ entries: [other], rows_read: 1, total: 2, truncated: true } as never)
      .mockResolvedValueOnce({
        entries: [{ memory_key: "deploy_note", agent: "claude-desktop", user_id: "u1", session_id: "s1" }],
        rows_read: 1,
        total: 2,
        truncated: false,
      } as never);
    expect(await resolveSearchWriter(identity, hit, ABORT)).toMatchObject({ user_id: "u1", session_id: "s1" });
    expect(vi.mocked(listMemories).mock.calls.map((c) => c[2]?.offset)).toEqual([0, 1]);
  });

  it("advances by the entries when the server reports no rows read", async () => {
    vi.mocked(storedToken).mockReturnValue(jwt({}));
    const row = (k: string) => ({ memory_key: k, agent: "claude-desktop", user_id: "u1", session_id: "s1" });
    vi.mocked(listMemories)
      .mockResolvedValueOnce({ entries: [row("x")], rows_read: 0, total: 2, truncated: true } as never)
      .mockResolvedValueOnce({ entries: [row("deploy_note")], rows_read: 0, total: 2, truncated: false } as never);
    expect(await resolveSearchWriter(identity, hit, ABORT)).toMatchObject({ user_id: "u1" });
    expect(vi.mocked(listMemories).mock.calls.map((c) => c[2]?.offset)).toEqual([0, 1]);
  });

  it("names no writer when the list is not read to its end", async () => {
    vi.mocked(storedToken).mockReturnValue(jwt({}));
    vi.mocked(listMemories).mockResolvedValue({ entries: [], rows_read: 0, total: 5, truncated: true } as never);
    expect(await resolveSearchWriter(identity, hit, ABORT)).toBeUndefined();
    expect(listMemories).toHaveBeenCalledTimes(1);
  });

  it("keeps the wildcards for a namespace-wide token", async () => {
    vi.mocked(storedToken).mockReturnValue(jwt({}));
    await resolveSearchWriter(identity, hit, ABORT);
    expect(vi.mocked(listMemories).mock.calls[0][0]).toMatchObject({ user_id: "*", session_id: "*" });
  });
});
