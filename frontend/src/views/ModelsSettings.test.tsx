// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  getModelStatus,
  resetModelConfiguration,
  retryModelConfiguration,
  saveModelConfiguration,
  validateModelConfiguration,
  type ModelStatus,
} from "../api/models";
import { ModelsSettings } from "./ModelsSettings";
import { ApiError } from "../lib/errors";

vi.mock("../api/models", async (original) => ({
  ...(await original<typeof import("../api/models")>()),
  getModelStatus: vi.fn(),
  resetModelConfiguration: vi.fn(),
  retryModelConfiguration: vi.fn(),
  saveModelConfiguration: vi.fn(),
  validateModelConfiguration: vi.fn(),
}));

const status: ModelStatus = {
  version: "1791195706575122049",
  source: "dashboard",
  llm_provider: "openai",
  llm_model: "gpt-test",
  embedding_provider: "openai",
  embedding_model: "text-embedding-test",
  embedding_dimensions: 3072,
  configured_keys: { openai: true },
  requires_setup: false,
  llm_overrides: {},
  verification_models: null,
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getModelStatus).mockResolvedValue(status);
});
afterEach(cleanup);

describe("Environment configuration reset", () => {
  it("confirms intent and sends the exact displayed revision", async () => {
    vi.mocked(resetModelConfiguration).mockImplementation(async () => {
      vi.mocked(getModelStatus).mockResolvedValue({ ...status, source: "environment", version: "1791195706575122050" });
      return { config_id: "reset-id", state: "active" };
    });
    render(<ModelsSettings />);
    fireEvent.click(await screen.findByRole("button", { name: "Use environment configuration" }));
    expect(resetModelConfiguration).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Confirm environment configuration" }));
    await waitFor(() => expect(resetModelConfiguration).toHaveBeenCalledWith(status.version));
    await screen.findByText(/server environment/);
    expect(screen.queryByRole("button", { name: "Use environment configuration" })).toBeNull();
  });

  it("does not offer reset during an incomplete embedding rebuild", async () => {
    vi.mocked(getModelStatus).mockResolvedValue({
      ...status,
      pending: { config_id: "pending", state: "rebuilding", error_code: "", progress: {} },
    });
    render(<ModelsSettings />);
    await screen.findByText(/Embedding configuration: rebuilding/);
    expect(screen.queryByRole("button", { name: "Use environment configuration" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry rebuild" })).toBeNull();
  });

  it("describes rebuild progress in plain words and keeps raw fields under details", async () => {
    vi.mocked(getModelStatus).mockResolvedValue({
      ...status,
      pending: {
        config_id: "failed-job",
        state: "failed",
        error_code: "rebuild_cleanup_failed",
        progress: { phase: "cleanup", table_index: 16, table_count: 16, rows_done: 33, cursor: 0 },
      },
    });
    render(<ModelsSettings />);
    await screen.findByText(/Step 6 of 7: Removing temporary copies, 33 rows processed\./);
    expect(screen.getByText(/The rebuild stopped at “removing temporary copies”/)).toBeTruthy();
    expect(screen.getByText("error: rebuild_cleanup_failed")).toBeTruthy();
  });

  it("retries the displayed failed job and refreshes its checkpoint status", async () => {
    const pending = {
      config_id: "failed-job",
      state: "failed",
      error_code: "rebuild_rebuild_failed",
      progress: { cursor: 42 },
    };
    vi.mocked(getModelStatus).mockResolvedValue({ ...status, pending });
    vi.mocked(retryModelConfiguration).mockImplementation(async (config_id) => {
      vi.mocked(getModelStatus).mockResolvedValue({
        ...status,
        pending: { ...pending, state: "queued", error_code: "" },
      });
      return { config_id, state: "queued" };
    });
    render(<ModelsSettings />);
    fireEvent.click(await screen.findByRole("button", { name: "Retry rebuild" }));
    await waitFor(() => expect(retryModelConfiguration).toHaveBeenCalledWith("failed-job"));
    await screen.findByText(/Embedding configuration: queued/);
    expect(screen.getByText("cursor: 42")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry rebuild" })).toBeNull();
  });
});

describe("Saving", () => {
  it("keeps a saved confirmation after the form reloads on the new version", async () => {
    vi.mocked(validateModelConfiguration).mockResolvedValue({ ready: true, checks: [] });
    vi.mocked(saveModelConfiguration).mockImplementation(async () => {
      vi.mocked(getModelStatus).mockResolvedValue({ ...status, version: "1791195706575122050" });
      return { config_id: "saved-id", state: "active" };
    });
    render(<ModelsSettings />);
    fireEvent.click(await screen.findByRole("button", { name: "Test & apply" }));
    await screen.findByText("Saved. Loom is using the new configuration.");
  });
});

describe("Failed reads and writes", () => {
  it("says why the settings could not load instead of blaming the role", async () => {
    vi.mocked(getModelStatus).mockRejectedValue(
      new ApiError({
        status: 503,
        code: "BACKEND_UNAVAILABLE",
        message: "ClickHouse did not answer",
        detail: { category: "dependency", retryable: true },
      }),
    );
    render(<ModelsSettings />);
    expect(
      await screen.findByText("Couldn't load model settings: ClickHouse did not answer. Try again in a moment."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Platform administrator access is required/)).toBeNull();
  });

  it("says why a reset was refused", async () => {
    vi.mocked(resetModelConfiguration).mockRejectedValue(
      new ApiError({
        status: 503,
        code: "MODEL_CHANGE_IN_PROGRESS",
        message: "another model change is being applied",
        detail: { category: "capacity", retryable: true, retry_after_s: 5 },
      }),
    );
    render(<ModelsSettings />);
    fireEvent.click(await screen.findByRole("button", { name: "Use environment configuration" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm environment configuration" }));
    expect(
      await screen.findByText(
        "Couldn't reset model settings: another model change is being applied. Try again in 5 s.",
      ),
    ).toBeInTheDocument();
  });
});

describe("A reset whose connection dropped", () => {
  it("keeps the warning that the reset may have gone through", async () => {
    vi.mocked(resetModelConfiguration).mockRejectedValue(new TypeError("Failed to fetch"));
    render(<ModelsSettings />);
    fireEvent.click(await screen.findByRole("button", { name: "Use environment configuration" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm environment configuration" }));
    expect(await screen.findByText(/Couldn't reset model settings: .*may still have committed/)).toBeInTheDocument();
  });
});
