import { afterEach, describe, expect, it, vi } from "vitest";
import { afterPaint, readSession, writeSession } from "./afterPaint";

describe("afterPaint", () => {
  afterEach(() => {
    vi.useRealTimers();
    sessionStorage.clear();
  });

  it("runs after two animation frames and an idle slot, not synchronously", async () => {
    const ran: number[] = [];
    afterPaint(() => ran.push(Date.now()));
    expect(ran).toHaveLength(0);
    await new Promise((r) => setTimeout(r, 0));
    expect(ran).toHaveLength(0); // still inside the frame wait
    await new Promise((r) => setTimeout(r, 120));
    expect(ran).toHaveLength(1);
  });

  it("can be cancelled before it fires", async () => {
    const fn = vi.fn();
    const cancel = afterPaint(fn);
    cancel();
    await new Promise((r) => setTimeout(r, 120));
    expect(fn).not.toHaveBeenCalled();
  });

  it("uses requestIdleCallback when the browser has one", async () => {
    const ric = vi.fn((cb: () => void) => {
      cb();
      return 1;
    });
    vi.stubGlobal("requestIdleCallback", ric);
    const fn = vi.fn();
    afterPaint(fn);
    await new Promise((r) => setTimeout(r, 120));
    expect(ric).toHaveBeenCalledTimes(1);
    expect(fn).toHaveBeenCalledTimes(1);
    vi.unstubAllGlobals();
  });
});

describe("badge session cache", () => {
  afterEach(() => sessionStorage.clear());

  it("round-trips a value within its ttl and forgets it after", () => {
    writeSession("badges:acme/team", { conflicts: 3, overCap: false, newAgents: 1 });
    expect(readSession("badges:acme/team", 60_000)).toEqual({ conflicts: 3, overCap: false, newAgents: 1 });
    expect(readSession("badges:acme/team", -1)).toBeNull();
    expect(readSession("badges:acme/other", 60_000)).toBeNull();
  });

  it("ignores a corrupt entry", () => {
    sessionStorage.setItem("loom_badge_v1:badges:acme/team", "{nope");
    expect(readSession("badges:acme/team", 60_000)).toBeNull();
  });
});
