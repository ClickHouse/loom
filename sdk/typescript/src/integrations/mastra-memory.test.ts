import { createHash } from "node:crypto";
import type { MastraDBMessage, StorageThreadType } from "@mastra/core/memory";
import { MastraCompositeStore, MemoryStorage } from "@mastra/core/storage";
import { Memory } from "@mastra/memory";
import { describe, expect, it, vi } from "vitest";
import { Agent } from "../agent.js";
import {
  AutomaticCaptureSuppressed,
  SessionCapture,
  SessionProtection,
  SessionProtectionUnavailable,
} from "../session-protection.js";
import { LoomMemoryStorage } from "./mastra.js";

process.env.MASTRA_TELEMETRY_DISABLED = "1";

type Row = {
  body: Record<string, unknown>;
  value: { kind: string; resourceId: string; data: Record<string, unknown> };
  hash: string;
  version: string;
};
type Call = { op: string; body: Record<string, unknown> };
const hash = (value: unknown) => createHash("sha256").update(JSON.stringify(value)).digest("hex");

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function harness(options: { root?: SessionProtection; token?: () => Promise<string> } = {}) {
  const rows = new Map<string, Row>();
  const calls: Call[] = [];
  let version = 1792436295126090000n;
  const controls = {
    cap: 2,
    emptyPages: 0,
    noHash: false,
    noVersion: false,
    disappear: false,
    refuseSet: false,
    refuseAbsence: false,
    beforeSet: undefined as ((row: Row) => void) | undefined,
    afterGet: undefined as ((body: Call["body"], found: boolean) => Promise<void>) | undefined,
    beforeWrite: undefined as ((body: Call["body"]) => Promise<void>) | undefined,
    afterWrite: undefined as ((row: Row) => void) | undefined,
    beforeDelete: undefined as ((body: Call["body"]) => void) | undefined,
    mirrored: undefined as ((op: string, body: Call["body"]) => boolean) | undefined,
    failMessage: "",
    failDelete: "",
  };
  const id = (body: Record<string, unknown>) =>
    JSON.stringify([body.org, body.namespace, body.agent, body.user_id, body.session_id, body.memory_key]);
  const rewrite = (row: Row, update: (row: Row) => void = () => {}) => {
    update(row);
    row.hash = hash(row.value);
    row.version = String(++version);
  };
  const fallback = (body: Call["body"]) =>
    [...rows.values()].find((row) =>
      ["org", "namespace", "user_id", "session_id", "memory_key"].every((field) => row.body[field] === body[field]),
    );
  const conflict = () =>
    new Response(JSON.stringify({ detail: { code: "PRECONDITION_FAILED", message: "Changed" } }), { status: 409 });
  const fetch = async (url: string, init: RequestInit) => {
    const op = url.split("/v1/")[1];
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    calls.push({ op, body });
    let reply: unknown = {};
    const key = id(body);
    if (op === "memory.set") {
      await controls.beforeWrite?.(body);
      const value = JSON.parse(body.value as string) as Row["value"];
      const current = rows.get(key);
      if (current && controls.beforeSet) {
        const update = controls.beforeSet;
        controls.beforeSet = undefined;
        rewrite(current, update);
      }
      if (body.if_match_sha256 === "absent") {
        if (current || controls.refuseAbsence) return conflict();
      } else if (body.if_match_sha256 && body.if_match_sha256 !== current?.hash) {
        return conflict();
      }
      if (value.data.id === controls.failMessage) {
        return new Response(JSON.stringify({ detail: { message: "Write refused" } }), { status: 422 });
      }
      if (controls.refuseSet) return new Response(JSON.stringify({ version: 0, status: "proposed" }));
      const row = { body: { ...body }, value, hash: hash(value), version: String(++version) };
      rows.set(key, row);
      controls.afterWrite?.(row);
      reply = { version: Number(row.version), memory_key: body.memory_key, content_sha256: row.hash };
    } else if (op === "memory.get") {
      const row = controls.disappear ? undefined : (rows.get(key) ?? fallback(body));
      const out = row
        ? {
            found: true,
            value: structuredClone(row.value),
            content_sha256: controls.noHash ? "" : row.hash,
            version: Number(row.version),
            version_text: controls.noVersion ? undefined : row.version,
            agent: row.body.agent,
            user_id: row.body.user_id,
            session_id: row.body.session_id,
          }
        : { found: false };
      reply = Object.fromEntries(
        Object.entries(out).filter(([field]) => field === "found" || (body.fields as string[]).includes(field)),
      );
      await controls.afterGet?.(body, !!row);
    } else if (op === "memory.list") {
      const scope = ["org", "namespace", "agent", "user_id", "session_id"];
      const matching = [...rows.values()].filter(
        (row) =>
          scope.every((field) => row.body[field] === body[field]) &&
          (body.memory_type === undefined || row.body.memory_type === body.memory_type) &&
          (row.body.memory_key as string).startsWith(body.prefix as string),
      );
      const scanned = [...Array.from({ length: controls.emptyPages }, () => null), ...matching];
      const offset = body.offset as number;
      const page = scanned.slice(offset, offset + controls.cap);
      reply = {
        entries: page.filter((row) => row !== null).map((row) => ({ memory_key: row.body.memory_key })),
        truncated: offset + controls.cap < scanned.length,
      };
    } else if (op === "memory.delete_version" || op === "memory.delete") {
      controls.beforeDelete?.(body);
      const row = rows.get(key) ?? (op === "memory.delete" ? fallback(body) : undefined);
      if (row?.value.data.id === controls.failDelete) {
        return new Response(JSON.stringify({ detail: { message: "Delete refused" } }), { status: 422 });
      }
      if (!row) reply = { found: false, tombstoned: false };
      else if (op === "memory.delete_version" && body.version !== row.version)
        reply = { found: true, tombstoned: false, reason: "version_changed" };
      else reply = { tombstoned: rows.delete(id(row.body)) };
    } else throw new Error(`Unexpected operation ${op}`);
    const observed = controls.mirrored?.(op, body) ? { ...(reply as object), mirrored: true } : reply;
    return new Response(JSON.stringify(observed));
  };
  const agent = new Agent("o", "n", "bot", {
    baseUrl: "http://x",
    fetch,
    logLevel: "off",
    sessionProtection: options.root,
    ...(options.token ? { tokenProvider: { origin: "http://x", token: options.token } } : {}),
  });
  const storage = (resourceId = "alice", storeId = "app", name = "bot") =>
    new LoomMemoryStorage(
      name === "bot" ? agent : new Agent("o", "n", name, { baseUrl: "http://x", fetch, logLevel: "off" }),
      { resourceId, storeId },
    );
  return { agent, rows, calls, controls, storage, rewrite };
}

