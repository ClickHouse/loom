import { ToolError } from "@anthropic-ai/sdk/lib/tools/ToolError";
import { describe, expect, it } from "vitest";
import { Agent } from "../agent.js";
import { loomMemoryHandlers, loomMemoryTool } from "./claude-memory.js";

/** A loom server holding documents in a map: knowledge.get/upsert/list/bulk_delete, listed two rows a page. */
function harness() {
  const docs = new Map<string, { content: string; tags: string[] }>();
  const ops: string[] = [];
  const fetch = async (url: string, init: RequestInit) => {
    const op = url.split("/v1/")[1];
    const body = JSON.parse(init.body as string);
    ops.push(op);
    let reply: unknown = {};
    if (op === "knowledge.get") {
      const doc = docs.get(body.source_uri);
      reply = doc ? { found: true, content: doc.content, source_uri: body.source_uri } : { found: false };
    } else if (op === "knowledge.upsert") {
      docs.set(body.source_uri, { content: body.content, tags: body.tags });
    } else if (op === "knowledge.list") {
      const rows = [...docs.entries()]
        .filter(([, d]) => d.tags.some((t) => body.tags.includes(t)))
        .filter(([u]) => body.uri_prefix.some((p: string) => u.startsWith(p)))
        .map(([u]) => u)
        .sort();
      const start = Number(body.cursor ?? 0);
      reply = {
        rows: rows.slice(start, start + 2).map((source_uri) => ({ source_uri })),
        next_cursor: start + 2 < rows.length ? String(start + 2) : null,
      };
    } else if (op === "knowledge.bulk_delete") {
      for (const uri of body.source_uris) docs.delete(uri);
    }
    return new Response(JSON.stringify(reply), { status: 200 });
  };
  const agent = new Agent("o", "n", "bot", { baseUrl: "http://x", fetch, logLevel: "off" });
  return { agent, docs, ops };
}

const text = (docs: Map<string, { content: string }>, path: string, store = "default") =>
  docs.get(`loom-memory://${store}${path}`)?.content;

