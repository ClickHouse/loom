/**
 * The Loom assistant, asked as the client's token: `stream` yields an answer as it is
 * written (`loom.assistant.ask`, the op, returns one whole). It sends
 * `POST /v1/assistant/chat/completions`;
 * the server checks each citation the answer writes against Loom, as the caller, when
 * the answer ends. An answer that began is never resent: asking again is a new answer,
 * with its own spend.
 *
 *     for await (const event of loom.assistant.stream({ namespace, question })) {
 *       if (event.type === "text") process.stdout.write(event.text);
 *       else console.log(event.answer.citations.filter((c) => c.verified));
 *     }
 */

import {
  BackendUnavailable,
  classed,
  errorFromResponse,
  LoomError,
  NETWORK_ERROR_CODE,
  TimeoutError,
} from "./errors.js";
import type { Transport } from "./transport.js";

export const ASSISTANT_ROUTE = "/v1/assistant/chat/completions";
/** How long one read of an answer waits by default: the answer is silent while one tool
 * call runs, and Node's fetch cuts a body read that waits longer than this anyway. */
export const DEFAULT_READ_TIMEOUT_MS = 300_000;

/** One citation the answer wrote, whether Loom verified it, and why not. */
export interface AssistantCitation {
  kind: "memory" | "doc" | "help" | "code" | "sql" | string;
  text: string;
  verified: boolean;
  reason: string;
  /** The memory key, document source, documentation page or code path it names ('' for sql). */
  ref: string;
  /** A verified help citation's section anchor on its page; '' otherwise. */
  anchor: string;
}

/** How far an answer rests on what Loom holds: `supported`, `unsupported`, `unchecked` or
 *  `none`, as docs/architecture/assistant.md#support defines them; a value this SDK does
 *  not know, or none from an older server, reads as `unchecked`. */
export type AssistantSupport = "supported" | "unsupported" | "unchecked" | "none";

/** The whole answer; `runId` is the `run_id` of its ledger rows. */
export interface AssistantAnswer {
  text: string;
  runId: string;
  model: string;
  citations: AssistantCitation[];
  support: AssistantSupport;
  promptTokens: number;
  completionTokens: number;
  /** Op and LOOM_* names the answer wrote that the server neither serves nor documents. */
  unknownNames: string[];
}

export type AssistantEvent = { type: "text"; text: string } | { type: "answer"; answer: AssistantAnswer };

export interface AssistantTurn {
  role: "user" | "assistant";
  content: string;
}

export interface AskInput {
  /** The namespace the answer reads; the token's default namespace when absent. */
  namespace?: string;
  question: string;
  /** The earlier turns, oldest first. */
  history?: AssistantTurn[];
  signal?: AbortSignal;
  /** How long the request, and then each read, waits for data (`DEFAULT_READ_TIMEOUT_MS`). */
  readTimeoutMs?: number;
}

/** The methods bound to a transport; `LoomClient` merges them into `loom.assistant`. */
export interface AssistantChat {
  /** Each piece of the answer's text as it is written, then the answer. A refusal, or a failure after the answer began, throws its typed error. */
  stream(input: AskInput): AsyncGenerator<AssistantEvent, void, undefined>;
}

export function assistantChat(transport: Transport): AssistantChat {
  async function* stream(input: AskInput): AsyncGenerator<AssistantEvent, void, undefined> {
    const body = { stream: true, messages: [...(input.history ?? []), { role: "user", content: input.question }] };
    const headers: Record<string, string> = input.namespace ? { "X-Loom-Namespace": input.namespace } : {};
    // One signal for the request: the caller's abort, or no data for `ms`.
    const ms = input.readTimeoutMs ?? DEFAULT_READ_TIMEOUT_MS;
    const cut = new AbortController();
    let idle = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    // Armed while the request or a read waits; held while the caller handles an event.
    const pause = () => clearTimeout(timer);
    const touch = () => {
      pause();
      timer = setTimeout(() => {
        idle = true;
        cut.abort();
      }, ms);
    };
    const onAbort = () => cut.abort();
    input.signal?.addEventListener("abort", onAbort, { once: true });
    if (input.signal?.aborted) cut.abort();
    let requestId = "";
    // An abort the idle timer made is a timeout, not the caller's.
    const timedOut = (e: unknown) =>
      idle
        ? classed(
            new TimeoutError(`${ASSISTANT_ROUTE}: no data within ${ms} ms`, {
              op: ASSISTANT_ROUTE,
              cause: e,
              requestId,
            }),
            "dependency",
            false,
          )
        : e;
    try {
      touch();
      let res: Response;
      try {
        res = await transport.stream(ASSISTANT_ROUTE, body, { headers, signal: cut.signal });
      } catch (e) {
        throw timedOut(e);
      }
      requestId = res.headers.get("X-Request-Id") ?? "";
      const answer: AssistantAnswer = {
        text: "",
        runId: "",
        model: "",
        citations: [],
        support: "unchecked",
        promptTokens: 0,
        completionTokens: 0,
        unknownNames: [],
      };
      try {
        for await (const data of events(res, transport.baseUrl, requestId, cut.signal, touch)) {
          if (data === "[DONE]") continue;
          const event = parse(data);
          if (!event) continue;
          if (isObject(event.error)) {
            const error = event.error;
            // A build that sent no status: the answer failed upstream, as a 503.
            throw errorFromResponse(ASSISTANT_ROUTE, count(error.status) || 503, { detail: error }, requestId);
          }
          const text = fold(event, answer);
          if (text) {
            pause();
            yield { type: "text", text };
            touch();
          }
        }
      } catch (e) {
        throw timedOut(e);
      }
      if (!answer.runId)
        throw new LoomError(`${ASSISTANT_ROUTE}: the stream ended before the answer began`, { requestId });
      pause();
      yield { type: "answer", answer };
    } finally {
      clearTimeout(timer);
      input.signal?.removeEventListener("abort", onAbort);
    }
  }

  return { stream };
}

