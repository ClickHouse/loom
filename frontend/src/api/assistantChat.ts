// The assistant's chat: `POST /v1/assistant/chat/completions` (server/assistant/app/routes.py),
// OpenAI's chat request and its streamed chunks. Loom stores no conversation, so each turn
// sends every earlier message. A refusal before the stream starts is the request's ApiError;
// one after it starts is an error event, thrown here as an ApiError too.
import { postStreaming } from "../api";
import { ApiError } from "../lib/errors";
import { isRecord } from "../lib/guards";

export const CHAT_PATH = "/v1/assistant/chat/completions";

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

/** One citation of a finished answer, as the server checked it against Loom: `ref` is the
 *  memory key, document source, documentation page or code path it names. */
export interface Citation {
  kind: string;
  text: string;
  verified: boolean;
  reason: string;
  ref: string;
  /** A verified help citation's section anchor on its page; "" otherwise. */
  anchor?: string;
}

/** One tool call the answer made, as its ledger row shows it (server/assistant/domain/steps.py):
 *  its op, the words for it ("Read the audit", "Couldn't search memories") and whether it
 *  succeeded. */
export interface Step {
  op: string;
  text: string;
  ok: boolean;
}

/** The steps of an event, whether the run held more than it lists, and whether the event
 *  carries a list at all (`listed`: an answer's last event leaves it out when its run could
 *  not be read, and a server that sends no steps never sends one). */
export function stepsOf(chunk: Record<string, unknown>): { steps: Step[]; cut: boolean; listed: boolean } {
  const raw = Array.isArray(chunk.steps) ? chunk.steps : [];
  const steps = raw.flatMap((s) =>
    isRecord(s) && typeof s.text === "string"
      ? [{ op: typeof s.op === "string" ? s.op : "", text: s.text, ok: s.ok === true }]
      : [],
  );
  return { steps, cut: chunk.steps_cut === true, listed: Array.isArray(chunk.steps) };
}

/** How far an answer rests on what Loom holds (server/assistant/domain/support.py). */
export type Support = "supported" | "unsupported" | "unchecked" | "none";

const SUPPORTS: readonly string[] = ["supported", "unsupported", "unchecked", "none"];

/** The support the server sent; a value this dashboard does not know, or none at all,
 *  reads as unchecked. */
export function supportOf(raw: unknown): Support {
  return typeof raw === "string" && SUPPORTS.includes(raw) ? (raw as Support) : "unchecked";
}

export interface ChatHandlers {
  /** Each piece of the answer's text, in order. */
  onText: (text: string) => void;
  /** The answer's citations, checked, its support, the op and setting names it wrote that
   *  the server neither serves nor documents, and what it read, once its text is complete. */
  onCitations?: (
    citations: Citation[],
    support: Support,
    unknownNames: string[],
    read: { steps: Step[]; cut: boolean; listed: boolean },
  ) => void;
  /** What the answer has read so far, while it streams: the whole list each time. */
  onSteps?: (read: { steps: Step[]; cut: boolean; listed: boolean }) => void;
  /** The answer's text is complete and its citations are being checked (`loom.text_done`;
   *  a server that sends none goes straight to `onCitations`). */
  onTextDone?: () => void;
}

function citationOf(raw: unknown): Citation | null {
  if (!isRecord(raw) || typeof raw.text !== "string") return null;
  const str = (v: unknown) => (typeof v === "string" ? v : "");
  return {
    kind: str(raw.kind),
    text: raw.text,
    verified: raw.verified === true,
    reason: str(raw.reason),
    ref: str(raw.ref),
    anchor: str(raw.anchor),
  };
}

/** An error event's payload as an ApiError; `status` 0 says it came inside the stream. */
function streamError(raw: unknown, requestId: string): ApiError {
  const detail = isRecord(raw) ? raw : {};
  const message = typeof raw === "string" ? raw : typeof detail.message === "string" ? detail.message : "";
  const code = typeof detail.code === "string" ? detail.code : "";
  return new ApiError({ status: 0, code, message, detail, requestId });
}

/** The answer stopped before `[DONE]`: its connection was lost. */
const streamEnded = (requestId: string) =>
  new ApiError({ status: 0, code: "STREAM_ENDED", message: "the answer stopped before it was complete", requestId });

/** One server-sent event's data: true at `[DONE]`. */
function readEvent(data: string, on: ChatHandlers, requestId: string): boolean {
  if (data === "[DONE]") return true;
  let chunk: unknown;
  try {
    chunk = JSON.parse(data);
  } catch {
    return false; // not a chunk: nothing to show
  }
  if (!isRecord(chunk)) return false;
  if (chunk.error) throw streamError(chunk.error, requestId);
  if (chunk.object === "loom.citations") {
    const all = Array.isArray(chunk.citations) ? chunk.citations : [];
    on.onCitations?.(
      all.map(citationOf).filter((c): c is Citation => c !== null),
      supportOf(chunk.support),
      Array.isArray(chunk.unknown_names) ? chunk.unknown_names.filter((n) => typeof n === "string") : [],
      stepsOf(chunk),
    );
    return false;
  }
  if (chunk.object === "loom.step") {
    on.onSteps?.(stepsOf(chunk));
    return false;
  }
  if (chunk.object === "loom.text_done") {
    on.onTextDone?.();
    return false;
  }
  const choices = Array.isArray(chunk.choices) ? chunk.choices : [];
  for (const choice of choices) {
    const delta = isRecord(choice) && isRecord(choice.delta) ? choice.delta : null;
    if (delta && typeof delta.content === "string" && delta.content) on.onText(delta.content);
  }
  return false;
}

/** Ask the assistant in `namespace`, with the whole conversation in `messages` (its last is
 *  the question). Resolves when the answer ends; an abort of `signal` closes the request
 *  and rejects with the abort. */
export async function streamAssistantChat(
  namespace: string,
  messages: ChatMessage[],
  on: ChatHandlers,
  signal: AbortSignal,
): Promise<void> {
  const r = await postStreaming(
    CHAT_PATH,
    { stream: true, messages },
    { "X-Loom-Namespace": namespace, Accept: "text/event-stream" },
    signal,
  );
  if (!r.body) throw new ApiError({ status: r.status, message: "the answer carried no body" });
  const requestId = r.headers.get("X-Request-Id") ?? "";
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buffered = "";
  try {
    for (;;) {
      let read: ReadableStreamReadResult<Uint8Array>;
      try {
        read = await reader.read();
      } catch (e) {
        // The caller's abort stays an abort; a connection lost mid-answer cut it short.
        if (signal.aborted) throw e;
        throw streamEnded(requestId);
      }
      const { value, done } = read;
      if (done) break;
      buffered += decoder.decode(value, { stream: true }).replace(/\r\n?/g, "\n");
      // An event ends at a blank line; its data lines join with newlines.
      for (let end = buffered.indexOf("\n\n"); end >= 0; end = buffered.indexOf("\n\n")) {
        const event = buffered.slice(0, end);
        buffered = buffered.slice(end + 2);
        const data = event
          .split("\n")
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(line.startsWith("data: ") ? 6 : 5))
          .join("\n");
        if (data && readEvent(data, on, requestId)) return;
      }
    }
  } finally {
    reader.cancel().catch(() => {
      // Already closed or aborted: nothing left to release.
    });
  }
  // The server always ends with [DONE]: a stream that stops short lost its connection.
  throw streamEnded(requestId);
}
