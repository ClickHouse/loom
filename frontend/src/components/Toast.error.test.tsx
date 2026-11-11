// An error toast reads "<context>: <the server's words>" — one lead-in, never two — keeps a
// code's own sentence, and stays up long enough to read.
import { act, cleanup, render, screen } from "../test/rtl";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../lib/errors";
import { ToastViewport, committedAnyway, errorToastMs, toastError, toastErrorLine } from "./Toast";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("toastError", () => {
  it("uses the context as the only lead-in and keeps the retry advice", async () => {
    render(<ToastViewport />);
    act(() =>
      toastError(
        "Couldn't delete Research notes",
        new ApiError({
          status: 429,
          code: "RATE_LIMITED",
          message: "namespace 'x' exceeded its rate limit",
          requestId: "req-9",
          detail: { category: "capacity", retryable: true, retry_after_s: 4 },
        }),
      ),
    );
    expect(
      await screen.findByText(
        "Couldn't delete Research notes: Over a limit for now — namespace 'x' exceeded its rate limit. Try again in 4 s.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("request id req-9")).toBeInTheDocument();
    act(() =>
      toastError(
        "Couldn't save",
        new ApiError({ status: 409, code: "PRECONDITION_FAILED", message: "the upload expired" }),
      ),
    );
    expect(await screen.findByText("Couldn't save: the upload expired.")).toBeInTheDocument();
  });

  it("keeps the may-have-committed warning on a write that lost its connection", async () => {
    render(<ToastViewport />);
    act(() => toastError("Save failed", new TypeError("Failed to fetch")));
    expect(
      await screen.findByText(
        "Save failed: Lost connection to Loom mid-request. The work may still have committed on the server — re-check the list before retrying.",
      ),
    ).toBeInTheDocument();
  });

  it("says a committed write went through, not that it failed, for a delete too", async () => {
    render(<ToastViewport />);
    act(() =>
      toastError(
        "Delete failed",
        new ApiError({
          status: 503,
          code: "PUBLICATION_PENDING",
          message: "the write is committed; do not repeat it",
          detail: { category: "dependency", retryable: false, committed: true },
        }),
      ),
    );
    expect(
      await screen.findByText(
        "Done, but lists and search have not caught up yet, so this page may still show the old state. Don't send it once more; reload in a moment to see the result.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Delete failed/)).toBeNull();
    // Information, not a failure: no alert, no danger icon.
    expect(document.querySelector("[data-toast-kind]")).toHaveAttribute("data-toast-kind", "info");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("tells a write's catch that a committed write went through, and toasts every failure", async () => {
    render(<ToastViewport />);
    const pending = new ApiError({ status: 503, code: "PUBLICATION_PENDING", requestId: "req-p", detail: {} });
    let committed = false;
    act(() => {
      committed = committedAnyway("Save failed", pending);
    });
    expect(committed).toBe(true);
    expect(await screen.findByText(/^Done, but lists and search have not caught up yet/)).toBeInTheDocument();
    expect(screen.getByText("request id req-p")).toBeInTheDocument();
    act(() => {
      committed = committedAnyway("Save failed", new ApiError({ status: 409, message: "the row changed" }));
    });
    expect(committed).toBe(false);
    expect(await screen.findByText(/^Save failed: /)).toBeInTheDocument();
  });

  it("gives a caller's own line the request id, and one toast per key", async () => {
    render(<ToastViewport />);
    const err = (id: string) => new ApiError({ status: 503, message: "down", requestId: id });
    act(() => toastErrorLine("Expand of beta failed: down.", err("req-1"), { key: "expand" }));
    expect(await screen.findByText("request id req-1")).toBeInTheDocument();
    act(() => toastErrorLine("Expand of gamma failed: down.", err("req-2"), { key: "expand" }));
    expect(await screen.findByText("Expand of gamma failed: down.")).toBeInTheDocument();
    expect(screen.queryByText("Expand of beta failed: down.")).toBeNull();
  });

  it("words a failed read as a read: no may-have-committed warning", async () => {
    render(<ToastViewport />);
    act(() => toastError("Load failed", new DOMException("signal timed out", "TimeoutError"), { read: true }));
    expect(await screen.findByText("Load failed: Loom didn't answer in time.")).toBeInTheDocument();
  });

  it("keeps an identifier at the start of the server's words as it is", async () => {
    render(<ToastViewport />);
    act(() =>
      toastError(
        "Save failed",
        new ApiError({ status: 403, code: "AUTH_INSUFFICIENT", message: "memory.set needs writer on t" }),
      ),
    );
    expect(
      await screen.findByText("Save failed: Your role doesn't allow this — memory.set needs writer on t."),
    ).toBeInTheDocument();
  });

  it("adds only the code when a coded refusal carries no message", async () => {
    render(<ToastViewport />);
    act(() => toastError("Pin failed", new ApiError({ status: 422, code: "BAD_SHA" })));
    expect(await screen.findByText("Pin failed: error code BAD_SHA.")).toBeInTheDocument();
  });

  it("stays up longer for a longer line, at most 20 s", async () => {
    vi.useFakeTimers();
    render(<ToastViewport />);
    const long = "x".repeat(150);
    toastError("Couldn't save", new Error(long));
    await act(() => vi.dynamicImportSettled());
    act(() => vi.advanceTimersByTime(8_000));
    expect(screen.getByText(`Couldn't save: ${long}`)).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(errorToastMs(`Couldn't save: ${long}`)));
    expect(screen.queryByText(`Couldn't save: ${long}`)).toBeNull();
    expect(errorToastMs("y".repeat(10_000))).toBe(20_000);
  });
});

describe("an internal error's toast", () => {
  it("says the request id once: in the line, with no second line under it", async () => {
    render(<ToastViewport />);
    act(() =>
      toastError(
        "Save failed",
        new ApiError({
          status: 500,
          code: "INTERNAL_ERROR",
          message: "internal error; request_id=r9",
          requestId: "r9",
          detail: { category: "internal", retryable: false },
        }),
      ),
    );
    expect(
      await screen.findByText(
        "Save failed: Loom hit an internal error. If it keeps happening, tell your operator. Request id r9. It may have been saved; check before trying again.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("request id r9")).toBeNull();
  });
});