describe("loomMemoryTool (Claude memory tool over loom documents)", () => {
  it("is the SDK's memory tool", () => {
    const tool = loomMemoryTool(harness().agent);
    expect([tool.type, tool.name]).toEqual(["memory_20250818", "memory"]);
  });

  it("creates a file and views it with line numbers and a range", async () => {
    const { agent, docs } = harness();
    const h = loomMemoryHandlers(agent);
    expect(await h.create({ command: "create", path: "/memories/notes.md", file_text: "a\nb\nc" })).toBe(
      "File created successfully at: /memories/notes.md",
    );
    expect(docs.get("loom-memory://default/memories/notes.md")).toEqual({
      content: "a\nb\nc",
      tags: ["claude-memory:default"],
    });
    expect(await h.view({ command: "view", path: "/memories/notes.md" })).toBe(
      "Here's the content of /memories/notes.md with line numbers:\n     1\ta\n     2\tb\n     3\tc",
    );
    expect(await h.view({ command: "view", path: "/memories/notes.md", view_range: [2, -1] })).toMatch(
      / {5}2\tb\n {5}3\tc$/,
    );
    await expect(h.create({ command: "create", path: "/memories/notes.md", file_text: "x" })).rejects.toThrow(
      "already exists",
    );
  });

  it("views a directory two levels deep across pages", async () => {
    const { agent } = harness();
    const h = loomMemoryHandlers(agent);
    for (const path of ["/memories/a.md", "/memories/p/b.md", "/memories/p/q/c.md", "/memories/.hidden"]) {
      await h.create({ command: "create", path, file_text: "x" });
    }
    expect(await h.view({ command: "view", path: "/memories" })).toBe(
      "Here're the files and directories up to 2 levels deep in /memories, excluding hidden items:\n" +
        "/memories\n/memories/a.md\n/memories/p/\n/memories/p/b.md\n/memories/p/q/",
    );
    await expect(h.view({ command: "view", path: "/memories/none" })).rejects.toThrow("does not exist");
  });

  it("replaces one occurrence and refuses none or many", async () => {
    const { agent, docs } = harness();
    const h = loomMemoryHandlers(agent);
    await h.create({ command: "create", path: "/memories/n.md", file_text: "one\ntwo\nthree\ntwo" });
    await expect(
      h.str_replace({ command: "str_replace", path: "/memories/n.md", old_str: "four", new_str: "4" }),
    ).rejects.toThrow("did not appear verbatim");
    await expect(
      h.str_replace({ command: "str_replace", path: "/memories/n.md", old_str: "two", new_str: "2" }),
    ).rejects.toThrow("Multiple occurrences of old_str `two` in lines: 2, 4");
    const answer = await h.str_replace({
      command: "str_replace",
      path: "/memories/n.md",
      old_str: "three",
      new_str: "$&3",
    });
    expect(answer).toMatch(/^The memory file has been edited\./);
    expect(text(docs, "/memories/n.md")).toBe("one\ntwo\n$&3\ntwo"); // the new text is literal
  });

  it("inserts a line and checks its range", async () => {
    const { agent, docs } = harness();
    const h = loomMemoryHandlers(agent);
    await h.create({ command: "create", path: "/memories/n.md", file_text: "a\nc\n" });
    expect(await h.insert({ command: "insert", path: "/memories/n.md", insert_line: 1, insert_text: "b\n" })).toBe(
      "The file /memories/n.md has been edited.",
    );
    expect(text(docs, "/memories/n.md")).toBe("a\nb\nc\n");
    await expect(
      h.insert({ command: "insert", path: "/memories/n.md", insert_line: 9, insert_text: "z" }),
    ).rejects.toThrow("within the range [0, 3]");
  });

  it("deletes and renames a file or a directory", async () => {
    const { agent, docs } = harness();
    const h = loomMemoryHandlers(agent);
    for (const path of ["/memories/a.md", "/memories/p/b.md", "/memories/p/q/c.md", "/memories/pq.md"]) {
      await h.create({ command: "create", path, file_text: path });
    }
    await h.rename({ command: "rename", old_path: "/memories/p", new_path: "/memories/r" });
    expect(text(docs, "/memories/r/q/c.md")).toBe("/memories/p/q/c.md");
    expect(await h.delete({ command: "delete", path: "/memories/r" })).toBe("Successfully deleted /memories/r");
    expect([...docs.keys()].sort()).toEqual([
      "loom-memory://default/memories/a.md",
      "loom-memory://default/memories/pq.md",
    ]);
    await expect(
      h.rename({ command: "rename", old_path: "/memories/a.md", new_path: "/memories/pq.md" }),
    ).rejects.toThrow("already exists");
    await expect(h.delete({ command: "delete", path: "/memories" })).rejects.toThrow("itself");
  });

  it("keeps every path under /memories and a store to its own files", async () => {
    const { agent, docs, ops } = harness();
    const h = loomMemoryHandlers(agent);
    for (const path of ["/etc/passwd", "/memoriesx/a", "/memories/../secret"]) {
      const err = await Promise.resolve(h.create({ command: "create", path, file_text: "x" })).catch((e: unknown) => e);
      expect(err).toBeInstanceOf(ToolError);
    }
    expect(ops).toEqual([]);
    await h.create({ command: "create", path: "/memories/a/../b.md", file_text: "x" });
    expect([...docs.keys()]).toEqual(["loom-memory://default/memories/b.md"]);
    const alice = loomMemoryHandlers(agent, { store: "alice" });
    await expect(alice.view({ command: "view", path: "/memories/b.md" })).rejects.toThrow("does not exist");
    expect(() => loomMemoryHandlers(agent, { store: "a/b" })).toThrow("store");
    // Back under the root once resolved, as POSIX normpath reads it (the Python tool agrees).
    await h.create({ command: "create", path: "/memories/a/../../memories/c.md", file_text: "x" });
    expect(docs.has("loom-memory://default/memories/c.md")).toBe(true);
  });

  it("refuses an empty old_str, hides nested hidden files and refuses create on a directory", async () => {
    const { agent } = harness();
    const h = loomMemoryHandlers(agent);
    await h.create({ command: "create", path: "/memories/n.md", file_text: "abc" });
    await expect(
      h.str_replace({ command: "str_replace", path: "/memories/n.md", old_str: "", new_str: "x" }),
    ).rejects.toThrow("old_str is empty");
    for (const path of ["/memories/p/.secret", "/memories/.d/a.md", "/memories/p/b.md"]) {
      await h.create({ command: "create", path, file_text: "x" });
    }
    expect(await h.view({ command: "view", path: "/memories/p" })).toMatch(/\/memories\/p\n\/memories\/p\/b\.md$/);
    await expect(h.create({ command: "create", path: "/memories/p", file_text: "x" })).rejects.toThrow(
      "already exists",
    );
  });

  it("deletes a large directory in batches the server takes", async () => {
    const { agent, docs, ops } = harness();
    for (let i = 0; i < 501; i++) {
      docs.set(`loom-memory://default/memories/big/${String(i).padStart(4, "0")}.md`, {
        content: "x",
        tags: ["claude-memory:default"],
      });
    }
    await loomMemoryHandlers(agent).delete({ command: "delete", path: "/memories/big" });
    expect(ops.filter((op) => op === "knowledge.bulk_delete")).toHaveLength(2);
    expect(docs.size).toBe(0);
  });

  it("refuses a path under a file and answers that a directory is not a file", async () => {
    const { agent } = harness();
    const h = loomMemoryHandlers(agent);
    await h.create({ command: "create", path: "/memories/a", file_text: "x" });
    await expect(h.create({ command: "create", path: "/memories/a/b", file_text: "x" })).rejects.toThrow(
      "/memories/a is a file",
    );
    await h.create({ command: "create", path: "/memories/p/b.md", file_text: "x" });
    await expect(
      h.str_replace({ command: "str_replace", path: "/memories/p", old_str: "x", new_str: "y" }),
    ).rejects.toThrow("The path /memories/p is not a file");
    await expect(
      h.rename({ command: "rename", old_path: "/memories/p/b.md", new_path: "/memories/a/b.md" }),
    ).rejects.toThrow("/memories/a is a file");
  });

  it("lists only the path's own directory, its URIs only", async () => {
    const calls: Array<Record<string, unknown>> = [];
    const fetch = async (url: string, init: RequestInit) => {
      const body = JSON.parse(init.body as string);
      if (url.endsWith("knowledge.list")) calls.push(body);
      return new Response(JSON.stringify(url.endsWith("knowledge.list") ? { rows: [] } : { found: false }), {
        status: 200,
      });
    };
    const agent = new Agent("o", "n", "bot", { baseUrl: "http://x", fetch, logLevel: "off" });
    await loomMemoryHandlers(agent, { store: "alice" }).create({
      command: "create",
      path: "/memories/a.md",
      file_text: "x",
    });
    expect(calls).toEqual([
      expect.objectContaining({
        uri_prefix: ["loom-memory://alice/memories/a.md/"],
        fields: ["source_uri"],
        limit: 1,
        tags: ["claude-memory:alice"],
      }),
    ]);
  });

  it("sorts a listing by code point, as the Python tool does", async () => {
    const { agent } = harness();
    const h = loomMemoryHandlers(agent);
    for (const path of ["/memories/\uFF21.md", "/memories/\u{1F600}.md"]) {
      await h.create({ command: "create", path, file_text: "x" });
    }
    expect(await h.view({ command: "view", path: "/memories" })).toMatch(
      /\/memories\/\uFF21\.md\n\/memories\/\u{1F600}\.md$/u,
    );
  });

  it("passes the tool's extra properties through", () => {
    const tool = loomMemoryTool(harness().agent, {}, { cache_control: { type: "ephemeral" } });
    expect(tool).toMatchObject({ type: "memory_20250818", cache_control: { type: "ephemeral" } });
  });
});
