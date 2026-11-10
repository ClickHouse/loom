// When the error wording cannot be used (its chunk failed to load after a deploy, or the
// network dropped), the toast still says what failed, from the caller's context alone.
import { cleanup, render, screen } from "../test/rtl";
import { afterEach, expect, it, vi } from "vitest";
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
