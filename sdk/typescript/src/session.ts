/**
 * `Session` — the unit an agent framework holds.
 *
 * An agent's persistence is a session: resume it after a restart, append
 * turns as they happen, park a large tool result and keep a pointer in the
 * transcript, drop notes the next turn must see. Loom has an op for each;
 * `Session` binds them to one (user, session) scope so an agent loop calls
 * four verbs:
 *
 *     const s = bot.session("thread-42", { userId: "u1" });
 *     const { system, messages } = await s.resume({ query: "resume the billing migration" });
 *     await s.append({ role: "user", content: "..." }, { role: "assistant", content: "..." });
 *     const blob = await s.offload(buildLog, { summary: "pytest run 812" });   // keep blob.pointer
 *     await s.close();                                                          // flush → extraction
 *
 * Appended turns go to two places: the conversation transcript
 * (`conversation.append`, replayed by `history()` / `resume()`) and, in
 * batches of `flushEvery`, to `memory.set_from_messages`, where the server
 * extracts the facts worth keeping. `resume()` is `context.pack` for this
 * scope plus the transcript tail — the system prompt and the recent turns.
 */

import type { Agent, Body, Scope } from "./agent.js";
import type { OpOutput } from "./client.js";
import { NotFound } from "./errors.js";
import type {
  ContextFetchOutput,
  ContextOffloadOutput,
  ContextPackInput,
  ContextPackOutput,
  ConversationEntry,
  MemorySetFromMessagesOutput,
  Message,
  OpName,
} from "./generated/ops.js";
import {
  AutomaticCaptureSuppressed,
  boundProtection,
  requireAutomaticAllowed,
  requireAutomaticLocallyAllowed,
  responseRoots,
  SessionCapture,
  type SessionProtection,
  SessionProtectionUnavailable,
} from "./session-protection.js";
import { randomId } from "./transport.js";

export interface SessionOptions {
  /** Immutable contributing conversation handles retained for this session. */
  capture?: SessionCapture;
  userId?: string;
  runId?: string;
  /** Turns buffered before `memory.set_from_messages` runs. Default 8; 0 = only on `flush()` / `close()`. */
  flushEvery?: number;
  /** Also write every turn to the conversation transcript (`conversation.append`). Default true. */
  transcript?: boolean;
  /** Turns from the previous flush handed to the extractor as `last_messages`. Default 4. */
  carryOver?: number;
  /** Extra extraction fields for every flush (`extra_tags`, `extraction_instructions`, …). */
  extraction?: Omit<Body<"memory.set_from_messages">, "messages" | "last_messages">;
}

export interface ResumeOptions {
  /** The task at hand; selects the memories and procedures the pack carries. */
  query?: string;
  mode?: ContextPackInput["mode"];
  tokenBudget?: number;
  /** Transcript turns to return with the pack. Default 20; 0 skips the transcript read. */
  history?: number;
  pack?: Omit<Body<"context.pack">, "query" | "mode" | "token_budget">;
}

export interface Resumed {
  /** The rendered context block: what goes into the system prompt. */
  system: string;
  pack: ContextPackOutput;
  /** The most recent transcript turns, oldest first. */
  messages: ConversationEntry[];
}

export interface OffloadOptions {
  summary?: string;
  /** Empty = content-addressed (`blob/<sha256 prefix>`), so parking the same output twice stores one row. */
  blobKey?: string;
  ttlSeconds?: number;
  contentType?: string;
}

export interface FetchOptions {
  offset?: number;
  limit?: number;
  /** A regex: return only matching lines (with `contextLines` around each). */
  pattern?: string;
  contextLines?: number;
}

export type Turn = Message & { role: "user" | "assistant" | "system" | "tool" };

export class Session {
  readonly flushEvery: number;
  readonly transcript: boolean;
  readonly carryOver: number;
  private pending: Turn[] = [];
  private lastFlushed: Turn[] = [];
  private threadCreated = false;
  private producers: readonly SessionProtection[];
  /**
   * Whether the transcript is being stored: undefined until the first turn
   * is appended, false when the namespace has conversation logging off (an
   * admin turns it on with `conversation.set_logging`); extraction and
   * `resume()` keep working either way.
   */
  transcriptStored: boolean | undefined = undefined;
  private readonly extraction: SessionOptions["extraction"];

