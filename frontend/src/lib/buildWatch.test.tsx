import { act, cleanup, render, screen } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ReloadBar } from "../components/ReloadBar";
import { BUILD_FIRST_CHECK_MS, BUILD_POLL_MS } from "./buildWatch";
import { invalidateShared } from "./sharedReads";

function health(build: string | null) {
  return vi.fn(
    async () =>
      new Response(JSON.stringify(build ? { status: "ok", build_commit: build } : { status: "ok" }), { status: 200 }),
  );
}

describe("ReloadBar", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    invalidateShared();
  });

  it("stays hidden while the build matches, and appears once a later poll differs", async () => {
    const f = health("aaaaaaaaa");
    vi.stubGlobal("fetch", f);
    render(<ReloadBar />);
    // nothing before the deferred first check
    await act(async () => {
      await Promise.resolve();
    });
    expect(f).not.toHaveBeenCalled();
    await act(async () => {
      vi.advanceTimersByTime(BUILD_FIRST_CHECK_MS + 10);
      await Promise.resolve();
    });
    expect(screen.queryByTestId("reload-bar")).toBeNull();
    // same build on the next poll: still hidden
    await act(async () => {
      vi.advanceTimersByTime(BUILD_POLL_MS + 10);
      await Promise.resolve();
    });
    expect(screen.queryByTestId("reload-bar")).toBeNull();
    // the server moved
    f.mockImplementation(
      async () => new Response(JSON.stringify({ status: "ok", build_commit: "bbbbbbbbb" }), { status: 200 }),
    );
    await act(async () => {
      vi.advanceTimersByTime(BUILD_POLL_MS + 10);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByTestId("reload-bar").textContent).toContain("bbbbbbbbb");
    expect(f).toHaveBeenCalledTimes(3);
  });

  it("an unreachable or build-less health answer never counts as a change", async () => {
    const f = vi.fn(async () => new Response("oops", { status: 503 }));
    vi.stubGlobal("fetch", f);
    render(<ReloadBar />);
    await act(async () => {
      vi.advanceTimersByTime(BUILD_FIRST_CHECK_MS + 10);
      await Promise.resolve();
    });
    await act(async () => {
      vi.advanceTimersByTime(BUILD_POLL_MS + 10);
      await Promise.resolve();
    });
    expect(screen.queryByTestId("reload-bar")).toBeNull();
  });
});
