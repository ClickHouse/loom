/**
 * Loom as the storage of the Claude API memory tool (TypeScript twin of
 * `loom.integrations.claude_memory`).
 *
 * The memory tool (`memory_20250818`) lets Claude keep files under `/memories`
 * across conversations; the application runs its commands. `loomMemoryTool`
 * runs them against Loom: each file is a Loom document, so every agent of the
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
const PAGE = 500; // knowledge.list's cap on rows a page
// knowledge.bulk_delete's cap on URIs a call.
const DELETE_BATCH = 500;

export interface LoomMemoryToolOptions {
  /** The file tree: one per application, or per user. One path segment. Default `default`. */
  store?: string;
}

/** `betaMemoryTool`'s extra tool properties, such as `cache_control`. */
export type LoomMemoryToolExtras = Parameters<typeof betaMemoryTool>[1];

class Store {
  readonly tag: string;
  private readonly prefix: string;

  constructor(store: string) {
    // Both SDKs refuse a store whose first or last code point is at or below the space.
    const edge = (ch: string | undefined) => ch === undefined || (ch.codePointAt(0) ?? 0) <= 32;
    if (!store || store.includes("/") || edge(store[0]) || edge(store[store.length - 1])) {
      throw new Error(`store is one non-empty path segment, got ${JSON.stringify(store)}`);
    }
    this.tag = `claude-memory:${store}`;
    this.prefix = `loom-memory://${store}`;
  }

  uri(path: string): string {
    return this.prefix + path;
  }

  /** The path of a URI this store's listing returned (it starts with the prefix). */
  path(uri: string): string {
    return uri.slice(this.prefix.length);
  }
}

/** `path` normalised under /memories; a ToolError when it is not under it. */
function checked(path: string): string {
  if (path !== ROOT && !path.startsWith(`${ROOT}/`)) {
    throw new ToolError(`Path must start with /memories, got: ${path}`);
  }
  // As POSIX normpath: resolve `.` and `..` over the whole path, then check the result.
  const parts: string[] = [];
  for (const part of path.split("/")) {
    if (part === "" || part === ".") continue;
    if (part === "..") parts.pop();
    else parts.push(part);
  }
  const normal = `/${parts.join("/")}`;
  if (normal !== ROOT && !normal.startsWith(`${ROOT}/`)) {
    throw new ToolError(`Path ${path} would escape /memories directory`);
  }
  return normal;
}

/** Strings in code-point order, as Python sorts them, so both SDKs list alike. */
function byCodePoint(a: string, b: string): number {
  const x = [...a];
  const y = [...b];
  for (let i = 0; i < Math.min(x.length, y.length); i++) {
    const d = (x[i].codePointAt(0) ?? 0) - (y[i].codePointAt(0) ?? 0);
    if (d !== 0) return d;
  }
  return x.length - y.length;
}

/** Paths compared segment by segment, each by code point, as Python compares lists. */
function bySegments(a: string[], b: string[]): number {
  for (let i = 0; i < Math.min(a.length, b.length); i++) {
    const d = byCodePoint(a[i], b[i]);
    if (d !== 0) return d;
  }
  return a.length - b.length;
}

/** `text` without the trailing runs of `ch` (a loop: a regex would be quadratic here). */
function trimEnd(text: string, ch: string): string {
  let end = text.length;
  while (end > 0 && text[end - 1] === ch) end--;
  return text.slice(0, end);
}

/** A directory view of `paths` (the files under `directory`), two levels deep, as the
 * Python SDK's local memory tool lists one (no sizes). */
function listing(shown: string, directory: string, paths: string[]): string {
  const entries = new Set<string>();
  for (const path of paths) {
    const parts = path.slice(directory.length + 1).split("/");
    for (let depth = 1; depth <= Math.min(parts.length, 2); depth++) {
      entries.add(parts.slice(0, depth).join("/") + (depth < parts.length ? "/" : ""));
    }
  }
  const head = `Here're the files and directories up to 2 levels deep in ${shown}, excluding hidden items:`;
  const base = trimEnd(shown, "/");
  // By path segment, as the reference walks each directory: `notes/` and its files before `notes.md`.
  const visible = [...entries]
    .sort((a, b) => bySegments(trimEnd(a, "/").split("/"), trimEnd(b, "/").split("/")))
    .filter((e) => !e.split("/").some((part) => part.startsWith(".")));
  return `${head}\n${[shown, ...visible.map((e) => `${base}/${e}`)].join("\n")}`;
}

/** The directories above `path`, below /memories. */
function ancestors(path: string): string[] {
  const parts = path
    .slice(ROOT.length + 1)
    .split("/")
    .slice(0, -1);
  return parts.map((_, n) => `${ROOT}/${parts.slice(0, n + 1).join("/")}`);
}

