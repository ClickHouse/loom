/** A Session resumes, appends, flushes, parks and notes under one scope. */
import { describe, expect, it } from "vitest";
import { Agent } from "./agent.js";
import { LoomClient } from "./client.js";
import { BackendUnavailable } from "./errors.js";
import { Transport } from "./transport.js";

type Call = { op: string; body: Record<string, unknown> };

function harness(replies: Record<string, unknown | ((body: Record<string, unknown>) => unknown)> = {}) {
  const calls: Call[] = [];
  const fetch = async (url: string, init: RequestInit): Promise<Response> => {
    const op = url.split("/v1/")[1];
    const body = JSON.parse(String(init.body)) as Record<string, unknown>;
    calls.push({ op, body });
    const r = replies[op];
    if (r instanceof Error)
      return new Response(JSON.stringify({ detail: { code: "X", message: r.message } }), { status: 503 });
    const out = typeof r === "function" ? (r as (b: Record<string, unknown>) => unknown)(body) : (r ?? {});
    return new Response(JSON.stringify(out), { status: 200 });
  };
  const t = new Transport({
    baseUrl: "http://x",
    fetch,
    logLevel: "off",
    sleep: async () => {},
    retry: { backoffMs: 1 },
  });
  const bot = new Agent("acme", "support", "bot", new LoomClient(t));
  return { bot, calls, ops: () => calls.map((c) => c.op) };
}

