import {
  boundProtection,
  observeResponse,
  refuseResponse,
  type SessionProtection,
  SessionProtectionUnavailable,
} from "./session-protection.js";

const MAX_FRAME_BYTES = 4 * 1024 * 1024;

/** Retain only one unfinished frame, preserving the original CR/LF bytes. */
class Frames {
  private bytes = new Uint8Array(1024);
  private length = 0;
  private lineLength = 0;
  private pendingCR = false;
  complete = false;

  private append(byte: number): void {
    if (this.length === MAX_FRAME_BYTES) throw new SessionProtectionUnavailable();
    if (this.length === this.bytes.length) {
      const next = new Uint8Array(Math.min(MAX_FRAME_BYTES, this.bytes.length * 2));
      next.set(this.bytes);
      this.bytes = next;
    }
    this.bytes[this.length++] = byte;
  }

  private newline(): Uint8Array | undefined {
    if (this.lineLength) {
      this.lineLength = 0;
      return;
    }
    const frame = this.bytes.slice(0, this.length);
    this.length = 0;
    return frame;
  }

  *feed(chunk: Uint8Array): Generator<Uint8Array> {
    for (const byte of chunk) {
      if (this.pendingCR) {
        this.pendingCR = false;
        if (byte === 10) {
          this.append(byte);
          const frame = this.newline();
          if (frame) yield frame;
          continue;
        }
        const frame = this.newline();
        if (frame) yield frame;
      }
      this.append(byte);
      if (byte === 13) this.pendingCR = true;
      else if (byte === 10) {
        const frame = this.newline();
        if (frame) yield frame;
      } else this.lineLength++;
    }
  }

  *finish(): Generator<Uint8Array> {
    let frame: Uint8Array | undefined;
    if (this.pendingCR) {
      this.pendingCR = false;
      frame = this.newline();
    }
    if (this.length) throw new SessionProtectionUnavailable();
    this.complete = true;
    if (frame) yield frame;
  }
}

function dataFields(frame: string): string | undefined {
  const fields: string[] = [];
  for (const line of frame.replace(/\r\n?/g, "\n").split("\n")) {
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    if (field !== "data") continue;
    const value = colon < 0 ? "" : line.slice(colon + 1);
    fields.push(value.startsWith(" ") ? value.slice(1) : value);
  }
  return fields.length ? fields.join("\n") : undefined;
}

function retainMetadata(response: Response, source: Pick<Response, "url" | "redirected" | "type">): Response {
  Object.defineProperties(response, {
    url: { value: source.url },
    redirected: { value: source.redirected },
    type: { value: source.type },
    // Native cloning tees the guarded internal body, never the original source.
    clone: { value: () => retainMetadata(Response.prototype.clone.call(response), source) },
  });
  return response;
}

/** Every native Response body method and clone consumes already-observed frames. */
export async function protectEventStream(
  source: Response,
  roots: readonly SessionProtection[],
  timeoutMs: number,
  signal?: AbortSignal,
): Promise<Response> {
  const encoding = source.headers.get("content-encoding")?.trim().toLowerCase();
  if (
    !source.body ||
    source.bodyUsed ||
    source.body.locked ||
    source.headers.get("content-type")?.split(";", 1)[0]?.trim().toLowerCase() !== "text/event-stream" ||
    (encoding && encoding !== "identity") ||
    source.status < 200 ||
    source.status > 299
  ) {
    const marking = boundProtection(refuseResponse(roots), roots, timeoutMs, signal);
    const closing = boundProtection(Promise.resolve(source.body?.cancel()), roots, timeoutMs, signal);
    await Promise.allSettled([marking, closing]);
    throw new SessionProtectionUnavailable();
  }
  const reader = source.body.getReader();
  const frames = new Frames();
  let pending: Generator<Uint8Array> | undefined;
  let sourceDone = false;
  let complete = false;
  let stopped = false;
  let started = false;
  let controller: ReadableStreamDefaultController<Uint8Array> | undefined;
  let cleanup: Promise<void> | undefined;
  const detached = () => signal?.removeEventListener("abort", onAbort);

  function stop(error: unknown, cancelling = false): Promise<void> {
    if (cleanup) return cleanup;
    stopped = true;
    detached();
    const marking = complete ? Promise.resolve() : refuseResponse(roots);
    const closing = reader.cancel(error).finally(() => reader.releaseLock());
    cleanup = Promise.allSettled([
      boundProtection(marking, roots, timeoutMs, signal),
      boundProtection(closing, roots, timeoutMs, signal),
    ]).then(() => {
      // Returning a read failure joins bounded durable marking and owned cancellation.
      if (!cancelling) controller?.error(error);
    });
    return cleanup;
  }

  function onAbort(): void {
    void stop(new DOMException("The operation was aborted", "AbortError"));
  }

  async function observe(frame: Uint8Array): Promise<void> {
    let text: string;
    try {
      text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(frame);
    } catch {
      throw new SessionProtectionUnavailable();
    }
    if (!started) {
      if (text.startsWith("\ufeff")) text = text.slice(1);
      started = true;
    }
    const data = dataFields(text);
    if (data === undefined || data === "[DONE]") return;
    let value: unknown;
    try {
      value = JSON.parse(data);
    } catch {
      throw new SessionProtectionUnavailable();
    }
    await boundProtection(observeResponse(value, roots), roots, timeoutMs, signal);
  }

  const body = new ReadableStream<Uint8Array>(
    {
      start(value) {
        controller = value;
      },
      async pull(output) {
        try {
          while (!stopped) {
            const frame = pending?.next();
            if (frame && !frame.done) {
              await observe(frame.value);
              if (stopped) return;
              output.enqueue(frame.value);
              if (sourceDone && frames.complete) {
                complete = true;
                detached();
                reader.releaseLock();
                output.close();
              }
              return;
            }
            pending = undefined;
            if (sourceDone) {
              complete = true;
              detached();
              reader.releaseLock();
              output.close();
              return;
            }
            const chunk = await reader.read();
            if (stopped) return;
            sourceDone = chunk.done;
            if (chunk.done) pending = frames.finish();
            else {
              if (!(chunk.value instanceof Uint8Array)) throw new SessionProtectionUnavailable();
              pending = frames.feed(chunk.value);
            }
          }
        } catch (error) {
          await stop(error);
        }
      },
      cancel(reason) {
        return stop(reason, true);
      },
    },
    { highWaterMark: 0 },
  );
  signal?.addEventListener("abort", onAbort, { once: true });
  if (signal?.aborted) onAbort();
  try {
    return retainMetadata(
      new Response(body, {
        status: source.status,
        statusText: source.statusText,
        headers: source.headers,
      }),
      source,
    );
  } catch {
    await stop(new SessionProtectionUnavailable());
    throw new SessionProtectionUnavailable();
  }
}