/** Each server-sent event's `data`, as the body arrives. */
async function* events(
  res: Response,
  baseUrl: string,
  requestId: string,
  signal: AbortSignal | undefined,
  touch: () => void,
): AsyncGenerator<string> {
  const reader = res.body?.getReader();
  if (!reader) return;
  const decoder = new TextDecoder();
  let buffered = "";
  try {
    for (;;) {
      let chunk: ReadableStreamReadResult<Uint8Array>;
      try {
        chunk = await reader.read();
        touch();
      } catch (e) {
        if (signal?.aborted) {
          const aborted = new LoomError(`${ASSISTANT_ROUTE}: aborted by caller`, {
            op: ASSISTANT_ROUTE,
            cause: e,
            requestId,
          });
          throw classed(aborted, "input", false);
        }
        const cut = new BackendUnavailable(`${ASSISTANT_ROUTE}: response from ${baseUrl} cut off`, {
          op: ASSISTANT_ROUTE,
          cause: e,
          requestId,
          code: NETWORK_ERROR_CODE,
        });
        throw classed(cut, "dependency", false);
      }
      buffered += decoder.decode(chunk.value, { stream: !chunk.done });
      let end = buffered.indexOf("\n\n");
      while (end >= 0) {
        for (const line of buffered.slice(0, end).split("\n")) if (line.startsWith("data: ")) yield line.slice(6);
        buffered = buffered.slice(end + 2);
        end = buffered.indexOf("\n\n");
      }
      if (chunk.done) return;
    }
  } finally {
    // A caller that stops reading closes the answer: the server stops it too.
    await reader.cancel().catch(() => undefined);
  }
}

function parse(data: string): Record<string, unknown> | undefined {
  try {
    const value: unknown = JSON.parse(data);
    return isObject(value) ? value : undefined;
  } catch {
    return undefined;
  }
}

/** A count the server sent, or 0 when it is not one. */
function count(v: unknown): number {
  const n = Math.trunc(Number(v ?? 0));
  return Number.isFinite(n) ? n : 0;
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

const SUPPORTS: readonly string[] = ["supported", "unsupported", "unchecked", "none"];

/** The support the server sent; a value this SDK does not know, or none, reads as unchecked. */
function supportOf(v: unknown): AssistantSupport {
  return typeof v === "string" && SUPPORTS.includes(v) ? (v as AssistantSupport) : "unchecked";
}

/** Fold one chunk into `answer`; the text it adds. */
function fold(event: Record<string, unknown>, answer: AssistantAnswer): string {
  if (event.object === "loom.citations") {
    answer.citations = (Array.isArray(event.citations) ? event.citations : []).filter(isObject).map((c) => ({
      kind: String(c.kind ?? ""),
      text: String(c.text ?? ""),
      verified: c.verified === true,
      reason: String(c.reason ?? ""),
      ref: String(c.ref ?? ""),
      anchor: String(c.anchor ?? ""),
    }));
    answer.support = supportOf(event.support);
    answer.unknownNames = (Array.isArray(event.unknown_names) ? event.unknown_names : []).map(String);
    return "";
  }
  answer.runId ||= String(event.id ?? "");
  answer.model ||= String(event.model ?? "");
  if (isObject(event.usage)) {
    answer.promptTokens = count(event.usage.prompt_tokens);
    answer.completionTokens = count(event.usage.completion_tokens);
  }
  let text = "";
  for (const choice of Array.isArray(event.choices) ? event.choices : []) {
    const delta = isObject(choice) ? choice.delta : undefined;
    if (isObject(delta) && typeof delta.content === "string") text += delta.content;
  }
  answer.text += text;
  return text;
}