describe("Session", () => {
  it("stamps the session scope on every op and writes each turn to the transcript", async () => {
    const { bot, calls, ops } = harness({ "memory.set_from_messages": { written: [] } });
    const s = bot.session("thread-1", { userId: "u1", flushEvery: 0 });
    await s.append({ role: "user", content: "hi" }, { role: "assistant", content: "hello", name: "bot" });
    expect(ops()).toEqual(["conversation.create", "conversation.append", "conversation.append"]);
    for (const c of calls) {
      expect(c.body.session_id).toBe("thread-1");
      expect(c.body.user_id).toBe("u1");
      expect(c.body.org).toBe("acme");
    }
    expect(calls[0].body.thread_id).toBe("thread-1");
    expect(calls[2].body).toMatchObject({ role: "assistant", content: "hello", metadata: { name: "bot" } });
    expect(s.buffered.length).toBe(2);
    await s.close();
    expect(ops().at(-1)).toBe("memory.set_from_messages");
    expect(s.buffered.length).toBe(0);
    await s.close(); // nothing buffered → no call
    expect(ops().filter((o) => o === "memory.set_from_messages").length).toBe(1);
  });

  it("flushes every N turns, hands the previous tail to the extractor, and creates the thread once", async () => {
    const { bot, calls } = harness({ "memory.set_from_messages": { written: [{ memory_key: "k" }] } });
    const s = bot.session("t", { flushEvery: 2, carryOver: 1, transcript: true });
    const first = await s.append({ role: "user", content: "a" });
    expect(first).toBeUndefined();
    const second = await s.append({ role: "assistant", content: "b" });
    expect(second).toEqual({ written: [{ memory_key: "k" }] });
    await s.append({ role: "user", content: "c" }, { role: "assistant", content: "d" });
    const flushes = calls.filter((c) => c.op === "memory.set_from_messages");
    expect(flushes.length).toBe(2);
    expect(flushes[0].body.last_messages).toBeUndefined();
    expect(flushes[1].body.last_messages).toEqual([{ role: "assistant", content: "b" }]);
    expect((flushes[1].body.messages as unknown[]).length).toBe(2);
    expect(calls.filter((c) => c.op === "conversation.create").length).toBe(1);
  });

  it("keeps the turns buffered when extraction fails, and skips the transcript when told", async () => {
    const { bot, ops } = harness({ "memory.set_from_messages": new Error("down") });
    const s = bot.session("t", { flushEvery: 1, transcript: false });
    await expect(s.append({ role: "user", content: "a" })).rejects.toBeInstanceOf(BackendUnavailable);
    expect(s.buffered).toEqual([{ role: "user", content: "a" }]);
    expect(ops()).not.toContain("conversation.append");
  });

  it("stops writing the transcript, once warned, when the namespace has logging off", async () => {
    const { bot, ops } = harness({
      "conversation.append": { msg_id: "m", ts: "t", stored: false, reason: "logging_disabled" },
      "memory.set_from_messages": { written: [] },
    });
    const s = bot.session("t", { flushEvery: 0 });
    expect(s.transcriptStored).toBeUndefined();
    await s.append({ role: "user", content: "a" }, { role: "assistant", content: "b" });
    expect(s.transcriptStored).toBe(false);
    expect(ops().filter((o) => o === "conversation.append").length).toBe(1); // stopped after the first refusal
    await s.append({ role: "user", content: "c" });
    expect(ops().filter((o) => o === "conversation.append").length).toBe(1);
    expect(s.buffered.length).toBe(3); // extraction still gets every turn
    await s.close();
    expect(ops().at(-1)).toBe("memory.set_from_messages");
  });

  it("resume returns the pack as the system prompt plus the transcript tail, [] when there is none", async () => {
    const { bot, calls } = harness({
      "context.pack": { rendered: "## Working memory\n- plan", sections: {}, section_count: 1, token_budget: 2000 },
      "conversation.get": (b: Record<string, unknown>) => ({
        found: true,
        thread_id: b.thread_id,
        messages: [{ msg_id: "1", role: "user", content: "hi" }],
      }),
    });
    const s = bot.session("t", { userId: "u1" });
    const r = await s.resume({ query: "resume billing", tokenBudget: 1500, history: 5 });
    expect(r.system).toBe("## Working memory\n- plan");
    expect(r.messages).toEqual([{ msg_id: "1", role: "user", content: "hi" }]);
    const pack = calls.find((c) => c.op === "context.pack");
    expect(pack?.body).toMatchObject({
      query: "resume billing",
      mode: "startup",
      token_budget: 1500,
      session_id: "t",
      user_id: "u1",
    });
    const get = calls.find((c) => c.op === "conversation.get");
    expect(get?.body).toMatchObject({ thread_id: "t", include_messages: true, limit: 5 });

    const h2 = harness({
      "context.pack": { rendered: "", sections: {}, section_count: 0, token_budget: 2000 },
      "conversation.get": { found: false, thread_id: "t" },
    });
    expect((await h2.bot.session("t").resume()).messages).toEqual([]);
    const h3 = harness({ "context.pack": { rendered: "", sections: {}, section_count: 0, token_budget: 2000 } });
    expect((await h3.bot.session("t", { transcript: false }).resume()).messages).toEqual([]);
    expect(h3.ops()).toEqual(["context.pack"]);
  });

  it("offload, fetch and note map to context.offload, context.fetch and working_memory.append", async () => {
    const { bot, calls } = harness({
      "context.offload": { blob_key: "blob/abc", pointer: "[offloaded 3 bytes → context.fetch blob_key=blob/abc] log" },
      "context.fetch": { found: true, blob_key: "blob/abc", content: "x", next_offset: -1 },
      "working_memory.append": { block_key: "main", version: 2, char_limit: 4000, value_length: 10 },
    });
    const s = bot.session("t");
    const blob = await s.offload("log", { summary: "pytest run", ttlSeconds: 60 });
    expect(blob.pointer).toContain("blob/abc");
    expect(calls[0].body).toMatchObject({ content: "log", summary: "pytest run", ttl_seconds: 60, session_id: "t" });
    const got = await s.fetch("blob/abc", { pattern: "FAILED", contextLines: 2 });
    expect(got.content).toBe("x");
    expect(calls[1].body).toMatchObject({ blob_key: "blob/abc", pattern: "FAILED", context_lines: 2 });
    const n = await s.note("plan: migrate billing first");
    expect(n.version).toBe(2);
    expect(calls[2].body).toMatchObject({ addition: "plan: migrate billing first" });
  });
});
