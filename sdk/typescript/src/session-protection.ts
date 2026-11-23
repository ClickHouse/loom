/** App-owned durable control shared by every client in one logical conversation. */
export interface SessionProtectionStore {
  /** Missing, damaged or inaccessible state must reject; restore never creates a clean record. */
  read(): Promise<"clean" | "protected">;
  /** Resolve only after a monotonic protected state is durably committed. No reset operation. */
  mark(): Promise<void>;
}

export class SessionProtectionUnavailable extends Error {
  constructor() {
    super("Session protection state could not be confirmed");
    this.name = "SessionProtectionUnavailable";
  }
}

export class AutomaticCaptureSuppressed extends Error {
  constructor() {
    super("Automatic session capture is suppressed");
    this.name = "AutomaticCaptureSuppressed";
  }
}

/** A trusted application supplies storage; this handle never initializes or repairs it. */
export class SessionProtection {
  readonly #read: SessionProtectionStore["read"];
  readonly #mark: SessionProtectionStore["mark"];
  #tainted = false;
  #pending: Promise<void> | undefined;

  constructor(store: SessionProtectionStore) {
    if (!store || typeof store.read !== "function" || typeof store.mark !== "function") {
      throw new SessionProtectionUnavailable();
    }
    this.#read = store.read.bind(store);
    this.#mark = store.mark.bind(store);
  }

  get locallyProtected(): boolean {
    return this.#tainted;
  }

