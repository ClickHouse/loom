import { act, cleanup, render, screen, waitFor } from "../../test/rtl";
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
    await userEvent.click(screen.getByRole("button", { name: "Retry failures" }));
    await waitFor(() =>
      expect(api.retryProjectionFailures).toHaveBeenCalledWith(expect.objectContaining({ namespace: "team" })),
    );
    await userEvent.click(screen.getByRole("button", { name: "Dismiss failure f1" }));
    await waitFor(() => expect(api.resolveProjectionFailures).toHaveBeenCalledWith("acme", ["f1"]));
  });

  it("says when there is nothing to retry", async () => {
    vi.mocked(api.listProjectionFailures).mockResolvedValueOnce({ total: 0, rows: [] });
    render(<ProjectionFailuresPanel />);
    expect(await screen.findByText("No projection failures")).toBeInTheDocument();
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