function numbered(path: string, text: string, range?: number[] | null): string {
  const lines = text.split("\n");
  if (lines.length > 999_999) {
    throw new ToolError(`File ${path} exceeds maximum line limit of 999,999 lines.`);
  }
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
  if (!old) throw new ToolError(`No replacement was performed, old_str is empty in ${path}.`);
  const count = text.split(old).length - 1;
  if (count === 0) {
    throw new ToolError(`No replacement was performed, old_str \`${old}\` did not appear verbatim in ${path}.`);
  }
  if (count > 1) {
    // One entry per match, overlapping ones too, as the reference tool answers.
    const lines: number[] = [];
    let line = 1;
    let seen = 0;
    for (let at = text.indexOf(old); at !== -1; at = text.indexOf(old, at + 1)) {
      for (let k = seen; k < at; k++) if (text[k] === "\n") line++; // one pass over the text
      seen = at;
      lines.push(line);
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
  if (lines[lines.length - 1] === "") lines.pop();
  if (line < 0 || line > lines.length) {
    throw new ToolError(
      `Invalid \`insert_line\` parameter: ${line}. It should be within the range [0, ${lines.length}].`,
    );
  }
  lines.splice(line, 0, trimEnd(insert, "\n"));
  const updated = lines.join("\n");
  return updated.endsWith("\n") ? updated : `${updated}\n`;
}

const missing = (path: string) => new ToolError(`The path ${path} does not exist. Please provide a valid path.`);

/** The memory tool's command handlers over a Loom `Agent`'s documents. */
export function loomMemoryHandlers(agent: Agent, opts: LoomMemoryToolOptions = {}): MemoryToolHandlers {
  const store = new Store(opts.store ?? "default");

  /** The files under `directory`, sorted; at most `limit` when given (any of them). */
  const under = async (directory: string, limit?: number): Promise<string[]> => {
    const out: string[] = [];
    let cursor: string | null | undefined;
    do {
      const page = await agent.call("knowledge.list", {
        tags: [store.tag],
        uri_prefix: [`${store.uri(directory)}/`],
        fields: ["source_uri"],
        limit: limit ?? PAGE,
        cursor: cursor ?? null,
      });
      for (const row of page.rows) out.push(store.path(row.source_uri));
      cursor = limit ? null : page.next_cursor;
    } while (cursor);
    return out.sort(byCodePoint);
  };
  const text = async (path: string): Promise<string | null> => {
    const doc = await agent.call("knowledge.get", { source_uri: store.uri(path) });
    return doc.found ? (doc.content ?? "") : null;
  };
  const write = async (path: string, content: string): Promise<void> => {
    await agent.call("knowledge.upsert", { content, source_uri: store.uri(path), tags: [store.tag] });
  };
  const remove = async (list: string[]): Promise<void> => {
    const uris = list.map((p) => store.uri(p));
    for (let start = 0; start < uris.length; start += DELETE_BATCH) {
      await agent.call("knowledge.bulk_delete", { source_uris: uris.slice(start, start + DELETE_BATCH) });
    }
  };

  /** The text of the file `path`; a ToolError when it is a directory or nothing. */
  const file = async (shown: string, path: string): Promise<string> => {
    if (path === ROOT) throw new ToolError(`The path ${shown} is not a file.`);
    const body = await text(path);
    if (body !== null) return body;
    if ((await under(path, 1)).length > 0) throw new ToolError(`The path ${shown} is not a file.`);
    throw missing(shown);
  };
  /** A ToolError when a directory above `path` is a file. */
  const noFileAbove = async (path: string): Promise<void> => {
    for (const above of ancestors(path)) {
      if ((await text(above)) !== null) throw new ToolError(`The path ${above} is a file, not a directory`);
    }
  };

  return {
    async view(command) {
      const path = checked(command.path);
      const body = path === ROOT ? null : await text(path);
      if (body !== null) return numbered(command.path, body, command.view_range);
      const files = await under(path);
      if (path !== ROOT && files.length === 0) throw missing(command.path);
      return listing(command.path, path, files);
    },
    async create(command) {
      const path = checked(command.path);
      if (path === ROOT || (await text(path)) !== null || (await under(path, 1)).length > 0) {
        throw new ToolError(`File ${command.path} already exists`);
      }
      await noFileAbove(path);
      await write(path, command.file_text);
      return `File created successfully at: ${command.path}`;
    },
    async str_replace(command) {
      const path = checked(command.path);
      const body = await file(command.path, path);
      const [updated, answer] = replaced(command.path, body, command.old_str, command.new_str);
      await write(path, updated);
      return answer;
    },
    async insert(command) {
      const path = checked(command.path);
      const body = await file(command.path, path);
      await write(path, inserted(body, command.insert_line, command.insert_text));
      return `The file ${command.path} has been edited.`;
    },
    async delete(command) {
      const path = checked(command.path);
      if (path === ROOT) throw new ToolError("Cannot delete the /memories directory itself");
      const doomed = (await text(path)) !== null ? [path] : await under(path);
      if (doomed.length === 0) throw new ToolError(`The path ${command.path} does not exist`);
      await remove(doomed);
      return `Successfully deleted ${command.path}`;
    },
    async rename(command) {
      const old = checked(command.old_path);
      const next = checked(command.new_path);
      if (old === ROOT) throw new ToolError("Cannot rename the /memories directory itself");
      if (next === ROOT || (await text(next)) !== null || (await under(next, 1)).length > 0) {
        throw new ToolError(`The destination ${command.new_path} already exists`);
      }
      await noFileAbove(next);
      const body = await text(old);
      if (body !== null) {
        await write(next, body);
        await remove([old]);
      } else {
        const sources = await under(old);
        if (sources.length === 0) throw new ToolError(`The path ${command.old_path} does not exist`);
        for (const source of sources) {
          const moved = await text(source);
          if (moved !== null) await write(next + source.slice(old.length), moved);
        }
        await remove(sources);
      }
      return `Successfully renamed ${command.old_path} to ${command.new_path}`;
    },
  };
}

/** The Claude API memory tool over a Loom `Agent`'s documents, for the SDK's tool runner. */
export function loomMemoryTool(agent: Agent, opts: LoomMemoryToolOptions = {}, extras?: LoomMemoryToolExtras) {
  return betaMemoryTool(loomMemoryHandlers(agent, opts), extras);
}
