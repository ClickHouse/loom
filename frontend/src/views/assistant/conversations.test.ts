// The assistant's conversations in this browser: who sees them, how long and how much is
// kept, and that no late write brings back what a sign-out, another person, Delete or
// Delete all removed.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EPOCH_KEY, INDEX_KEY, forgetAssistantConversations } from "../../lib/assistantStorage";
import { forgetOtherPeople, readIndex } from "./storedIndex";
import { setPageOwner } from "../../api";
import {
  ANSWER_MAX_MS,
  ConversationDeleted,
  KEPT_TURNS,
  StorageRefused,
  adoptEpoch,
  deleteAllConversations,
  deleteConversation,
  listConversations,
  mergeTurns,
  randomId,
  readConversation,
  saveConversation,
  type Conversation,
  type Turn,
} from "./conversations";

function jwt(payload: Record<string, unknown>): string {
  const b64 = (s: string) => btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64(JSON.stringify({ alg: "HS256" }))}.${b64(JSON.stringify(payload))}.sig`;
}

const alice = { subject: "alice", org: "acme", scope: "-", user: "-" };
const turn = (i: number, state: Turn["state"] = "done"): Turn => ({
  id: `t${i}`,
  question: `q${i}`,
  answer: `a${i}`,
  state,
  at: i,
});
const conv = (id: string, turns: Turn[], extra: Partial<Conversation> = {}): Conversation => ({
  id,
  owner: alice,
  namespace: "team",
  title: "q0",
  turns,
  trimmed: false,
  updated: Date.now(),
  ...extra,
});
const all = () => true;

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("loom_token_v1", jwt({ sub: "alice", org: "acme" }));
  adoptEpoch(null);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("stored conversations", () => {
  it("makes ids from getRandomValues, which works without randomUUID", () => {
    vi.stubGlobal("crypto", { getRandomValues: crypto.getRandomValues.bind(crypto), randomUUID: undefined });
    const a = randomId();
    expect(a).toMatch(/^[0-9a-f]{32}$/);
    expect(randomId()).not.toBe(a);
  });

  it("lists only the owner's, by subject, org, read scope and read user, and only readable namespaces", () => {
    saveConversation(conv("c1", [turn(0)]));
    saveConversation(conv("c2", [turn(0)], { owner: { ...alice, scope: "own_user", user: "u1" } }));
    saveConversation(conv("c3", [turn(0)], { namespace: "secret" }));
    expect(listConversations(alice, (ns) => ns !== "secret").map((e) => e.id)).toEqual(["c1"]);
    expect(listConversations({ ...alice, scope: "own_user", user: "u1" }, all).map((e) => e.id)).toEqual(["c2"]);
    expect(readConversation("c2", alice)).toBeNull();
  });

  it(`keeps the newest ${KEPT_TURNS} turns and says older ones were not kept`, () => {
    const turns = Array.from({ length: 30 }, (_, i) => turn(i));
    saveConversation(conv("c1", turns));
    const back = readConversation("c1", alice);
    expect(back?.turns).toHaveLength(KEPT_TURNS);
    expect(back?.turns[0].id).toBe("t5");
    expect(back?.trimmed).toBe(true);
  });

  it("leaves out a turn saved mid-stream, and reads it as stopped once no tab answers it", () => {
    saveConversation(conv("c1", [turn(0), { ...turn(1, "answering"), answer: "Ada", at: Date.now() }]));
    expect(readConversation("c1", alice)?.turns.map((t) => t.id)).toEqual(["t0"]);
    const back = readConversation("c1", alice, { answering: "stopped" });
    expect(back?.turns[1]).toMatchObject({ state: "stopped", answer: "Ada" });
  });

  it("keeps a turn saved mid-stream as it is when asked to, for a save to write it back", () => {
    saveConversation(conv("c1", [turn(0), { ...turn(1, "answering"), at: Date.now() }]));
    expect(readConversation("c1", alice, { answering: "kept" })?.turns[1].state).toBe("answering");
  });

  it("reads a turn still answering after ANSWER_MAX_MS as stopped in every mode", () => {
    const old = Date.now() - ANSWER_MAX_MS - 1000;
    saveConversation(conv("c1", [turn(0), { ...turn(1, "answering"), at: old }]));
    expect(readConversation("c1", alice)?.turns[1].state).toBe("stopped");
    expect(readConversation("c1", alice, { answering: "kept" })?.turns[1].state).toBe("stopped");
  });

  it("deletes what is 30 days old on the next read", () => {
    const old = Date.now() - 31 * 24 * 3600 * 1000;
    saveConversation(conv("c1", [turn(0)], { updated: old }));
    saveConversation(conv("c2", [turn(0)]));
    expect(listConversations(alice, all).map((e) => e.id)).toEqual(["c2"]);
    expect(localStorage.getItem("loom_assistant_c_c1")).toBeNull();
  });

  it("drops the oldest conversations past 2 MB in all", () => {
    const big = "x".repeat(200 * 1024);
    for (let i = 0; i < 12; i++)
      saveConversation(conv(`c${i}`, [{ ...turn(0), answer: big }], { updated: Date.now() - (12 - i) * 1000 }));
    const ids = listConversations(alice, all).map((e) => e.id);
    expect(ids).toContain("c11");
    expect(ids).not.toContain("c0");
    expect(readIndex().entries.reduce((n, e) => n + e.bytes, 0)).toBeLessThanOrEqual(2 * 1024 * 1024);
  });

  it("refuses a write for a deleted id, so a late write cannot bring it back", () => {
    saveConversation(conv("c1", [turn(0)]));
    deleteConversation("c1");
    expect(() => saveConversation(conv("c1", [turn(0), turn(1)]))).toThrow(ConversationDeleted);
    expect(localStorage.getItem("loom_assistant_c_c1")).toBeNull();
  });

  it("refuses every write after another tab's sign-out, even once that tab's epoch is read", () => {
    saveConversation(conv("c1", [turn(0, "answering")]));
    // Tab A signs out while this tab (B) still streams: the token goes and the epoch moves.
    forgetAssistantConversations();
    localStorage.removeItem("loom_token_v1");
    expect(saveConversation(conv("c1", [turn(0)]))).toBe(false);
    // Even under the new epoch there is no token of the page's person to write as.
    adoptEpoch(null);
    expect(saveConversation(conv("c1", [turn(0)]))).toBe(false);
    expect(localStorage.getItem("loom_assistant_c_c1")).toBeNull();
    expect(readIndex().entries).toEqual([]);
  });

  it("refuses a write once another person's token is the stored one", () => {
    setPageOwner({ authenticated: true, subject: "alice", org: "acme" });
    localStorage.setItem("loom_token_v1", jwt({ sub: "mallory", org: "acme" }));
    expect(saveConversation(conv("c1", [turn(0)]))).toBe(false);
    setPageOwner(null);
  });

  it("deletes what another person stored when someone else signs in here", () => {
    saveConversation(conv("c1", [turn(0)]));
    expect(forgetOtherPeople("alice", "acme")).toBe(false);
    expect(forgetOtherPeople("mallory", "acme")).toBe(true);
    expect(localStorage.getItem("loom_assistant_c_c1")).toBeNull();
    expect(readIndex().entries).toEqual([]);
  });

  it("Delete all bumps the epoch: another tab's write under the old one is refused", () => {
    saveConversation(conv("c1", [turn(0)]));
    const epoch = Number(localStorage.getItem(EPOCH_KEY) || "0");
    deleteAllConversations(alice);
    expect(Number(localStorage.getItem(EPOCH_KEY))).toBe(epoch + 1);
    expect(listConversations(alice, all)).toEqual([]);
    // Another tab bumps the epoch again: this tab's is now the old one.
    localStorage.setItem(EPOCH_KEY, String(epoch + 2));
    expect(saveConversation(conv("c3", [turn(0)]))).toBe(false);
  });

  it("drops a tombstone after 30 days, and on an epoch bump", () => {
    vi.useFakeTimers({ now: Date.now() });
    deleteConversation("c1");
    expect(readIndex().tombstones.c1).toBeDefined();
    vi.setSystemTime(Date.now() + 31 * 24 * 3600 * 1000);
    listConversations(alice, all);
    expect(readIndex().tombstones.c1).toBeUndefined();
    deleteConversation("c2");
    deleteAllConversations(alice);
    expect(readIndex().tombstones).toEqual({});
  });

  it("writes nothing when storage refuses even after making room, and says so", () => {
    saveConversation(conv("c1", [turn(0)]));
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    expect(() => saveConversation(conv("c2", [turn(0)]))).toThrow();
    setItem.mockRestore();
    expect(localStorage.getItem(INDEX_KEY)).toContain("c1");
  });

  it("leaves no key behind for a new conversation whose index entry was refused, and keeps an existing one's", () => {
    // The conversation's key fits; the index write after it is refused, every time.
    const real = Storage.prototype.setItem;
    const refuseIndex = () =>
      vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key: string, value: string) {
        if (key === INDEX_KEY) throw new DOMException("full", "QuotaExceededError");
        real.call(this, key, value);
      });
    let spy = refuseIndex();
    expect(() => saveConversation(conv("c1", [turn(0)]))).toThrow(StorageRefused);
    spy.mockRestore();
    expect(localStorage.getItem("loom_assistant_c_c1")).toBeNull();
    expect(readIndex().entries).toEqual([]);

    saveConversation(conv("c2", [turn(0)]));
    spy = refuseIndex();
    expect(() => saveConversation(conv("c2", [turn(0), turn(1)]))).toThrow(StorageRefused);
    spy.mockRestore();
    expect(localStorage.getItem("loom_assistant_c_c2")).not.toBeNull();
    expect(readIndex().entries.map((e) => e.id)).toEqual(["c2"]);
  });

  it("stops listing a conversation whose key went to make room when the save is refused anyway", () => {
    saveConversation(conv("old", [turn(0)], { updated: Date.now() - 2000 }));
    saveConversation(conv("kept", [turn(0)], { updated: Date.now() - 1000 }));
    // Every write of the new conversation is refused; the index shrinking fits.
    const real = Storage.prototype.setItem;
    const spy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (
      this: Storage,
      key: string,
      value: string,
    ) {
      if (key === "loom_assistant_c_new") throw new DOMException("full", "QuotaExceededError");
      real.call(this, key, value);
    });
    expect(() => saveConversation(conv("new", [turn(0)]))).toThrow(StorageRefused);
    spy.mockRestore();
    const listed = readIndex().entries.map((e) => e.id);
    for (const id of listed) expect(localStorage.getItem(`loom_assistant_c_${id}`)).not.toBeNull();
    expect(listed).toEqual(["kept"]);
  });

  it("merges two tabs' copies turn by turn, the newer copy of each turn winning", () => {
    const mine = [turn(0), { ...turn(1), answer: "mine", at: 5 }];
    const theirs = [turn(0), { ...turn(1), answer: "theirs", at: 9 }, turn(2)];
    expect(mergeTurns(mine, theirs).map((t) => t.answer)).toEqual(["a0", "theirs", "a2"]);
  });
});
