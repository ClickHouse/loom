import { act, cleanup, render, screen, waitFor } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../api/maintenance";
import type { ProjectionRetry } from "../../api/maintenance";
import { toast } from "../../components/Toast";
import { useStore } from "../../lib/state";
import { ProjectionFailuresPanel } from "./ProjectionFailures";

vi.mock("../../components/Toast", () => ({ toast: vi.fn(), toastError: vi.fn(), ToastViewport: () => null }));
vi.mock("../../api/maintenance", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/maintenance")>();
  return {
    ...actual,
    listProjectionFailures: vi.fn(async () => ({
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
    retryProjectionFailures: vi.fn(async () => ({ results: [], retried: 0, resolved: 0, abandoned: 0 })),
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
    await userEvent.click(screen.getByRole("button", { name: "Retry failures" }));
    await waitFor(() =>
      expect(api.retryProjectionFailures).toHaveBeenCalledWith(expect.objectContaining({ namespace: "team" })),
    );
    await userEvent.click(screen.getByRole("button", { name: "Dismiss failure f1" }));
    await waitFor(() => expect(api.resolveProjectionFailures).toHaveBeenCalledWith("acme", ["f1"]));
  });

  it("counts what each retry came to: fixed, failed again, given up, waiting or no retry", async () => {
    const row = (over: Partial<ProjectionRetry>): ProjectionRetry => ({
      failure_id: "f",
      projection: "embedding",
      retried: false,
      resolved: false,
      abandoned: false,
      reason: "",
      error: "",
      ...over,
    });
    vi.mocked(api.retryProjectionFailures).mockResolvedValueOnce({
      results: [
        row({ retried: true, resolved: true }),
        row({ retried: true, error: "timeout" }),
        row({ retried: true, abandoned: true, reason: "malformed_payload" }),
        row({ reason: "model_calls_held" }),
        row({ reason: "pending" }),
        row({ reason: "unsupported_projection" }),
      ],
      retried: 3,
      resolved: 1,
      abandoned: 1,
    });
    render(<ProjectionFailuresPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Retry failures" }));
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(
        "Tried 6: 1 fixed, 1 failed again, 1 given up, 2 waiting for a later pass, 1 with no retry",
        "ok",
      ),
    );
  });

  it("says a retry held by the org's model calls is waiting, not lost", async () => {
    vi.mocked(api.retryProjectionFailures).mockResolvedValueOnce({
      results: [
        {
          failure_id: "f",
          projection: "embedding",
          retried: false,
          resolved: false,
          abandoned: false,
          reason: "model_calls_held",
          error: "",
        },
      ],
      retried: 0,
      resolved: 0,
      abandoned: 0,
    });
    render(<ProjectionFailuresPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Retry failures" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Tried 1: 1 waiting for a later pass", "info"));
  });

  it("says when there is nothing to retry", async () => {
    vi.mocked(api.listProjectionFailures).mockResolvedValueOnce({ rows: [] });
    render(<ProjectionFailuresPanel />);
    expect(await screen.findByText("No projection failures")).toBeInTheDocument();
    // The toolbar stays above the empty state.
    expect(screen.getByRole("button", { name: "Retry failures" })).toBeDisabled();
  });

  it("says a failed read failed, and Retry reads again", async () => {
    vi.mocked(api.listProjectionFailures).mockRejectedValueOnce(new Error("down"));
    render(<ProjectionFailuresPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Retry" }));
    expect(await screen.findByText("embedder timed out")).toBeInTheDocument();
    expect(api.listProjectionFailures).toHaveBeenCalledTimes(2);
  });

  it("drops the old namespace's failures on a switch", async () => {
    render(<ProjectionFailuresPanel />);
    expect(await screen.findByText("embedder timed out")).toBeInTheDocument();
    vi.mocked(api.listProjectionFailures).mockImplementationOnce(() => new Promise(() => {}));
    act(() => useStore.setState({ identity: { org: "acme", namespace: "other", agent: "loom-ui" } }));
    await waitFor(() => expect(screen.queryByText("embedder timed out")).toBeNull());
    expect(screen.queryByRole("button", { name: "Dismiss failure f1" })).toBeNull();
  });
});
