import { describe, expect, it, vi } from "vitest";
import { installStaleChunkReload, STALE_CHUNK_RETRY_WINDOW_MS } from "./staleChunk";

function setup(t0 = 1_000_000) {
  const target = new EventTarget();
  const reload = vi.fn();
  const win = Object.assign(target, { location: { reload } });
  const store = new Map<string, string>();
  const storage = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => {
      store.set(k, v);
    },
  };
  let t = t0;
  installStaleChunkReload(win, storage, () => t);
  const fire = (chunk = "/assets/Code-abc123.js") => {
    const e = Object.assign(new Event("vite:preloadError", { cancelable: true }), {
      payload: new Error(`Failed to fetch dynamically imported module: ${chunk}`),
    });
    win.dispatchEvent(e);
    return e;
  };
  return {
    reload,
    fire,
    advance: (ms: number) => {
      t += ms;
    },
  };
}

describe("installStaleChunkReload", () => {
  it("leaves the error wording's chunk to its own fallback, with no reload", () => {
    const { reload, fire } = setup();
    const e = fire("/assets/errorWords-BsRZ_TNd.js");
    expect(reload).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
  });

  it("reloads once and suppresses the error on a missing chunk", () => {
    const { reload, fire } = setup();
    const e = fire();
    expect(reload).toHaveBeenCalledTimes(1);
    expect(e.defaultPrevented).toBe(true);
  });

  it("lets a second failure right after the reload reach the error boundary", () => {
    const { reload, fire, advance } = setup();
    fire();
    advance(STALE_CHUNK_RETRY_WINDOW_MS - 1);
    const e = fire();
    expect(reload).toHaveBeenCalledTimes(1);
    expect(e.defaultPrevented).toBe(false);
  });

  it("reloads again for a later deploy", () => {
    const { reload, fire, advance } = setup();
    fire();
    advance(STALE_CHUNK_RETRY_WINDOW_MS);
    fire();
    expect(reload).toHaveBeenCalledTimes(2);
  });

  it("still reloads when storage throws", () => {
    const target = new EventTarget();
    const reload = vi.fn();
    const boom = () => {
      throw new Error("blocked");
    };
    installStaleChunkReload(Object.assign(target, { location: { reload } }), { getItem: boom, setItem: boom });
    target.dispatchEvent(new Event("vite:preloadError", { cancelable: true }));
    expect(reload).toHaveBeenCalledTimes(1);
  });
});
