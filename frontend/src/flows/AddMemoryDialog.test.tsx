/**
 * Add memory: a write that committed with only lists and search behind closes the dialog as
 * after a success, so the person does not send it again; a failure keeps it open and says why.
 */
import { cleanup, render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  setMemory: vi.fn(),
}));
vi.mock("../lib/sharedReads", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/sharedReads")>()),
  sharedTagVocabulary: vi.fn(async () => []),
}));
vi.mock("../components/Toast", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../components/Toast")>()),
  toast: vi.fn(),
  toastCommitted: vi.fn(),
}));
import { setMemory } from "../api";
import { toast, toastCommitted } from "../components/Toast";
import { ApiError } from "../lib/errors";
import { AddMemoryDialog } from "./AddMemoryDialog";

const identity = { org: "acme", namespace: "team", agent: "loom-ui" };

async function mount() {
  const onAdded = vi.fn();
  const onOpenChange = vi.fn();
  render(<AddMemoryDialog open onOpenChange={onOpenChange} identity={identity} onAdded={onAdded} />);
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Key (lower_snake_case)"), "coffee_pref");
  await user.type(screen.getByLabelText("Value"), "oat milk");
  await user.click(screen.getByRole("button", { name: "Add memory" }));
  return { onAdded, onOpenChange };
}

describe("AddMemoryDialog", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("closes on a write that committed with only lists and search behind", async () => {
    vi.mocked(setMemory).mockRejectedValue(
      new ApiError({ status: 503, code: "PUBLICATION_PENDING", detail: { retryable: false } }),
    );
    const { onAdded, onOpenChange } = await mount();
    await waitFor(() => expect(setMemory).toHaveBeenCalled());
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    expect(onAdded).toHaveBeenCalledTimes(1);
    expect(toastCommitted).toHaveBeenCalledTimes(1);
    expect(toast).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("stays open with the reason on a failure", async () => {
    vi.mocked(setMemory).mockRejectedValue(new ApiError({ status: 409, message: "the key is locked" }));
    const { onAdded, onOpenChange } = await mount();
    expect(await screen.findByText(/the key is locked/)).toBeInTheDocument();
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
    expect(onAdded).not.toHaveBeenCalled();
  });
});
