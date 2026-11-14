import { act, render, screen } from "../test/rtl";
import { StrictMode, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useAbortableEffect, useKeyboard, useMounted, useVisiblePoll } from "./hooks";
import { must } from "../test/must";

// A read that lands after its view moved on is dropped, and the request itself is
// aborted; a poll sleeps while the tab is hidden.

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (v: T) => void;
}
function deferred<T>(): Deferred<T> {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

function Rows({
  ns,
  fetchRows,
  signals,
}: {
  ns: string;
  fetchRows: (ns: string, signal: AbortSignal) => Promise<string[]>;
  signals: AbortSignal[];
}) {
  const [shown, setShown] = useState("nothing yet");
  useAbortableEffect(
    async (signal, live) => {
      signals.push(signal);
      const rows = await fetchRows(ns, signal);
      if (!live()) return;
      setShown(`${ns}: ${rows.join(",")}`);
    },
    [ns],
  );
  return <output>{shown}</output>;
}

describe("useAbortableEffect", () => {
  it("drops a response that lands after the deps changed, and aborts its request", async () => {
    const pending = new Map<string, Deferred<string[]>>();
    const signals: AbortSignal[] = [];
    const fetchRows = (ns: string) => {
      const d = deferred<string[]>();
      pending.set(ns, d);
      return d.promise;
    };
    const { rerender } = render(<Rows ns="a" fetchRows={fetchRows} signals={signals} />);
    await act(async () => {});
    rerender(<Rows ns="b" fetchRows={fetchRows} signals={signals} />);
    await act(async () => {});
    expect(signals).toHaveLength(2);
    expect(signals[0].aborted).toBe(true);
    expect(signals[1].aborted).toBe(false);

    // b answers first, then a's stale response arrives: b must stay.
    await act(async () => {
      must(pending.get("b"), "pending b").resolve(["b1", "b2"]);
    });
    expect(screen.getByRole("status")).toHaveTextContent("b: b1,b2");
    await act(async () => {
      must(pending.get("a"), "pending a").resolve(["a1"]);
    });
    expect(screen.getByRole("status")).toHaveTextContent("b: b1,b2");
  });

  it("does not set state after unmount and swallows the aborted request's rejection", async () => {
    const signals: AbortSignal[] = [];
    let settled = false;
    const fetchRows = (_ns: string, signal: AbortSignal) =>
      new Promise<string[]>((_resolve, reject) => {
        signal.addEventListener("abort", () => {
          settled = true;
          reject(new DOMException("Aborted", "AbortError"));
        });
      });
    const { unmount } = render(<Rows ns="a" fetchRows={fetchRows} signals={signals} />);
    await act(async () => {});
    expect(screen.getByRole("status")).toHaveTextContent("nothing yet");
    unmount();
    await act(async () => {});
    expect(signals[0].aborted).toBe(true);
    expect(settled).toBe(true);
  });
});

function Poller({ fn, ms, enabled = true }: { fn: () => void; ms: number; enabled?: boolean }) {
  useVisiblePoll(fn, ms, enabled);
  return null;
}

describe("useVisiblePoll", () => {
  let hidden = false;
  afterEach(() => {
    hidden = false;
    vi.useRealTimers();
  });

  function stubHidden(): void {
    Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
  }

  it("ticks while visible, skips hidden ticks, and catches up once on return", () => {
    vi.useFakeTimers();
    stubHidden();
    const fn = vi.fn();
    render(<Poller fn={fn} ms={1000} />);
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(fn).toHaveBeenCalledTimes(1);

    hidden = true;
    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(fn).toHaveBeenCalledTimes(1);

    hidden = false;
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(fn).toHaveBeenCalledTimes(2);
    // A second visibilitychange without a missed tick does nothing.
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(fn).toHaveBeenCalledTimes(2);
  });

  it("arms nothing when disabled", () => {
    vi.useFakeTimers();
    stubHidden();
    const fn = vi.fn();
    render(<Poller fn={fn} ms={500} enabled={false} />);
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(fn).not.toHaveBeenCalled();
  });
});

describe("useKeyboard", () => {
  it("fires a bound key, and leaves it to a field that has the focus", () => {
    const escape = vi.fn();
    function Bound() {
      useKeyboard({ Escape: escape });
      return <input aria-label="field" />;
    }
    render(<Bound />);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(escape).toHaveBeenCalledTimes(1);
    screen.getByLabelText("field").focus();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(escape).toHaveBeenCalledTimes(1);
  });
});

describe("useMounted", () => {
  it("is true while mounted, under StrictMode too, and false once unmounted", () => {
    for (const strict of [false, true]) {
      let seen: { readonly current: boolean } | null = null;
      function Probe() {
        seen = useMounted();
        return null;
      }
      const { unmount } = render(
        strict ? (
          <StrictMode>
            <Probe />
          </StrictMode>
        ) : (
          <Probe />
        ),
      );
      const mounted = must(seen as { readonly current: boolean } | null, "the mounted ref");
      expect(mounted.current).toBe(true);
      unmount();
      expect(mounted.current).toBe(false);
    }
  });
});
