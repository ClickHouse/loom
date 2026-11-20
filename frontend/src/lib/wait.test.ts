import { afterEach, describe, expect, it, vi } from "vitest";
import { wait } from "./wait";

describe("wait", () => {
  afterEach(() => vi.useRealTimers());

  it("resolves once its time is up", async () => {
    vi.useFakeTimers();
    let done = false;
    const waiting = wait(1000, new AbortController().signal).then(() => {
      done = true;
    });
    await vi.advanceTimersByTimeAsync(999);
    expect(done).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    await waiting;
    expect(done).toBe(true);
  });

  it("rejects with the reason as soon as its signal aborts, and clears its timer", async () => {
    vi.useFakeTimers();
    const controller = new AbortController();
    const waiting = wait(1000, controller.signal);
    const reason = new Error("left");
    controller.abort(reason);
    await expect(waiting).rejects.toBe(reason);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("rejects at once on a signal already aborted", async () => {
    const controller = new AbortController();
    const reason = new Error("gone");
    controller.abort(reason);
    await expect(wait(1000, controller.signal)).rejects.toBe(reason);
  });
});
