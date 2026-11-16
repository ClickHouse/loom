/**
 * loom as the storage of the Claude API memory tool (TypeScript twin of
 * `loom.integrations.claude_memory`).
 *
 * The memory tool (`memory_20250818`) lets Claude keep files under `/memories`
 * across conversations; the application runs its commands. `loomMemoryTool`
 * runs them against loom: each file is a loom document, so every agent of the
 * namespace reads the same files and `knowledge.search` finds what they say.
 *
 *     const memory = loomMemoryTool(agent, { store: "alice" });
 *     const runner = client.beta.messages.toolRunner({
 *       model: "claude-opus-5-5", max_tokens: 16000, tools: [memory], messages,
 *     });
 *
 * `@anthropic-ai/sdk` is an optional peer dependency.
 */

import { betaMemoryTool, type MemoryToolHandlers } from "@anthropic-ai/sdk/helpers/beta/memory";
import { ToolError } from "@anthropic-ai/sdk/lib/tools/ToolError";
import type { Agent } from "../agent.js";

const ROOT = "/memories";
// The SDK's local memory tool numbers lines to 999,999.
const LINE_WIDTH = 6;
const PAGE = 200;

export interface LoomMemoryToolOptions {
  /** The file tree: one per application, or per user. One path segment. Default `default`. */
  store?: string;
}

class Store {
  readonly tag: string;
  private readonly prefix: string;

  constructor(store: string) {
    if (!store || store.includes("/") || store !== store.trim()) {
      throw new Error(`store is one non-empty path segment, got ${JSON.stringify(store)}`);
    }
    this.tag = `claude-memory:${store}`;
    this.prefix = `loom-memory://${store}`;
  }

  uri(path: string): string {
    return this.prefix + path;
  }

  path(uri: string): string | null {
    return uri.startsWith(this.prefix + ROOT) ? uri.slice(this.prefix.length) : null;
  }
}

/** `path` normalised under /memories; a ToolError when it is not under it. */
function checked(path: string): string {
  if (path !== ROOT && !path.startsWith(`${ROOT}/`)) {
    throw new ToolError(`Path must start with /memories, got: ${path}`);
  }
  const parts: string[] = [];
  for (const part of path.split("/")) {
    if (part === "" || part === ".") continue;
    if (part === "..") {
      if (parts.length <= 1) throw new ToolError(`Path ${path} would escape /memories directory`);
      parts.pop();
    } else {
      parts.push(part);
    }
  }
  return `/${parts.join("/")}`;
}

function under(paths: string[], directory: string): string[] {
  const prefix = `${directory.replace(/\/+$/, "")}/`;
  return paths.filter((p) => p.startsWith(prefix)).sort();
}

/** A directory view, two levels deep, as the SDK's local memory tool lists one (no sizes). */
function listing(shown: string, directory: string, paths: string[]): string {
  const base = directory.replace(/\/+$/, "");
  const entries = new Set<string>();
  for (const path of under(paths, directory)) {
    const parts = path.slice(base.length + 1).split("/");
    for (let depth = 1; depth <= Math.min(parts.length, 2); depth++) {
      entries.add(parts.slice(0, depth).join("/") + (depth < parts.length ? "/" : ""));
    }
  }
  const head = `Here're the files and directories up to 2 levels deep in ${shown}, excluding hidden items:`;
  const lines = [
    shown,
    ...[...entries]
      .sort()
      .filter((e) => !e.startsWith("."))
      .map((e) => `${shown.replace(/\/+$/, "")}/${e}`),
  ];
  return `${head}\n${lines.join("\n")}`;
}

function numbered(path: string, text: string, range?: number[] | null): string {
  const lines = text.split("\n");
  let start = 1;
  let shown = lines;
  if (range && range.length === 2) {
    start = Math.max(1, range[0] ?? 1);
    const end = range[1] === -1 ? lines.length : (range[1] ?? lines.length);
    shown = lines.slice(start - 1, end);
  }
  const body = shown.map((line, i) => `${String(i + start).padStart(LINE_WIDTH)}\t${line}`).join("\n");
  return `Here's the content of ${path} with line numbers:\n${body}`;
}

function replaced(path: string, text: string, old: string, next: string): [string, string] {
  const count = text.split(old).length - 1;
  if (count === 0) {
    throw new ToolError(`No replacement was performed, old_str \`${old}\` did not appear verbatim in ${path}.`);
  }
  if (count > 1) {
    const lines: number[] = [];
    let at = text.indexOf(old);
    while (at !== -1) {
      lines.push(text.slice(0, at).split("\n").length);
      at = text.indexOf(old, at + 1);
    }
    throw new ToolError(
      `No replacement was performed. Multiple occurrences of old_str \`${old}\` in lines: ${lines.join(", ")}. Please ensure it is unique`,
    );
  }
  const changed = text.slice(0, text.indexOf(old)).split("\n").length - 1;
  const updated = text.replace(old, () => next);
  const rows = updated.split("\n");
  const snippet: string[] = [];
  for (let n = Math.max(0, changed - 2) + 1; n <= Math.min(rows.length, changed + 3); n++) {
    snippet.push(`${String(n).padStart(LINE_WIDTH)}\t${rows[n - 1]}`);
  }
  return [
    updated,
    `The memory file has been edited. Here is the snippet showing the change (with line numbers):\n${snippet.join("\n")}`,
  ];
}