  constructor(
    readonly agent: Agent,
    readonly sessionId: string,
    private readonly opts: SessionOptions = {},
  ) {
    this.producers = responseRoots(agent.client.transport.sessionProtection, opts.capture);
    this.flushEvery = Math.max(0, opts.flushEvery ?? 8);
    this.transcript = opts.transcript ?? true;
    this.carryOver = Math.max(0, opts.carryOver ?? 4);
    this.extraction = opts.extraction;
  }

  /** The per-call scope every op in this session carries. */
  scope(): Scope {
    return { sessionId: this.sessionId, userId: this.opts.userId, runId: this.opts.runId };
  }

  private rememberProducers(): void {
    this.producers = Object.freeze([
      ...new Set([...this.producers, ...responseRoots(this.agent.client.transport.sessionProtection)]),
    ]);
  }

  private discardProtected(): boolean {
    this.rememberProducers();
    if (!this.producers.some((root) => root.locallyProtected)) return false;
    this.pending = [];
    this.lastFlushed = [];
    return true;
  }

  private async requireAllowed(): Promise<void> {
    this.rememberProducers();
    try {
      await boundProtection(
        requireAutomaticAllowed(this.producers, true),
        this.producers,
        this.agent.client.transport.timeoutMs,
      );
      requireAutomaticLocallyAllowed(this.producers, true);
    } finally {
      this.discardProtected();
    }
  }

  private async call<K extends OpName>(name: K, body: Body<K>, automatic = false, write = false): Promise<OpOutput<K>> {
    this.rememberProducers();
    const control = this.producers.length ? new SessionCapture(this.producers) : undefined;
    try {
      const out = await this.agent.call(
        name,
        body,
        this.scope(),
        control ? (automatic ? { capture: control } : { observe: control }) : undefined,
      );
      if (write) await this.requireAllowed();
      return out;
    } finally {
      this.discardProtected();
    }
  }

  /** Buffered turns; known local protection discards them before returning a copy. */
  get buffered(): readonly Turn[] {
    this.discardProtected();
    return [...this.pending];
  }

  /**
   * What a restarted process needs: the context pack for this scope (the
   * system prompt) and the recent transcript. A session that never wrote a
   * transcript resumes with `messages: []`.
   */
  async resume(opts: ResumeOptions = {}): Promise<Resumed> {
    const body = {
      ...(opts.pack ?? {}),
      query: opts.query ?? "",
      mode: opts.mode ?? "startup",
      token_budget: opts.tokenBudget ?? 2000,
    } as Body<"context.pack">;
    const wantHistory = (opts.history ?? 20) > 0 && this.transcript;
    const pack = await this.call("context.pack", body, true);
    let messages: ConversationEntry[] = [];
    if (wantHistory && !this.discardProtected()) {
      try {
        messages = await this.history(opts.history ?? 20);
      } catch (error) {
        if (!(error instanceof AutomaticCaptureSuppressed)) throw error;
      }
    }
    return { system: pack.rendered, pack, messages };
  }

  /** The most recent transcript turns, oldest first; `[]` when the thread does not exist yet. */
  async history(limit = 20): Promise<ConversationEntry[]> {
    try {
      const got = await this.call(
        "conversation.get",
        { thread_id: this.sessionId, include_messages: true, limit } as Body<"conversation.get">,
        true,
      );
      return got.found ? (got.messages ?? []) : [];
    } catch (e) {
      if (e instanceof NotFound) return [];
      throw e;
    }
  }

  /**
   * Record turns: each goes to the transcript now; extraction runs once
   * `flushEvery` turns are buffered. Returns the extraction result when a
   * flush happened, else undefined.
   */
  async append(...turns: Turn[]): Promise<MemorySetFromMessagesOutput | undefined> {
    if (turns.length === 0) return undefined;
    try {
      await this.requireAllowed();
      if (this.transcript && this.transcriptStored !== false) {
        await this.ensureThread();
        for (const t of turns) {
          const res = await this.call(
            "conversation.append",
            {
              thread_id: this.sessionId,
              msg_id: randomId(),
              role: t.role,
              content: t.content,
              ...(t.name ? { metadata: { name: t.name } } : {}),
            } as Body<"conversation.append">,
            true,
            true,
          );
          if (res.stored === false && res.reason === "logging_disabled") {
            this.transcriptStored = false;
            this.agent.client.transport.log.warn(
              `session ${this.sessionId}: conversation logging is off for namespace ${this.agent.namespace}; turns are not stored in the transcript (an admin enables it with conversation.set_logging). Extraction still runs.`,
            );
            break;
          }
          this.transcriptStored = true;
        }
      }
      await this.requireAllowed();
      requireAutomaticLocallyAllowed(this.producers, true);
      this.pending.push(...turns);
      if (this.flushEvery > 0 && this.pending.length >= this.flushEvery) return await this.flush();
      return undefined;
    } finally {
      this.discardProtected();
    }
  }

