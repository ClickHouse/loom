// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ModelStatus } from "../api/models";
import { initialModelConfiguration, ModelsForm } from "./ModelsForm";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
const status: ModelStatus = {
  version: "1791195706575122049",
  source: "environment",
  llm_provider: "fake",
  llm_model: "fake-echo-1",
  embedding_provider: "fake",
  embedding_model: "fake-ngram-3072",
  embedding_dimensions: 3072,
  configured_keys: {},
  requires_setup: true,
  llm_overrides: {},
  verification_models: null,
};

// Click UI's Select: the label names the trigger; the options open in a popover dialog.
function choose(label: string, option: string) {
  fireEvent.click(screen.getByLabelText(label));
  fireEvent.click(within(screen.getByRole("dialog")).getByText(option));
}

describe("Models & keys", () => {
  it("submits a compatible endpoint with its own embedding key", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(
      <ModelsForm
        status={status}
        onValidate={vi.fn().mockResolvedValue({ ready: true, checks: [] })}
        onSave={onSave}
      />,
    );
    choose("Embedding provider", "Other (OpenAI-compatible API)");
    fireEvent.change(screen.getByLabelText("Embedding model"), { target: { value: "vendor/custom-embedding" } });
    fireEvent.change(screen.getByLabelText("Embedding API base URL"), {
      target: { value: "https://embedding.example/v1" },
    });
    fireEvent.change(screen.getByLabelText("Custom embedding provider API key"), {
      target: { value: "custom-test-key" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Test & apply" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledOnce());
    expect(onSave.mock.calls[0][0].embedding).toMatchObject({
      provider: "compatible",
      base_url: "https://embedding.example/v1",
      model: "vendor/custom-embedding",
    });
    expect(onSave.mock.calls[0][0].keys).toEqual({ compatible: "custom-test-key" });
  });

  it("keeps browsers and password managers from filling the key fields", () => {
    render(<ModelsForm status={status} onValidate={vi.fn()} onSave={vi.fn()} />);
    choose("Embedding provider", "Other (OpenAI-compatible API)");
    for (const label of [
      "OpenAI API key",
      "Anthropic API key",
      "Google Gemini API key",
      "Custom embedding provider API key",
    ]) {
      const input = screen.getByLabelText(label);
      expect(input.getAttribute("autocomplete")).toBe("new-password");
      expect(input.hasAttribute("data-1p-ignore")).toBe(true);
      expect(input.getAttribute("data-lpignore")).toBe("true");
    }
  });

  it("locks the embedding fields with the server's reason when storage cannot rebuild", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const blocked: ModelStatus = {
      ...status,
      embedding_provider: "openai",
      embedding_model: "text-embedding-3-large",
      configured_keys: { openai: true },
      requires_setup: false,
      embedding_change_blocked: "loom.memory_embeddings uses SharedReplacingMergeTree.",
    };
    render(
      <ModelsForm
        status={blocked}
        onValidate={vi.fn().mockResolvedValue({ ready: true, checks: [] })}
        onSave={onSave}
      />,
    );
    expect(screen.getByText("loom.memory_embeddings uses SharedReplacingMergeTree.")).toBeTruthy();
    expect((screen.getByLabelText("Embedding provider") as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByLabelText("Embedding model") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText("Embedding dimensions") as HTMLInputElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("LLM model"), { target: { value: "gpt-6-luna-mini" } });
    fireEvent.click(screen.getByRole("button", { name: "Test & apply" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledOnce());
    expect(onSave.mock.calls[0][0].embedding).toMatchObject({ provider: "openai", model: "text-embedding-3-large" });
  });

  it("offers the hosted embedding providers and the local one, never the fake one", () => {
    render(<ModelsForm status={status} onValidate={vi.fn()} onSave={vi.fn()} />);
    fireEvent.click(screen.getByLabelText("Embedding provider"));
    const options = screen.getByRole("dialog").querySelectorAll("[data-value]");
    expect(Array.from(options, (option) => option.getAttribute("data-value"))).toEqual([
      "openai",
      "gemini",
      "compatible",
      "local",
    ]);
  });

  it("keeps the local embedder a server runs and asks for no key for it", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const local = { ...status, embedding_provider: "local", embedding_model: "local", embedding_dimensions: 384 };
    expect(initialModelConfiguration(local).embedding).toEqual({ provider: "local", model: "local", dimensions: 384 });
    render(
      <ModelsForm status={local} onValidate={vi.fn().mockResolvedValue({ ready: true, checks: [] })} onSave={onSave} />,
    );
    choose("LLM provider", "Anthropic (Claude)");
    choose("Embedding model", "bge-base, 768 dimensions");
    expect(screen.queryByLabelText("Embedding dimensions")).toBeNull();
    fireEvent.change(screen.getByLabelText("Anthropic API key"), { target: { value: "llm-test-key" } });
    fireEvent.click(screen.getByRole("button", { name: "Test & apply" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledOnce());
    expect(onSave.mock.calls[0][0].embedding).toEqual({ provider: "local", model: "local:bge-base", dimensions: 768 });
    expect(onSave.mock.calls[0][0].keys).toEqual({ anthropic: "llm-test-key" });
  });

  it("keeps configured models even when a key needs attention", () => {
    const config = initialModelConfiguration({
      ...status,
      llm_provider: "anthropic",
      llm_model: "claude-custom",
      embedding_provider: "gemini",
      embedding_model: "gemini-custom",
      embedding_dimensions: 768,
    });
    expect(config.llm.model).toBe("claude-custom");
    expect(config.embedding).toEqual({ provider: "gemini", model: "gemini-custom", dimensions: 768 });
    expect(config.expected_version).toBe(status.version);
  });

  it("does not save failed validation and rechecks changed inputs", async () => {
    const onSave = vi.fn();
    const onValidate = vi.fn().mockResolvedValue({ ready: false, checks: [] });
    render(<ModelsForm status={status} onValidate={onValidate} onSave={onSave} />);
    const save = screen.getByRole("button", { name: "Test & apply" }) as HTMLButtonElement;
    fireEvent.click(save);
    await waitFor(() => expect(save.disabled).toBe(false));
    expect(onSave).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("LLM model"), { target: { value: "gpt-other" } });
    onValidate.mockResolvedValue({ ready: true, checks: [] });
    fireEvent.click(save);
    await waitFor(() => expect(onSave).toHaveBeenCalledOnce());
    expect(onValidate).toHaveBeenCalledTimes(2);
    expect(onSave.mock.calls[0][0].llm.model).toBe("gpt-other");
  });

  it("sends keys only in the submission and clears them after save", async () => {
    const storageWrite = vi.fn();
    vi.stubGlobal("localStorage", { setItem: storageWrite });
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(
      <ModelsForm
        status={status}
        onValidate={vi.fn().mockResolvedValue({ ready: true, checks: [] })}
        onSave={onSave}
      />,
    );
    const key = screen.getByLabelText("OpenAI API key") as HTMLInputElement;
    expect(key.type).toBe("password");
    fireEvent.change(key, { target: { value: "test-secret-value" } });
    fireEvent.click(screen.getByRole("button", { name: "Test & apply" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledOnce());
    expect(onSave.mock.calls[0][0].keys).toEqual({ openai: "test-secret-value" });
    await waitFor(() => expect(key.value).toBe(""));
    expect(storageWrite).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("does not show raw exceptions that could include credentials", async () => {
    render(
      <ModelsForm
        status={status}
        onValidate={vi.fn().mockRejectedValue(new Error("test-secret-value"))}
        onSave={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Test & apply" }));
    await screen.findByText(/Configuration was not applied/);
    expect(screen.queryByText(/test-secret-value/)).toBeNull();
  });

  it("shows a compatible endpoint's dimensions beside its URL, not under Advanced", () => {
    render(<ModelsForm status={status} onValidate={vi.fn()} onSave={vi.fn()} />);
    choose("Embedding provider", "Other (OpenAI-compatible API)");
    const field = screen.getByLabelText("Embedding dimensions");
    expect(field.closest("details")).toBeNull();
    expect(screen.getAllByLabelText("Embedding dimensions")).toHaveLength(1);
  });

  it("shows the check's detail next to its message", async () => {
    const onValidate = vi.fn().mockResolvedValue({
      ready: false,
      checks: [
        {
          role: "llm",
          provider: "openai",
          model: "gpt-test",
          ready: false,
          code: "quota_or_rate_limit",
          detail: "HTTP 402 (insufficient_quota)",
        },
      ],
    });
    render(<ModelsForm status={status} onValidate={onValidate} onSave={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Test & apply" }));
    await screen.findByText(/Check provider billing or quota, then try again\. \(HTTP 402 \(insufficient_quota\)\)/);
  });
});