function inserted(text: string, line: number, insert: string): string {
  const lines = text.split("\n");
  if (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();
  if (line < 0 || line > lines.length) {
    throw new ToolError(
      `Invalid \`insert_line\` parameter: ${line}. It should be within the range [0, ${lines.length}].`,
    );
  }
  lines.splice(line, 0, insert.replace(/\n+$/, ""));
  return `${lines.join("\n")}\n`;
}

const missing = (path: string) => new ToolError(`The path ${path} does not exist. Please provide a valid path.`);

/** The memory tool's command handlers over a loom `Agent`'s documents. */
export function loomMemoryHandlers(agent: Agent, opts: LoomMemoryToolOptions = {}): MemoryToolHandlers {
  const store = new Store(opts.store ?? "default");

  const paths = async (): Promise<string[]> => {
    const out: string[] = [];
    let cursor: string | null | undefined;
    do {
      const page = await agent.call("knowledge.list", { tags: [store.tag], limit: PAGE, cursor: cursor ?? null });
      for (const row of page.rows) {
        const path = store.path(row.source_uri);
        if (path) out.push(path);
      }
      cursor = page.next_cursor;
    } while (cursor);
    return out;
  };
  const text = async (path: string): Promise<string | null> => {
    const doc = await agent.call("knowledge.get", { source_uri: store.uri(path) });
    return doc.found ? (doc.content ?? "") : null;
  };
  const write = async (path: string, content: string): Promise<void> => {
    await agent.call("knowledge.upsert", { content, source_uri: store.uri(path), tags: [store.tag] });
  };
  const remove = async (list: string[]): Promise<void> => {
    await agent.call("knowledge.bulk_delete", { source_uris: list.map((p) => store.uri(p)) });
  };

  return {
    async view(command) {
      const path = checked(command.path);
      const body = path === ROOT ? null : await text(path);
      if (body !== null) return numbered(command.path, body, command.view_range);
      const all = await paths();
      if (path !== ROOT && under(all, path).length === 0) throw missing(command.path);
      return listing(command.path, path, all);
    },
    async create(command) {
      const path = checked(command.path);
      if (path === ROOT || (await text(path)) !== null) throw new ToolError(`File ${command.path} already exists`);
      await write(path, command.file_text);
      return `File created successfully at: ${command.path}`;
    },
    async str_replace(command) {
      const path = checked(command.path);
      const body = await text(path);
      if (body === null) throw missing(command.path);
      const [updated, answer] = replaced(command.path, body, command.old_str, command.new_str);
      await write(path, updated);
      return answer;
    },
    async insert(command) {
      const path = checked(command.path);
      const body = await text(path);
      if (body === null) throw missing(command.path);
      await write(path, inserted(body, command.insert_line, command.insert_text));
      return `The file ${command.path} has been edited.`;
    },
    async delete(command) {
      const path = checked(command.path);
      if (path === ROOT) throw new ToolError("Cannot delete the /memories directory itself");
      const doomed = (await text(path)) !== null ? [path] : under(await paths(), path);
      if (doomed.length === 0) throw new ToolError(`The path ${command.path} does not exist`);
      await remove(doomed);
      return `Successfully deleted ${command.path}`;
    },
    async rename(command) {
      const old = checked(command.old_path);
      const next = checked(command.new_path);
      if (old === ROOT) throw new ToolError("Cannot rename the /memories directory itself");
      const all = await paths();
      if (next === ROOT || all.includes(next) || under(all, next).length > 0) {
        throw new ToolError(`The destination ${command.new_path} already exists`);
      }
      const moves: [string, string][] = all.includes(old)
        ? [[old, next]]
        : under(all, old).map((p) => [p, next + p.slice(old.length)]);
      if (moves.length === 0) throw new ToolError(`The path ${command.old_path} does not exist`);
      for (const [source, target] of moves) {
        const body = await text(source);
        if (body !== null) await write(target, body);
      }
      await remove(moves.map(([source]) => source));
      return `Successfully renamed ${command.old_path} to ${command.new_path}`;
    },
  };
}

/** The Claude API memory tool over a loom `Agent`'s documents, for the SDK's tool runner. */
export function loomMemoryTool(agent: Agent, opts: LoomMemoryToolOptions = {}) {
  return betaMemoryTool(loomMemoryHandlers(agent, opts));
}