  /** Send the buffered turns to `memory.set_from_messages` now; a no-op with nothing buffered. */
  async flush(): Promise<MemorySetFromMessagesOutput | undefined> {
    if (this.pending.length === 0) {
      this.discardProtected();
      return undefined;
    }
    await this.requireAllowed();
    if (this.pending.length === 0) return undefined;
    const batch = this.pending;
    this.pending = [];
    const body = {
      ...(this.extraction ?? {}),
      messages: batch,
      ...(this.lastFlushed.length && this.carryOver ? { last_messages: this.lastFlushed.slice(-this.carryOver) } : {}),
    } as Body<"memory.set_from_messages">;
    try {
      const out = await this.call("memory.set_from_messages", body, true, true);
      requireAutomaticLocallyAllowed(this.producers, true);
      this.lastFlushed = batch;
      return out;
    } catch (error) {
      if (error instanceof AutomaticCaptureSuppressed || error instanceof SessionProtectionUnavailable) {
        this.pending = [];
        this.lastFlushed = [];
      } else if (!this.discardProtected()) {
        try {
          // Requeue only after a fresh durable check, preserving the request's primary error.
          await this.requireAllowed();
          requireAutomaticLocallyAllowed(this.producers, true);
          this.pending = [...batch, ...this.pending];
        } catch {
          this.pending = [];
          this.lastFlushed = [];
        }
      }
      throw error;
    } finally {
      this.discardProtected();
    }
  }

  /** Park a large tool result once; keep `.pointer` in the transcript instead of the content. */
  offload(content: string, opts: OffloadOptions = {}): Promise<ContextOffloadOutput> {
    return this.call("context.offload", {
      content,
      ...(opts.summary !== undefined ? { summary: opts.summary } : {}),
      ...(opts.blobKey !== undefined ? { blob_key: opts.blobKey } : {}),
      ...(opts.ttlSeconds !== undefined ? { ttl_seconds: opts.ttlSeconds } : {}),
      ...(opts.contentType !== undefined ? { content_type: opts.contentType } : {}),
    } as Body<"context.offload">);
  }

  /** Read a parked result back: a character slice, or the lines matching `pattern`. */
  fetch(blobKey: string, opts: FetchOptions = {}): Promise<ContextFetchOutput> {
    return this.call("context.fetch", {
      blob_key: blobKey,
      ...(opts.offset !== undefined ? { offset: opts.offset } : {}),
      ...(opts.limit !== undefined ? { limit: opts.limit } : {}),
      ...(opts.pattern !== undefined ? { pattern: opts.pattern } : {}),
      ...(opts.contextLines !== undefined ? { context_lines: opts.contextLines } : {}),
    } as Body<"context.fetch">);
  }

  /** Append to the always-resident working-memory block the next turn's pack carries. */
  note(
    addition: string,
    opts: { blockKey?: string; separator?: string } = {},
  ): Promise<OpOutput<"working_memory.append">> {
    return this.call("working_memory.append", {
      addition,
      ...(opts.blockKey !== undefined ? { block_key: opts.blockKey } : {}),
      ...(opts.separator !== undefined ? { separator: opts.separator } : {}),
    } as Body<"working_memory.append">);
  }

  /** Flush what is buffered. Call it when the conversation ends or the process is about to. */
  async close(): Promise<MemorySetFromMessagesOutput | undefined> {
    return this.flush();
  }

  private async ensureThread(): Promise<void> {
    if (this.threadCreated) return;
    // conversation.create is idempotent on thread_id; a second process resuming the thread is fine.
    await this.call("conversation.create", { thread_id: this.sessionId } as Body<"conversation.create">, true, true);
    this.threadCreated = true;
  }
}