const date = (second: number) => new Date(Date.UTC(2025, 0, 1, 0, 0, second));
const thread = (id: string, second = 0, resourceId = "alice"): StorageThreadType => ({
  id,
  resourceId,
  title: `Thread ${id}`,
  createdAt: date(second),
  updatedAt: date(second),
  metadata: { group: "work" },
});
const message = (id: string, threadId = "t1", second = 0, resourceId = "alice"): MastraDBMessage => ({
  id,
  threadId,
  resourceId,
  role: "user",
  createdAt: date(second),
  content: { format: 2, parts: [{ type: "text", text: "same text" }], metadata: { label: "kept", count: 1 } },
});

describe("LoomMemoryStorage native memory domain", () => {
  it("serves native Memory save, recall, working-memory and delete methods without a vector store", async () => {
    const h = harness();
    const native = () =>
      new Memory({
        storage: new MastraCompositeStore({ id: "app", domains: { memory: h.storage() } }),
        options: {
          semanticRecall: false,
          observationalMemory: false,
          workingMemory: { enabled: true, scope: "resource" },
        },
      });
    await native().saveThread({ thread: thread("t1") });
    await native().saveMessages({ messages: [message("m1")] });
    expect(
      (await native().recall({ threadId: "t1", resourceId: "alice", perPage: false })).messages.map((item) => item.id),
    ).toEqual(["m1"]);
    await native().updateWorkingMemory({ threadId: "t1", resourceId: "alice", workingMemory: "Resource notes" });
    expect(await native().getWorkingMemory({ threadId: "t1", resourceId: "alice" })).toBe("Resource notes");
    await native().deleteThread("t1");
    await expect(native().recall({ threadId: "t1", resourceId: "alice", perPage: false })).rejects.toThrow(
      "No thread found",
    );
    expect((await h.storage().listMessages({ threadId: "t1", perPage: false })).messages).toEqual([]);
  });
  it("is a native memory-only composite domain and refuses unsupported capabilities", async () => {
    const storage = harness().storage();
    const composite = new MastraCompositeStore({ id: "test", domains: { memory: storage } });
    expect(await composite.getStore("memory")).toBe(storage);
    expect(await composite.getStore("workflows")).toBeUndefined();
    expect(storage).toBeInstanceOf(MemoryStorage);
    expect(storage.supportsRunFencing()).toBe(false);
    expect(storage.supportsObservationalMemory).toBe(false);
    expect(storage.supportsAtomicWorkingMemoryMerge).toBe(false);
    expect(storage.supportsPartialThreadUpdate).toBe(true);
    await storage.init();
    await expect(storage.saveThread({ thread: thread("t1"), fence: {} as never })).rejects.toThrow("fencing");
    await expect(storage.copyThread({ sourceThreadId: "t1" })).rejects.toThrow("not implemented");
  });

  it.each(["", "-", "*", " alice", "alice\u0085", "alice\u001c"])("rejects noncanonical user %j", (resourceId) => {
    const { agent, calls } = harness();
    expect(() => new LoomMemoryStorage(agent, { resourceId })).toThrow("resourceId");
    expect(calls).toEqual([]);
  });

  it("snapshots constructor options and the client identity before the first write", async () => {
    const { agent, calls } = harness();
    const options = { resourceId: "alice", storeId: "app" };
    const storage = new LoomMemoryStorage(agent, options);
    options.resourceId = "bob";
    options.storeId = "other";
    Object.assign(agent, { org: "other", namespace: "other", agent: "other" });
    await storage.saveThread({ thread: thread("t1") });
    expect(
      calls.every(
        (call) =>
          call.body.org === "o" &&
          call.body.namespace === "n" &&
          call.body.agent === "bot" &&
          call.body.user_id === "alice",
      ),
    ).toBe(true);
    expect(calls[0].body).toMatchObject({ session_id: "-", memory_type: "episodic", clear_expiry: true });
  });

  it("resolves identical thread/message IDs from a fresh instance within each real user", async () => {
    const h = harness();
    for (const user of ["alice", "bob"]) {
      await h.storage(user).saveThread({ thread: thread("t1", 0, user) });
      await h.storage(user).saveMessages({ messages: [message("m1", "t1", 0, user)] });
    }
    const alice = h.storage();
    expect((await alice.getThreadById({ threadId: "t1" }))?.resourceId).toBe("alice");
    expect((await alice.listMessagesById({ messageIds: ["m1"] })).messages[0].resourceId).toBe("alice");
    expect((await h.storage("bob").listMessagesById({ messageIds: ["m1"] })).messages[0].resourceId).toBe("bob");
    expect([...h.rows.values()].every((row) => row.body.user_id === row.value.resourceId)).toBe(true);
  });

  it("returns missing across resources and rejects every cross-resource write before a batch starts", async () => {
    const h = harness();
    const storage = h.storage();
    await storage.saveThread({ thread: thread("t1") });
    const before = h.calls.length;
    expect(await storage.getThreadById({ threadId: "t1", resourceId: "bob" })).toBeNull();
    expect(await storage.getResourceById({ resourceId: "bob" })).toBeNull();
    expect((await storage.listThreads({ filter: { resourceId: "bob" } })).threads).toEqual([]);
    expect(
      (await storage.listMessages({ threadId: "t1", resourceId: "bob", include: [{ id: "m1" }] })).messages,
    ).toEqual([]);
    expect((await storage.listMessagesByResourceId({ resourceId: "bob" })).messages).toEqual([]);
    await expect(storage.saveThread({ thread: thread("t2", 0, "bob") })).rejects.toThrow("resource");
    await expect(storage.saveMessages({ messages: [message("good"), message("bad", "t1", 0, "bob")] })).rejects.toThrow(
      "resource",
    );
    await expect(
      storage.updateMessages({ messages: [{ id: "good" }, { id: "bad", resourceId: "bob" }] }),
    ).rejects.toThrow("resource");
    await expect(storage.updateResource({ resourceId: "bob", workingMemory: "secret" })).rejects.toThrow("resource");
    await expect(storage.updateThreadResourceId({ threadId: "t1", resourceId: "bob" })).rejects.toThrow("resource");
    expect(h.calls).toHaveLength(before);
  });

  it("round-trips full native payloads and dates and upserts a repeated message ID", async () => {
    const h = harness();
    await h.storage().saveThread({ thread: thread("t1") });
    const original = message("m1");
    original.role = "signal";
    original.type = "native";
    original.content.parts = [{ type: "text", text: "x".repeat(6000) }];
    original.content.providerMetadata = { test: { detail: "preserved" } };
    original.content.annotations = [{ source: "native" }];
    await h.storage().saveMessages({ messages: [original, message("m2")] });
    await h.storage().saveMessages({ messages: [original] });
    original.content.parts = [];
    for (const row of h.rows.values()) row.body.memory_type = "semantic";
    const resumed = h.storage();
    const out = await resumed.listMessages({ threadId: "t1", perPage: false });
    expect(out.messages).toHaveLength(2);
    expect(out.messages[0]).toMatchObject({ role: "signal", type: "native", createdAt: date(0) });
    expect(out.messages[0].content.parts).toEqual([{ type: "text", text: "x".repeat(6000) }]);
    expect(out.messages[0].content.providerMetadata).toEqual({ test: { detail: "preserved" } });
    expect(out.messages[0].content.annotations).toEqual([{ source: "native" }]);
    expect((await resumed.getThreadById({ threadId: "t1" }))?.createdAt).toEqual(date(0));
  });

  it("reapplies a partial metadata update after a concurrent title change without clobbering it", async () => {
    const h = harness();
    const storage = h.storage();
    await storage.saveThread({ thread: thread("t1") });
    h.controls.beforeSet = (row) => {
      row.value.data.title = "Concurrent title";
    };
    const updated = await storage.patchThread({ id: "t1", metadata: { added: true } });
    expect(updated.title).toBe("Concurrent title");
    expect(updated.metadata).toEqual({ group: "work", added: true });
    expect(h.calls.filter((call) => call.op === "memory.set" && call.body.if_match_sha256)).toHaveLength(2);
    await storage.updateThreadMetadata({
      id: "t1",
      update: (current) => ({ count: Number(current.metadata?.count ?? 0) + 1 }),
    });
    expect((await h.storage().getThreadById({ threadId: "t1" }))?.metadata).toMatchObject({ added: true, count: 1 });
    const writes = h.calls.filter((call) => call.op === "memory.set").length;
    await storage.updateThreadMetadata({ id: "t1", update: () => undefined });
    expect(h.calls.filter((call) => call.op === "memory.set")).toHaveLength(writes);
    await expect(storage.updateThread({ id: "missing", title: "x" })).rejects.toThrow("not found");
  });

  it("stores resource working memory and preserves omitted fields on metadata updates", async () => {
    const h = harness();
    await h.storage().updateResource({ resourceId: "alice", workingMemory: "preferences", metadata: { first: 1 } });
    await h.storage().updateResource({ resourceId: "alice", metadata: { second: 2 } });
    const resource = await h.storage().getResourceById({ resourceId: "alice" });
    expect(resource).toMatchObject({ id: "alice", workingMemory: "preferences", metadata: { first: 1, second: 2 } });
    expect(resource?.createdAt).toBeInstanceOf(Date);
    expect([...h.rows.values()].every((row) => row.body.user_id === "alice")).toBe(true);
  });

  it("scans capped and empty projected pages before native thread filtering and pagination", async () => {
    const h = harness();
    h.controls.cap = 1;
    h.controls.emptyPages = 1;
    const storage = h.storage();
    for (let i = 0; i < 4; i++) {
      await storage.saveThread({
        thread: { ...thread(`t${i}`, i), metadata: { nested: { a: 1, b: 2 }, group: i < 3 ? "work" : "other" } },
      });
    }
    const out = await storage.listThreads({
      filter: { metadata: { nested: { b: 2, a: 1 }, group: "work" } },
      page: 1,
      perPage: 1,
    });
    expect(out).toMatchObject({ total: 3, page: 1, perPage: 1, hasMore: true });
    expect(out.threads.map((item) => item.id)).toEqual(["t1"]);
    expect((await storage.listThreads({ perPage: false })).threads).toHaveLength(4);
    expect((await storage.listThreads({ perPage: 0 })).threads).toEqual([]);
    await expect(storage.listThreads({ page: 1, perPage: false })).rejects.toThrow();
    await expect(storage.listThreads({ perPage: -1 })).rejects.toThrow();
  });

  it("applies native date/scalar metadata filters and adds deduplicated cross-thread context", async () => {
    const h = harness();
    const storage = h.storage();
    await storage.saveMessages({
      messages: [
        message("a", "t1", 0),
        message("b", "t1", 1),
        message("c", "t1", 2),
        message("d", "t2", 3),
        message("e", "t2", 4),
      ],
    });
    const out = await storage.listMessages({
      threadId: ["t1"],
      perPage: 1,
      filter: {
        dateRange: { start: date(0), startExclusive: true, end: date(2), endExclusive: true },
        metadata: { count: 1 },
      },
      include: [{ id: "d", withNextMessages: 1 }, { id: "d" }],
    });
    expect(out.messages.map((item) => item.id)).toEqual(["b", "d", "e"]);
    expect(out.total).toBe(1);
    expect(out.hasMore).toBe(false);
    const allIncluded = await storage.listMessages({
      threadId: "t1",
      perPage: 1,
      include: [{ id: "b", withPreviousMessages: 1, withNextMessages: 1 }],
    });
    expect(allIncluded.messages.map((item) => item.id)).toEqual(["a", "b", "c"]);
    expect(allIncluded.hasMore).toBe(false);
    expect(await storage.listMessagesByResourceId({ resourceId: "alice", perPage: 0 })).toMatchObject({
      total: 5,
      hasMore: true,
      messages: [],
    });
    expect(
      (
        await storage.listMessages({ threadId: "t1", perPage: 0, include: [{ id: "b", withPreviousMessages: 1 }] })
      ).messages.map((item) => item.id),
    ).toEqual(["a", "b"]);
    expect(
      (
        await storage.listMessagesByResourceId({ resourceId: "alice", perPage: 2, orderBy: { direction: "DESC" } })
      ).messages.map((item) => item.id),
    ).toEqual(["e", "d"]);
    expect((await storage.listMessages({ threadId: "t1", filter: { metadata: { count: "1" } } })).messages).toEqual([]);
  });

  it("merges message metadata, preserves createdAt and role, and touches both threads on a move", async () => {
    const h = harness();
    const storage = h.storage();
    await storage.saveThread({ thread: thread("t1") });
    await storage.saveThread({ thread: thread("t2") });
    await storage.saveMessages({ messages: [message("m1")] });
    const before = await storage.getThreadById({ threadId: "t1" });
    if (!before) throw new Error("Missing source thread");
    const updated = await storage.updateMessages({
      messages: [
        { id: "missing" },
        { id: "m1", threadId: "t2", role: "system", content: { metadata: { added: true } } },
      ],
    });
    expect(updated).toHaveLength(1);
    expect(updated[0]).toMatchObject({
      role: "system",
      threadId: "t2",
      createdAt: date(0),
      content: { metadata: { label: "kept", count: 1, added: true } },
    });
    expect(updated[0].content.parts).toEqual(message("m1").content.parts);
    await storage.updateMessages({
      messages: [{ id: "m1", role: undefined, threadId: undefined, content: { metadata: undefined } }],
    });
    const preserved = (await h.storage().listMessagesById({ messageIds: ["m1"] })).messages[0];
    expect(preserved).toMatchObject({ role: "system", threadId: "t2", content: { metadata: { added: true } } });
    expect((await storage.getThreadById({ threadId: "t1" }))?.updatedAt.getTime()).toBeGreaterThan(
      before.updatedAt.getTime(),
    );
    expect((await storage.getThreadById({ threadId: "t2" }))?.updatedAt.getTime()).toBeGreaterThan(date(0).getTime());
  });

  it("cascades deletes and clears only its own store and user", async () => {
    const h = harness();
    for (const [user, store] of [
      ["alice", "app"],
      ["bob", "app"],
      ["alice", "other"],
    ]) {
      const storage = h.storage(user, store);
      await storage.saveThread({ thread: thread("t1", 0, user) });
      await storage.saveThread({ thread: thread("t2", 0, user) });
      await storage.saveMessages({ messages: [message("m1", "t1", 0, user), message("m2", "t2", 0, user)] });
      await storage.updateResource({ resourceId: user, workingMemory: "notes" });
    }
    await h.storage().deleteMessages(["m1", "missing"]);
    expect((await h.storage().listMessagesById({ messageIds: ["m1", "m2"] })).messages.map((item) => item.id)).toEqual([
      "m2",
    ]);
    await h.storage().deleteThread({ threadId: "t2" });
    expect((await h.storage().listMessagesById({ messageIds: ["m2"] })).messages).toEqual([]);
    await h.storage().dangerouslyClearAll();
    expect((await h.storage().listThreads({})).threads).toEqual([]);
    expect((await h.storage("bob").listMessagesById({ messageIds: ["m1"] })).messages).toHaveLength(1);
    expect((await h.storage("alice", "other").listMessagesById({ messageIds: ["m1"] })).messages).toHaveLength(1);
  });

  it("propagates partial batch failure, refuses missing hashes and fails a disappearing history read", async () => {
    const h = harness();
    h.controls.failMessage = "bad";
    await expect(h.storage().saveMessages({ messages: [message("good"), message("bad")] })).rejects.toThrow(
      "Write refused",
    );
    expect((await h.storage().listMessagesById({ messageIds: ["good", "bad"] })).messages).toHaveLength(1);
    await h.storage().saveThread({ thread: thread("t1") });
    h.controls.noHash = true;
    await expect(h.storage().updateThread({ id: "t1", title: "change" })).rejects.toThrow("hash");
    h.controls.noHash = false;
    h.controls.disappear = true;
    await expect(h.storage().listThreads({})).rejects.toThrow("changed during listing");
  });

  it("does not acknowledge a policy-held write as persisted", async () => {
    const h = harness();
    h.controls.refuseSet = true;
    await expect(h.storage().saveThread({ thread: thread("t1") })).rejects.toThrow("did not persist");
  });

  it.each([
    { first: 0, metadataOnly: false },
    { first: 1, metadataOnly: false },
    { first: 0, metadataOnly: true },
    { first: 1, metadataOnly: true },
  ])(
    "preserves disjoint first resource patches in order $first (metadata only: $metadataOnly)",
    async ({ first, metadataOnly }) => {
      const h = harness();
      const bothRead = deferred();
      const firstWritten = deferred();
      let missingReads = 0;
      let creations = 0;
      let winnerCreatedAt: unknown;
      h.controls.afterGet = async (body, found) => {
        if (!found && String(body.memory_key).includes("/resource/") && missingReads < 2) {
          if (++missingReads === 2) bothRead.resolve();
          await bothRead.promise;
        }
      };
      h.controls.beforeWrite = async (body) => {
        const data = JSON.parse(body.value as string).data;
        const index = data.metadata.first === 1 && data.metadata.second === undefined ? 0 : 1;
        if (index !== first) await firstWritten.promise;
      };
      h.controls.afterWrite = (row) => {
        if (++creations === 1) winnerCreatedAt = row.value.data.createdAt;
        firstWritten.resolve();
      };
      await Promise.all([
        h.storage().updateResource({
          resourceId: "alice",
          ...(metadataOnly ? {} : { workingMemory: "" }),
          metadata: { first: 1, shared: "a" },
        }),
        h.storage().updateResource({ resourceId: "alice", metadata: { second: 2, shared: "b" } }),
      ]);
      const out = await h.storage().getResourceById({ resourceId: "alice" });
      expect(out).toMatchObject({
        metadata: { first: 1, second: 2, shared: first === 0 ? "b" : "a" },
      });
      expect(out?.workingMemory).toBe(metadataOnly ? undefined : "");
      expect(out?.createdAt.toISOString()).toBe(winnerCreatedAt);
      const writes = h.calls.filter((call) => call.op === "memory.set");
      expect(writes).toHaveLength(3);
      expect(writes.filter((call) => call.body.if_match_sha256 === "absent")).toHaveLength(2);
      expect(writes.every((call) => !!call.body.if_match_sha256)).toBe(true);
      expect(creations).toBe(2);
    },
  );

  it("retries a first metadata-only patch after clear and recreates without old fields or dates", async () => {
    const h = harness();
    await h.storage().saveResource({
      resource: {
        id: "alice",
        workingMemory: "old",
        metadata: { old: true },
        createdAt: date(0),
        updatedAt: date(0),
      },
    });
    await h.storage().dangerouslyClearAll();
    const bothRead = deferred();
    let reads = 0;
    h.controls.afterGet = async (_body, found) => {
      if (!found && reads < 2) {
        if (++reads === 2) bothRead.resolve();
        await bothRead.promise;
      }
    };
    await Promise.all([
      h.storage().updateResource({ resourceId: "alice", metadata: { first: 1 } }),
      h.storage().updateResource({ resourceId: "alice", metadata: { second: 2 } }),
    ]);
    const out = await h.storage().getResourceById({ resourceId: "alice" });
    expect(out?.metadata).toEqual({ first: 1, second: 2 });
    expect(out?.workingMemory).toBeUndefined();
    expect(out?.createdAt).not.toEqual(date(0));
    expect(
      h.calls
        .filter((call) => call.op === "memory.set")
        .slice(1)
        .every((call) => !!call.body.if_match_sha256),
    ).toBe(true);
  });

  it("creates an empty resource conditionally and refuses an old server without an unconditional fallback", async () => {
    const h = harness();
    const empty = await h.storage().updateResource({ resourceId: "alice" });
    expect(empty).toMatchObject({ id: "alice" });
    expect(empty.createdAt).toEqual(empty.updatedAt);
    await h.storage().dangerouslyClearAll();
    h.controls.refuseAbsence = true;
    const before = h.calls.length;
    await expect(h.storage().updateResource({ resourceId: "alice", metadata: { flag: true } })).rejects.toMatchObject({
      code: "PRECONDITION_FAILED",
    });
    const writes = h.calls.slice(before).filter((call) => call.op === "memory.set");
    expect(writes).toHaveLength(4);
    expect(writes.every((call) => call.body.if_match_sha256 === "absent")).toBe(true);
    expect(h.rows.size).toBe(0);
  });

  it("keeps a concurrent existing resource patch and fails if its hash is omitted", async () => {
    const h = harness();
    await h.storage().updateResource({ resourceId: "alice", workingMemory: "notes" });
    h.controls.beforeSet = (row) => {
      row.value.data.metadata = { winner: true };
    };
    const out = await h.storage().updateResource({ resourceId: "alice", metadata: { added: true } });
    expect(out).toMatchObject({ workingMemory: "notes", metadata: { winner: true, added: true } });
    h.controls.noHash = true;
    await expect(h.storage().updateResource({ resourceId: "alice", workingMemory: "change" })).rejects.toThrow("hash");
  });

  it("keeps another agent's namesake thread and still cleans own orphan messages", async () => {
    const h = harness();
    await h.storage("alice", "app", "other").saveThread({ thread: thread("t1") });
    await h.storage().deleteThread({ threadId: "t1" });
    expect(await h.storage("alice", "app", "other").getThreadById({ threadId: "t1" })).not.toBeNull();
    await h.storage().saveMessages({ messages: [message("orphan")] });
    await h.storage().deleteThread({ threadId: "t1" });
    expect((await h.storage().listMessagesById({ messageIds: ["orphan"] })).messages).toEqual([]);
    expect(await h.storage("alice", "app", "other").getThreadById({ threadId: "t1" })).not.toBeNull();
    expect(h.calls.filter((call) => call.op === "memory.delete")).toEqual([]);
  });

  it.each(["message", "thread", "clear"])(
    "does not fall back after the owned row disappears during %s deletion",
    async (path) => {
      const h = harness();
      for (const name of ["bot", "other"]) {
        await h.storage("alice", "app", name).saveThread({ thread: thread("t1") });
        await h.storage("alice", "app", name).saveMessages({ messages: [message("m1")] });
      }
      h.controls.beforeDelete = (body) => {
        for (const [key, row] of h.rows) {
          if (row.body.agent === body.agent && row.body.memory_key === body.memory_key) h.rows.delete(key);
        }
      };
      if (path === "message") await h.storage().deleteMessages(["m1"]);
      else if (path === "thread") await h.storage().deleteThread({ threadId: "t1" });
      else await h.storage().dangerouslyClearAll();
      expect(await h.storage("alice", "app", "other").getThreadById({ threadId: "t1" })).not.toBeNull();
      expect((await h.storage("alice", "app", "other").listMessagesById({ messageIds: ["m1"] })).messages).toHaveLength(
        1,
      );
      expect(h.calls.filter((call) => call.op === "memory.delete")).toEqual([]);
    },
  );

  it("deletes with the lossless projected version and permits an explicit later upsert", async () => {
    const h = harness();
    await h.storage().saveThread({ thread: thread("t1") });
    await h.storage().deleteThread({ threadId: "t1" });
    const deletion = h.calls.find((call) => call.op === "memory.delete_version");
    expect(deletion?.body).toMatchObject({
      agent: "bot",
      user_id: "alice",
      session_id: "-",
      version: "1792436295126090001",
    });
    expect(
      h.calls
        .filter((call) => call.op === "memory.get")
        .every((call) => (call.body.fields as string[]).includes("version_text")),
    ).toBe(true);
    expect(String(Number(deletion?.body.version))).not.toBe(deletion?.body.version);
    await h.storage().saveThread({ thread: { ...thread("t1"), title: "Recreated" } });
    expect((await h.storage().getThreadById({ threadId: "t1" }))?.title).toBe("Recreated");
  });

  it("refuses deletion without an exact version before mutation", async () => {
    const h = harness();
    await h.storage().saveThread({ thread: thread("t1") });
    h.controls.noVersion = true;
    await expect(h.storage().deleteThread({ threadId: "t1" })).rejects.toThrow("exact version");
    expect(h.calls.some((call) => call.op.startsWith("memory.delete"))).toBe(false);
    expect(h.rows.size).toBe(1);
  });

  it.each(["move", "same-content"])("preserves a newer message version after a %s rewrite", async (kind) => {
    const h = harness();
    await h.storage().saveThread({ thread: thread("t1") });
    await h.storage().saveMessages({ messages: [message("m1")] });
    h.controls.beforeDelete = () => {
      h.controls.beforeDelete = undefined;
      const row = [...h.rows.values()].find((row) => row.value.kind === "message");
      if (!row) throw new Error("Missing message fixture");
      h.rewrite(row, (row) => {
        if (kind === "move") row.value.data.threadId = "t2";
      });
    };
    await expect(h.storage().deleteThread({ threadId: "t1" })).rejects.toThrow("changed during deletion");
    const out = (await h.storage().listMessagesById({ messageIds: ["m1"] })).messages;
    expect(out).toHaveLength(1);
    expect(out[0].threadId).toBe(kind === "move" ? "t2" : "t1");
    expect(await h.storage().getThreadById({ threadId: "t1" })).not.toBeNull();
  });

  it("captures the parent version before cascading and preserves a concurrent parent rewrite", async () => {
    const h = harness();
    await h.storage().saveThread({ thread: thread("t1") });
    await h.storage().saveMessages({ messages: [message("m1")] });
    h.controls.beforeDelete = () => {
      h.controls.beforeDelete = undefined;
      const row = [...h.rows.values()].find((row) => row.value.kind === "thread");
      if (!row) throw new Error("Missing thread fixture");
      h.rewrite(row);
    };
    await expect(h.storage().deleteThread({ threadId: "t1" })).rejects.toThrow("changed during deletion");
    expect((await h.storage().listMessagesById({ messageIds: ["m1"] })).messages).toEqual([]);
    expect(await h.storage().getThreadById({ threadId: "t1" })).not.toBeNull();
  });

  it("propagates a later child deletion failure without deleting the parent", async () => {
    const h = harness();
    await h.storage().saveThread({ thread: thread("t1") });
    await h.storage().saveMessages({ messages: [message("m1"), message("m2")] });
    h.controls.failDelete = "m2";
    await expect(h.storage().deleteThread({ threadId: "t1" })).rejects.toThrow("Delete refused");
    expect((await h.storage().listMessagesById({ messageIds: ["m1", "m2"] })).messages.map((row) => row.id)).toEqual([
      "m2",
    ]);
    expect(await h.storage().getThreadById({ threadId: "t1" })).not.toBeNull();
  });
});