  async protected(): Promise<boolean> {
    if (this.#tainted) return true;
    try {
      if ((await this.#read()) !== "clean") this.#tainted = true;
    } catch {
      this.#tainted = true;
    }
    return this.#tainted;
  }

  async requireReady(): Promise<void> {
    try {
      if (this.#pending) await this.#pending;
      const state = await this.#read();
      if (state !== "clean" && state !== "protected") throw new SessionProtectionUnavailable();
      if (state === "protected") this.#tainted = true;
      if (this.#tainted && state !== "protected") throw new SessionProtectionUnavailable();
    } catch {
      this.#tainted = true;
      throw new SessionProtectionUnavailable();
    }
  }

  mark(): Promise<void> {
    // The latch stops concurrent callbacks; only the durable acknowledgement releases content.
    this.#tainted = true;
    this.#pending ??= Promise.resolve()
      .then(async () => {
        try {
          await this.#mark();
          if ((await this.#read()) !== "protected") throw new SessionProtectionUnavailable();
        } catch {
          throw new SessionProtectionUnavailable();
        }
      })
      .finally(() => {
        this.#pending = undefined;
      });
    return this.#pending;
  }
}

/** Explicit invocation control avoids a mutable global stack across browser promises. */
export class SessionCapture {
  readonly roots: readonly SessionProtection[];

  constructor(roots: readonly SessionProtection[]) {
    if (!roots.length || roots.some((root) => !(root instanceof SessionProtection))) {
      throw new SessionProtectionUnavailable();
    }
    this.roots = Object.freeze([...new Set(roots)]);
    Object.freeze(this);
  }

  with(root: SessionProtection): SessionCapture {
    return new SessionCapture([...this.roots, root]);
  }
}

export function responseRoots(
  root?: SessionProtection,
  capture?: SessionCapture,
  observe?: SessionCapture,
): readonly SessionProtection[] {
  const roots = [...new Set([...(capture?.roots ?? []), ...(observe?.roots ?? []), ...(root ? [root] : [])])];
  if (roots.some((family) => !(family instanceof SessionProtection))) throw new SessionProtectionUnavailable();
  return Object.freeze(roots);
}

/** Only an installed observer may declare the existing request protocol. */
export async function protectionHeaders(
  headers: Record<string, string>,
  roots: readonly SessionProtection[],
): Promise<Record<string, string>> {
  if (Object.keys(headers).some((name) => name.toLowerCase() === "x-loom-session-protection")) {
    throw new SessionProtectionUnavailable();
  }
  await Promise.all(roots.map((root) => root.requireReady()));
  return roots.length ? { ...headers, "X-Loom-Session-Protection": "v1" } : { ...headers };
}

export async function requireAutomaticAllowed(roots: readonly SessionProtection[], automatic: boolean): Promise<void> {
  if (!automatic) return;
  for (const root of roots) {
    if (await root.protected()) throw new AutomaticCaptureSuppressed();
  }
}

export function requireAutomaticLocallyAllowed(roots: readonly SessionProtection[], automatic: boolean): void {
  if (automatic && roots.some((root) => root.locallyProtected)) throw new AutomaticCaptureSuppressed();
}

async function markEveryRoot(roots: readonly SessionProtection[]): Promise<boolean> {
  // Each handle latches synchronously before any durable attempt can block another.
  const results = await Promise.allSettled(roots.map((root) => root.mark()));
  return results.some((result) => result.status === "rejected");
}

export async function refuseResponse(roots: readonly SessionProtection[]): Promise<never> {
  await markEveryRoot(roots);
  throw new SessionProtectionUnavailable();
}

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function markedEnvelope(value: unknown): boolean {
  const pending: unknown[] = [value];
  const visited = new Set<object>();
  let remaining = 10_000;
  let textBytes = 4 * 1024 * 1024;
  while (pending.length) {
    if (--remaining < 0) throw new SessionProtectionUnavailable();
    const item = pending.pop();
    if (!object(item)) continue;
    if (visited.has(item)) throw new SessionProtectionUnavailable();
    visited.add(item);
    if (Object.hasOwn(item, "mirrored") && typeof item.mirrored !== "boolean") {
      throw new SessionProtectionUnavailable();
    }
    if (item.mirrored === true) return true;
    for (const key of ["result", "structuredContent"]) {
      if (object(item[key])) pending.push(item[key]);
    }
    if (object(item.error) && object(item.error.data)) pending.push(item.error.data);
    if (Array.isArray(item.items)) {
      if (pending.length + item.items.length > remaining) throw new SessionProtectionUnavailable();
      pending.push(...item.items);
    }
    if (Array.isArray(item.content)) {
      if (pending.length + item.content.length > remaining) throw new SessionProtectionUnavailable();
      remaining -= item.content.length;
      for (const block of item.content) {
        if (!object(block) || block.type !== "text" || typeof block.text !== "string") continue;
        if (block.text.length > textBytes) throw new SessionProtectionUnavailable();
        textBytes -= new TextEncoder().encode(block.text).byteLength;
        if (textBytes < 0) throw new SessionProtectionUnavailable();
        const text = block.text.trim();
        if (text.startsWith("{")) pending.push(JSON.parse(text));
      }
    }
  }
  return false;
}

export async function observeResponse<T>(value: T, roots: readonly SessionProtection[]): Promise<T> {
  if (!roots.length) return value;
  let marked: boolean;
  try {
    await Promise.all(roots.map((root) => root.requireReady()));
    if (!object(value)) throw new SessionProtectionUnavailable();
    marked = markedEnvelope(value);
  } catch {
    return refuseResponse(roots);
  }
  if (marked && (await markEveryRoot(roots))) throw new SessionProtectionUnavailable();
  return value;
}

/** Decode a bound JSON envelope before status handling, retry or typed conversion. */
export async function observeJsonResponse(
  text: string,
  status: number,
  roots: readonly SessionProtection[],
): Promise<unknown> {
  let value: unknown;
  try {
    if (!text && status !== 204 && status !== 304) throw new SessionProtectionUnavailable();
    value = text ? JSON.parse(text) : {};
  } catch {
    return refuseResponse(roots);
  }
  return observeResponse(value, roots);
}

/** Refuse within the call lifetime even when an application store ignores cancellation. */
export function boundProtection<T>(
  work: Promise<T>,
  roots: readonly SessionProtection[],
  timeoutMs: number,
  signal?: AbortSignal,
): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let onAbort: (() => void) | undefined;
  const cut = new Promise<never>((_, reject) => {
    onAbort = () => {
      // All handles latch now; late durable settlements remain consumed and cannot release output.
      void Promise.allSettled(roots.map((root) => root.mark()));
      reject(new SessionProtectionUnavailable());
    };
    signal?.addEventListener("abort", onAbort, { once: true });
    if (signal?.aborted || timeoutMs <= 0) onAbort();
    else timer = setTimeout(onAbort, timeoutMs);
  });
  return Promise.race([work, cut]).finally(() => {
    clearTimeout(timer);
    if (onAbort) signal?.removeEventListener("abort", onAbort);
  });
}
