// When the error wording cannot be used (its chunk failed to load after a deploy, or the
// network dropped), the toast still says what failed, from the caller's context alone.
import { cleanup, render, screen } from "../test/rtl";
import { afterEach, expect, it, vi } from "vitest";
import { ApiError } from "../lib/errors";
import { ToastViewport, toastError } from "./Toast";

// Throwing inside the import's `then` rejects the same promise a failed chunk load does.
vi.mock("../lib/errorWords", () => ({
  humanizeError: () => {
    throw new Error("chunk gone");
  },
}));

afterEach(cleanup);

it("falls back to the context when the wording cannot load", async () => {
  render(<ToastViewport />);
  toastError("Couldn't delete Research notes", new Error("boom"));
  expect(await screen.findByText("Couldn't delete Research notes.")).toBeInTheDocument();
});

it("keeps a write's lost-connection warning when the wording cannot load", async () => {
  render(<ToastViewport />);
  toastError("Save failed", new TypeError("Failed to fetch"));
  expect(
    await screen.findByText(/^Save failed: Lost connection to Loom mid-request\. The work may still have committed/),
  ).toBeInTheDocument();
});

it("keeps a write's timeout and unreadable-answer warnings when the wording cannot load", async () => {
  render(<ToastViewport />);
  toastError("Rename failed", new DOMException("signal timed out", "TimeoutError"));
  expect(
    await screen.findByText(/^Rename failed: Loom didn't answer in time\. It may still finish/),
  ).toBeInTheDocument();
  toastError("Pin failed", new ApiError({ status: 200, code: "ANSWER_UNREADABLE" }));
  expect(await screen.findByText("Pin failed: It may have been saved; check before trying again.")).toBeInTheDocument();
  toastError("Load failed", new DOMException("signal timed out", "TimeoutError"), { read: true });
  expect(await screen.findByText("Load failed.")).toBeInTheDocument();
});