function captureFamily(beforeCommit: () => Promise<void> = async () => {}) {
  const state = { value: "clean" as "clean" | "protected", marks: 0 };
  const root = new SessionProtection({
    async read() {
      return state.value;
    },
    async mark() {
      state.marks++;
      await beforeCommit();
      state.value = "protected";
    },
  });
  return { root, state };
}

function captureStorage(agent: Agent, capture: SessionCapture) {
  const options = { resourceId: "alice", storeId: "app", capture };
  return new LoomMemoryStorage(agent, options);
}

function bindStorageClient(agent: Agent, root: SessionProtection) {
  Object.defineProperty(agent.client.transport, "sessionProtection", { value: root, configurable: true });
}

describe("LoomMemoryStorage conversation capture ownership", () => {
  it.each(["thread", "messages", "thread update", "message update", "resource", "history"])(
    "suppresses protected automatic %s before the captured client sends HTTP",
    async (path) => {
      const family = captureFamily();
      const h = harness({ root: family.root });
      await h.storage().saveThread({ thread: thread("t1") });
      await h.storage().saveMessages({ messages: [message("m1")] });
      const storage = captureStorage(h.agent, new SessionCapture([family.root]));
      const before = JSON.stringify([...h.rows]);
      h.calls.length = 0;
      family.state.value = "protected";
      const operation = () => {
        if (path === "thread") return storage.saveThread({ thread: thread("new") });
        if (path === "messages") return storage.saveMessages({ messages: [message("new")] });
        if (path === "thread update") return storage.updateThread({ id: "t1", title: "private" });
        if (path === "message update") return storage.updateMessages({ messages: [{ id: "m1", role: "assistant" }] });
        if (path === "resource") return storage.updateResource({ resourceId: "alice", workingMemory: "private" });
        return storage.listMessagesById({ messageIds: ["m1"] });
      };
      await expect(operation()).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
      expect(h.calls).toEqual([]);
      expect(JSON.stringify([...h.rows])).toBe(before);
    },
  );

  it("snapshots the supplied capture instead of accepting a later clean replacement", async () => {
    const producer = captureFamily();
    const replacement = captureFamily();
    const h = harness();
    const options = { resourceId: "alice", storeId: "app", capture: new SessionCapture([producer.root]) };
    const storage = new LoomMemoryStorage(h.agent, options);
    options.capture = new SessionCapture([replacement.root]);
    producer.state.value = "protected";
    await expect(storage.saveThread({ thread: thread("t1") })).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(h.calls).toEqual([]);
    expect(replacement.state.value).toBe("clean");
  });

  it("propagates automatic capture refusal through native Memory without acknowledging messages", async () => {
    const family = captureFamily();
    const h = harness({ root: family.root });
    await h.storage().saveThread({ thread: thread("t1") });
    const native = new Memory({
      storage: new MastraCompositeStore({
        id: "capture",
        domains: { memory: captureStorage(h.agent, new SessionCapture([family.root])) },
      }),
      options: { semanticRecall: false, observationalMemory: false },
    });
    family.state.value = "protected";
    h.calls.length = 0;
    await expect(native.saveMessages({ messages: [message("private")] })).rejects.toBeInstanceOf(
      AutomaticCaptureSuppressed,
    );
    expect(h.calls).toEqual([]);
    expect([...h.rows.values()].map((row) => row.value.data.id)).toEqual(["t1"]);
  });

  it("retains the constructor client family after its bound family changes", async () => {
    const constructorFamily = captureFamily();
    const producer = captureFamily();
    const replacement = captureFamily();
    const h = harness({ root: constructorFamily.root });
    const storage = captureStorage(h.agent, new SessionCapture([producer.root]));
    bindStorageClient(h.agent, replacement.root);
    constructorFamily.state.value = "protected";
    await expect(storage.saveThread({ thread: thread("t1") })).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(h.calls).toEqual([]);
    expect(producer.state.value).toBe("clean");
    expect(replacement.state.value).toBe("clean");
  });

  it("retains a completed callback family when it is marked before the next client rebind", async () => {
    const constructorFamily = captureFamily();
    const producer = captureFamily();
    const earlier = captureFamily();
    const replacement = captureFamily();
    const h = harness({ root: constructorFamily.root });
    const storage = captureStorage(h.agent, new SessionCapture([producer.root]));
    bindStorageClient(h.agent, earlier.root);
    await storage.saveThread({ thread: thread("prior") });
    expect(earlier.state.value).toBe("clean");
    await earlier.root.mark();
    bindStorageClient(h.agent, replacement.root);
    const before = JSON.stringify([...h.rows]);
    h.calls.length = 0;
    await expect(storage.saveMessages({ messages: [message("unsent", "prior")] })).rejects.toBeInstanceOf(
      AutomaticCaptureSuppressed,
    );
    expect(h.calls).toEqual([]);
    expect(JSON.stringify([...h.rows])).toBe(before);
    expect([constructorFamily.state.value, producer.state.value, replacement.state.value]).toEqual([
      "clean",
      "clean",
      "clean",
    ]);
  });

  it("durably marks a completed callback family before returning a later marked recall after rebind", async () => {
    const release = deferred();
    const observed = deferred();
    const earlier = captureFamily(async () => {
      observed.resolve();
      await release.promise;
    });
    const constructorFamily = captureFamily();
    const producer = captureFamily();
    const replacement = captureFamily();
    const h = harness({ root: constructorFamily.root });
    await h.storage().updateResource({ resourceId: "alice", workingMemory: "retained notes" });
    const storage = captureStorage(h.agent, new SessionCapture([producer.root]));
    bindStorageClient(h.agent, earlier.root);
    expect((await storage.getResourceById({ resourceId: "alice" }))?.workingMemory).toBe("retained notes");
    expect(earlier.state.value).toBe("clean");
    bindStorageClient(h.agent, replacement.root);
    h.controls.mirrored = (op) => op === "memory.get";
    h.calls.length = 0;
    let returned = false;
    const read = storage.getResourceById({ resourceId: "alice" }).then((resource) => {
      returned = true;
      return resource;
    });
    try {
      const first = await Promise.race([read.then(() => "returned"), observed.promise.then(() => "marking")]);
      expect(first).toBe("marking");
      expect(returned).toBe(false);
      expect(earlier.state.value).toBe("clean");
    } finally {
      release.resolve();
      await read;
    }
    expect((await read)?.workingMemory).toBe("retained notes");
    expect(earlier.state.marks).toBe(1);
    expect([earlier.state.value, constructorFamily.state.value, producer.state.value, replacement.state.value]).toEqual(
      ["protected", "protected", "protected", "protected"],
    );
    expect(h.calls.map((call) => call.op)).toEqual(["memory.get"]);
  });

  it("retains a callback family before hashing when that client is rebound during the hash", async () => {
    const constructorFamily = captureFamily();
    const producer = captureFamily();
    const callback = captureFamily();
    const replacement = captureFamily();
    const h = harness({ root: constructorFamily.root });
    const storage = captureStorage(h.agent, new SessionCapture([producer.root]));
    const entered = deferred();
    const release = deferred();
    const digest = globalThis.crypto.subtle.digest.bind(globalThis.crypto.subtle);
    const hashing = vi.spyOn(globalThis.crypto.subtle, "digest").mockImplementation(async (algorithm, data) => {
      if (new TextDecoder().decode(data) === "paused-message") {
        entered.resolve();
        await release.promise;
      }
      return digest(algorithm, data);
    });
    bindStorageClient(h.agent, callback.root);
    const saved = storage.saveMessages({ messages: [message("paused-message")] });
    try {
      await entered.promise;
      await callback.root.mark();
      bindStorageClient(h.agent, replacement.root);
    } finally {
      release.resolve();
      hashing.mockRestore();
    }
    await expect(saved).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(h.calls).toEqual([]);
    expect(producer.state.value).toBe("clean");
    expect(replacement.state.value).toBe("clean");
  });

  it("rechecks retained capture families after credentials mark and rebind the actual client", async () => {
    const constructorFamily = captureFamily();
    const producer = captureFamily();
    const replacement = captureFamily();
    const h = harness({
      root: constructorFamily.root,
      token: async () => {
        await constructorFamily.root.mark();
        bindStorageClient(h.agent, replacement.root);
        return "token";
      },
    });
    const storage = captureStorage(h.agent, new SessionCapture([producer.root]));
    await expect(storage.saveThread({ thread: thread("t1") })).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(h.calls).toEqual([]);
    expect(producer.state.value).toBe("clean");
    expect(replacement.state.value).toBe("clean");
  });

  it("waits for every contributing family before returning marked native working memory", async () => {
    const release = deferred();
    const first = captureFamily(() => release.promise);
    const callbackObserved = deferred();
    const callback = captureFamily(async () => callbackObserved.resolve());
    const producer = captureFamily();
    const h = harness({ root: first.root });
    await h.storage().updateResource({ resourceId: "alice", workingMemory: "retained notes" });
    const storage = captureStorage(h.agent, new SessionCapture([producer.root]));
    bindStorageClient(h.agent, callback.root);
    h.controls.mirrored = (op) => op === "memory.get";
    h.calls.length = 0;
    let returned = false;
    const read = storage.getResourceById({ resourceId: "alice" }).then((resource) => {
      returned = true;
      return resource;
    });
    try {
      await callbackObserved.promise;
      expect(first.state.marks).toBe(1);
      expect(returned).toBe(false);
    } finally {
      release.resolve();
      await read;
    }
    expect((await read)?.workingMemory).toBe("retained notes");
    expect([first.state.value, callback.state.value, producer.state.value]).toEqual([
      "protected",
      "protected",
      "protected",
    ]);
    await expect(storage.saveMessages({ messages: [message("derived")] })).rejects.toBeInstanceOf(
      AutomaticCaptureSuppressed,
    );
    expect(h.calls.map((call) => call.op)).toEqual(["memory.get"]);
  });

  it("withholds marked native history if one contributing family cannot commit protection", async () => {
    const failed = captureFamily(async () => {
      throw new Error("Durable state unavailable");
    });
    const bound = captureFamily();
    const h = harness({ root: bound.root });
    await h.storage().updateResource({ resourceId: "alice", workingMemory: "retained notes" });
    const storage = captureStorage(h.agent, new SessionCapture([failed.root]));
    h.controls.mirrored = (op) => op === "memory.get";
    h.calls.length = 0;
    await expect(storage.getResourceById({ resourceId: "alice" })).rejects.toBeInstanceOf(SessionProtectionUnavailable);
    expect(failed.state.marks).toBe(1);
    expect(bound.state.value).toBe("protected");
    await expect(storage.saveThread({ thread: thread("derived") })).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(h.calls.map((call) => call.op)).toEqual(["memory.get"]);
  });

  it("does not acknowledge a marked automatic thread write as a completed callback", async () => {
    const family = captureFamily();
    const h = harness({ root: family.root });
    const storage = captureStorage(h.agent, new SessionCapture([family.root]));
    h.controls.mirrored = (op) => op === "memory.set";
    await expect(storage.saveThread({ thread: thread("t1") })).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(h.calls.map((call) => call.op)).toEqual(["memory.set"]);
    expect([...h.rows.values()].map((row) => row.value.data.id)).toEqual(["t1"]);
  });

  it("stops a marked message batch before later messages and the thread timestamp rewrite", async () => {
    const family = captureFamily();
    const h = harness({ root: family.root });
    await h.storage().saveThread({ thread: thread("t1") });
    const storage = captureStorage(h.agent, new SessionCapture([family.root]));
    h.controls.mirrored = (op) => op === "memory.set";
    h.calls.length = 0;
    await expect(storage.saveMessages({ messages: [message("first"), message("unsent")] })).rejects.toBeInstanceOf(
      AutomaticCaptureSuppressed,
    );
    expect(h.calls.map((call) => call.op)).toEqual(["memory.set"]);
    expect([...h.rows.values()].map((row) => row.value.data.id)).toEqual(["t1", "first"]);
    expect([...h.rows.values()][0].value.data.updatedAt).toBe(date(0).toISOString());
  });

  it("stops automatic hydration after a marked native listing page", async () => {
    const family = captureFamily();
    const h = harness({ root: family.root });
    await h.storage().saveThread({ thread: thread("t1") });
    const storage = captureStorage(h.agent, new SessionCapture([family.root]));
    h.controls.mirrored = (op) => op === "memory.list";
    h.calls.length = 0;
    await expect(storage.listThreads({})).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(h.calls.map((call) => call.op)).toEqual(["memory.list"]);
  });

  it("rechecks retained protection before retrying a conditional update conflict", async () => {
    const family = captureFamily();
    const h = harness({ root: family.root });
    await h.storage().saveThread({ thread: thread("t1") });
    const storage = captureStorage(h.agent, new SessionCapture([family.root]));
    h.controls.beforeSet = (row) => {
      row.value.data.title = "Concurrent title";
      family.state.value = "protected";
    };
    h.calls.length = 0;
    await expect(storage.updateThread({ id: "t1", metadata: { private: true } })).rejects.toBeInstanceOf(
      AutomaticCaptureSuppressed,
    );
    expect(h.calls.map((call) => call.op)).toEqual(["memory.get", "memory.set"]);
    expect([...h.rows.values()][0].value.data).toMatchObject({
      title: "Concurrent title",
      metadata: { group: "work" },
    });
  });

  it("does not acknowledge a message when its pending thread touch becomes protected", async () => {
    const family = captureFamily();
    const h = harness({ root: family.root });
    await h.storage().saveThread({ thread: thread("t1") });
    const storage = captureStorage(h.agent, new SessionCapture([family.root]));
    h.controls.mirrored = (op) => op === "memory.get";
    h.calls.length = 0;
    await expect(storage.saveMessages({ messages: [message("first")] })).rejects.toBeInstanceOf(
      AutomaticCaptureSuppressed,
    );
    expect(h.calls.map((call) => call.op)).toEqual(["memory.set", "memory.get"]);
    expect([...h.rows.values()].map((row) => row.value.data.id)).toEqual(["t1", "first"]);
    expect([...h.rows.values()][0].value.data.updatedAt).toBe(date(0).toISOString());
  });

  it("keeps explicitly owned native CRUD available under a protected bound family", async () => {
    const family = captureFamily();
    const h = harness({ root: family.root });
    const storage = h.storage();
    family.state.value = "protected";
    await storage.saveThread({ thread: thread("t1") });
    await storage.saveMessages({ messages: [message("m1")] });
    expect((await storage.getThreadById({ threadId: "t1" }))?.title).toBe("Thread t1");
    expect((await storage.listMessagesById({ messageIds: ["m1"] })).messages.map((row) => row.id)).toEqual(["m1"]);
    await storage.deleteThread({ threadId: "t1" });
    expect(h.rows.size).toBe(0);
  });
});
