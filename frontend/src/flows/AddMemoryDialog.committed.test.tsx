// A memory.set that committed while only Loom's search copy lags (PUBLICATION_PENDING,
// committed: true) was added: the dialog closes as on success instead of showing "Done"
// inside an error with the Add button live again.
import { render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { ApiError } from "../lib/errors";
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
vi.mock("../lib/sharedReads", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/sharedReads")>();
  return { ...actual, sharedTagVocabulary: vi.fn(async () => []) };
});

it("closes and reports the memory as added when the write committed", async () => {
  const onAdded = vi.fn();
  const onOpenChange = vi.fn();
  render(
    <AddMemoryDialog
      open
      onOpenChange={onOpenChange}
      identity={{ org: "dev", namespace: "team", agent: "dashboard" }}
      onAdded={onAdded}
    />,
  );
  const user = userEvent.setup();
  await user.type(screen.getByRole("textbox", { name: /^Key/ }), "user_editor");
  await user.type(screen.getByRole("textbox", { name: /^Value/ }), "vim");
  await user.click(screen.getByRole("button", { name: "Add memory" }));
  await waitFor(() => expect(onAdded).toHaveBeenCalled());
  expect(onOpenChange).toHaveBeenCalledWith(false);
  expect(screen.queryByRole("alert")).toBeNull();
});
