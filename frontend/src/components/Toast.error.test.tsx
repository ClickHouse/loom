// An error toast reads "<context>: <the server's words>" — one lead-in, never two — keeps a
// code's own sentence, and stays up long enough to read.
import { act, cleanup, render, screen } from "../test/rtl";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../lib/errors";
import { ToastViewport, errorToastMs, toastError } from "./Toast";

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
        "Couldn't delete Research notes: Too many requests. Namespace 'x' exceeded its rate limit. Try again in 4 s.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("request id req-9")).toBeInTheDocument();
    act(() =>
      toastError(
        "Couldn't save",
        new ApiError({ status: 409, code: "PRECONDITION_FAILED", message: "the upload expired" }),
      ),
    );
    expect(await screen.findByText("Couldn't save: the upload expired")).toBeInTheDocument();
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
      await screen.findByText("Done. Lists and search catch up shortly; don't send it again."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Delete failed/)).toBeNull();
    expect(screen.queryByText(/Saved/)).toBeNull();
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
