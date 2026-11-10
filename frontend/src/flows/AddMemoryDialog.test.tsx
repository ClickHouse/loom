// A memory that committed while only search lags is saved: the dialog closes as on success,
// so it never invites the second write its line warns against.
import { render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { ApiError } from "../lib/errors";
import { ToastViewport } from "../components/Toast";
import { AddMemoryDialog } from "./AddMemoryDialog";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    setMemory: vi.fn(async () => {
      throw new ApiError({
        status: 503,
        code: "PUBLICATION_PENDING",
        message: "the write is committed; do not repeat it",
        detail: { category: "dependency", retryable: false, committed: true },
      });
    }),
  };
});

it("closes on a write whose search copy is still pending, and says it is done", async () => {
  const onOpenChange = vi.fn();
  const onAdded = vi.fn();
  render(
    <>
      <ToastViewport />
      <AddMemoryDialog
        open
        onOpenChange={onOpenChange}
        identity={{ org: "dev", namespace: "team", agent: "dashboard" }}
        onAdded={onAdded}
      />
    </>,
  );
  const user = userEvent.setup();
  await user.type(screen.getByRole("textbox", { name: /^Key/ }), "user_pref");
  await user.type(screen.getByRole("textbox", { name: /^Value/ }), "dark mode");
  await user.click(screen.getByRole("button", { name: /^Add memory$/ }));
  await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
  expect(onAdded).toHaveBeenCalled();
  expect(await screen.findByText(/^Done, but lists and search have not caught up yet/)).toBeInTheDocument();
  expect(screen.queryByText(/Add memory:/)).toBeNull();
});
