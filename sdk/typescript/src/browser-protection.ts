import { protectEventStream } from "./protected-stream.js";
import {
  AutomaticCaptureSuppressed,
  boundProtection,
  observeJsonResponse,
  protectionHeaders,
  refuseResponse,
  requireAutomaticAllowed,
  requireAutomaticLocallyAllowed,
  SessionCapture,
  type SessionProtection,
  SessionProtectionUnavailable,
} from "./session-protection.js";
import type { FetchLike } from "./transport.js";

export interface ProtectedBrowserTransport {
  /** Explicit reads and writes still observe every response. */
  fetch: FetchLike;
  /** Use for all automatic callbacks, including writes to another namespace. */
  automaticFetch: FetchLike;
  /** GET downloads are conservatively marked before any opaque bytes are released. */
  downloadFetch: FetchLike;
}

async function readDownloadBody(
  response: Response,
  roots: readonly SessionProtection[],
  timeoutMs: number,
  signal: AbortSignal,
  maximum: number,
): Promise<ArrayBuffer> {
  const reader = response.body?.getReader();
  if (!reader) return new ArrayBuffer(0);
  const parts: Uint8Array[] = [];
  let bytes = 0;
  try {
    for (;;) {
      const part = await boundProtection(reader.read(), roots, timeoutMs, signal);
      if (part.done) break;
      bytes += part.value.byteLength;
      if (bytes > maximum || parts.length >= 4096) throw new SessionProtectionUnavailable();
      parts.push(part.value);
    }
    const body = new ArrayBuffer(bytes);
    const joined = new Uint8Array(body);
    let offset = 0;
    for (const part of parts) {
      joined.set(part, offset);
      offset += part.byteLength;
    }
    return body;
  } catch (error) {
    await boundProtection(reader.cancel(), roots, timeoutMs);
    throw error;
  } finally {
    reader.releaseLock();
  }
}

async function readJsonBody(
  response: Response,
  roots: readonly SessionProtection[],
  timeoutMs: number,
  signal: AbortSignal,
): Promise<string> {
  if (!response.body) return "";
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let text = "";
  let bytes = 0;
  try {
    for (;;) {
      const chunk = await boundProtection(reader.read(), roots, timeoutMs, signal);
      if (chunk.done) return text + decoder.decode();
      bytes += chunk.value.byteLength;
      if (bytes > 4 * 1024 * 1024) throw new SessionProtectionUnavailable();
      text += decoder.decode(chunk.value, { stream: true });
    }
  } catch (error) {
    await boundProtection(reader.cancel(), roots, timeoutMs);
    throw error;
  } finally {
    reader.releaseLock();
  }
}

/** Bind the application's existing durable family before its first private read. */
export function protectedBrowserTransport(options: {
  baseUrl: string;
  sessionProtection: SessionProtection;
  fetch?: FetchLike;
  timeoutMs?: number;
  /** Opaque downloads may lower this 64 MiB ceiling; JSON keeps its separate 4 MiB bound. */
  maxDownloadBytes?: number;
}): ProtectedBrowserTransport {
  const base = new URL(options.baseUrl);
  const roots = new SessionCapture([options.sessionProtection]).roots;
  const fetchImpl = options.fetch ?? globalThis.fetch.bind(globalThis);
  const timeoutMs = options.timeoutMs ?? 60_000;
  const maxDownloadBytes = options.maxDownloadBytes ?? 64 * 1024 * 1024;
  if (!Number.isSafeInteger(maxDownloadBytes) || maxDownloadBytes < 1 || maxDownloadBytes > 64 * 1024 * 1024) {
    throw new SessionProtectionUnavailable();
  }
  if (base.username || base.password || !["http:", "https:"].includes(base.protocol)) {
    throw new SessionProtectionUnavailable();
  }

  async function send(input: string, init: RequestInit, automatic: boolean, download = false): Promise<Response> {
    const url = new URL(input, base);
    if (url.origin !== base.origin || url.username || url.password) throw new SessionProtectionUnavailable();
    if (download && (init.method ?? "GET").toUpperCase() !== "GET") throw new SessionProtectionUnavailable();
    const controller = new AbortController();
    const onAbort = () => controller.abort();
    init.signal?.addEventListener("abort", onAbort, { once: true });
    if (init.signal?.aborted) controller.abort();
    const timer = setTimeout(onAbort, timeoutMs);
    let response: Response | undefined;
    try {
      // Presence refuses even when Headers has normalized duplicate values.
      const given: Record<string, string> = {};
      new Headers(init.headers).forEach((value, name) => {
        given[name] = value;
      });
      const headers = await boundProtection(
        protectionHeaders(given, roots),
        roots,
        timeoutMs,
        controller.signal,
      );
      await boundProtection(requireAutomaticAllowed(roots, automatic), roots, timeoutMs, controller.signal);
      requireAutomaticLocallyAllowed(roots, automatic);
      if (controller.signal.aborted) throw new SessionProtectionUnavailable();
      response = await boundProtection(
        fetchImpl(url.href, { ...init, headers, signal: controller.signal, redirect: "error" }),
        roots,
        timeoutMs,
        controller.signal,
      );
      if (download && ![204, 304].includes(response.status)) {
        // Opaque exports have no envelope contract, including JSON and line-delimited JSON.
        await boundProtection(Promise.all(roots.map((root) => root.mark())), roots, timeoutMs, controller.signal);
        const body = await readDownloadBody(response, roots, timeoutMs, controller.signal, maxDownloadBytes);
        return new Response(body, {
          status: response.status,
          statusText: response.statusText,
          headers: response.headers,
        });
      }
      if (response.headers.get("content-type")?.split(";", 1)[0]?.trim().toLowerCase() === "text/event-stream") {
        // The returned observer owns cancellation after these opening listeners are detached.
        return await protectEventStream(response, roots, timeoutMs, init.signal ?? undefined);
      }
      if (!response.headers.get("content-type")?.toLowerCase().includes("json") && ![204, 304].includes(response.status)) {
        return await boundProtection(refuseResponse(roots), roots, timeoutMs, controller.signal);
      }
      const text = await readJsonBody(response, roots, timeoutMs, controller.signal);
      await boundProtection(observeJsonResponse(text, response.status, roots), roots, timeoutMs, controller.signal);
      return new Response([204, 304].includes(response.status) ? null : text, {
        status: response.status,
        statusText: response.statusText,
        headers: response.headers,
      });
    } catch (error) {
      // An unconfirmed response never becomes a clean family after cancellation or decoding failure.
      await Promise.allSettled([
        boundProtection(refuseResponse(roots), roots, timeoutMs),
        boundProtection(Promise.resolve(response?.body?.cancel()), roots, timeoutMs),
      ]);
      if (error instanceof AutomaticCaptureSuppressed || error instanceof SessionProtectionUnavailable) throw error;
      throw new SessionProtectionUnavailable();
    } finally {
      clearTimeout(timer);
      init.signal?.removeEventListener("abort", onAbort);
    }
  }

  return Object.freeze({
    fetch: (input: string, init: RequestInit) => send(input, init, false),
    automaticFetch: (input: string, init: RequestInit) => send(input, init, true),
    downloadFetch: (input: string, init: RequestInit) => send(input, init, false, true),
  });
}
