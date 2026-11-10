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
  it("uses the context as the only lead-in and keeps the retry advice", () => {
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
      screen.getByText(
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
    expect(screen.getByText("Couldn't save: the upload expired")).toBeInTheDocument();
  });

  it("stays up longer for a longer line, at most 20 s", () => {
    vi.useFakeTimers();
    render(<ToastViewport />);
    const long = "x".repeat(150);
    act(() => toastError("Couldn't save", new Error(long)));
    act(() => vi.advanceTimersByTime(8_000));
    expect(screen.getByText(`Couldn't save: ${long}`)).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(errorToastMs(`Couldn't save: ${long}`)));
    expect(screen.queryByText(`Couldn't save: ${long}`)).toBeNull();
    expect(errorToastMs("y".repeat(10_000))).toBe(20_000);
  });
});
