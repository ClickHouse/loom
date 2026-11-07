import { cleanup, render, screen, waitFor } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../api/maintenance";
import { useStore } from "../../lib/state";
import { ProjectionFailuresPanel } from "./ProjectionFailures";

vi.mock("../../components/Toast", () => ({ toast: vi.fn(), toastError: vi.fn(), ToastViewport: () => null }));
vi.mock("../../api/maintenance", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/maintenance")>();
  return {
    ...actual,
    listProjectionFailures: vi.fn(async () => ({
      total: 1,
      rows: [
        {
          failure_id: "f1",
          namespace: "team",
          projection: "embedding",
          source_operation: "memory.set",
          memory_keys: ["deploy_window"],
          error_type: "Timeout",
          error_message: "embedder timed out",
          created_at: null,
        },
      ],
    })),
    retryProjectionFailures: vi.fn(async () => ({ retried: 1, resolved: 1, abandoned: 0 })),
    resolveProjectionFailures: vi.fn(async () => ({ resolved: 1 })),
  };
});

beforeEach(() => useStore.setState({ identity: { org: "acme", namespace: "team", agent: "loom-ui" } }));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ProjectionFailuresPanel", () => {
  it("retries the namespace's failures and dismisses one", async () => {
    render(<ProjectionFailuresPanel />);
    expect(await screen.findByText("embedder timed out")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Retry all" }));
    await waitFor(() =>
      expect(api.retryProjectionFailures).toHaveBeenCalledWith(expect.objectContaining({ namespace: "team" })),
    );
    await userEvent.click(screen.getByRole("button", { name: "Dismiss failure f1" }));
    await waitFor(() => expect(api.resolveProjectionFailures).toHaveBeenCalledWith("acme", ["f1"]));
  });
});
