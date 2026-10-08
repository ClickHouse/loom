import { expect, test } from "@playwright/test";
import { installApi } from "./fixtures";

test("local Settings navigation reaches the provider form and submits an explicit embedding choice", async ({
  page,
}) => {
  await installApi(page);
  await page.addInitScript(() => {
    Object.assign(window, { __LOOM_AUTH_DISABLED: true, __LOOM_DEFAULT_NAMESPACE: "bench" });
  });
  await page.route("**/v1/admin/models", async (route) => {
    if (route.request().method() === "POST") {
      await route.fulfill({ json: { config_id: "fixture-change", state: "queued" } });
      return;
    }
    await route.fulfill({
      json: {
        version: "0",
        source: "environment",
        llm_provider: "fake",
        llm_model: "fake-echo",
        embedding_provider: "fake",
        embedding_model: "fake-ngram-3072",
        embedding_dimensions: 3072,
        configured_keys: {},
        requires_setup: true,
        llm_overrides: {},
        verification_models: null,
        pending: null,
      },
    });
  });
  await page.route("**/v1/admin/models/validate", (route) =>
    route.fulfill({
      json: { ready: true, checks: [] },
    }),
  );
  await page.goto("/me/settings");
  await page.getByRole("tab", { name: "Models & keys", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Models & keys", exact: true })).toBeVisible();
  await page.getByRole("combobox", { name: "LLM provider", exact: true }).selectOption("openai");
  await page.getByLabel("LLM model", { exact: true }).fill("gpt-6-luna");
  await page.getByRole("combobox", { name: "Embedding provider", exact: true }).selectOption("compatible");
  await page.getByLabel("Embedding model", { exact: true }).fill("fixture-model");
  await page.getByLabel("Embedding API base URL", { exact: true }).fill("http://provider-smoke:8080/v1");
  await page.getByLabel("Custom embedding provider API key", { exact: true }).fill("fixture-embedding-key");
  await page.getByLabel("OpenAI API key", { exact: true }).fill("fixture-llm-key");
  await page.getByText("Advanced models per job", { exact: true }).click();
  await page.getByLabel("Embedding dimensions", { exact: true }).fill("8");
  const [save] = await Promise.all([
    page.waitForRequest(
      (request) => new URL(request.url()).pathname === "/v1/admin/models" && request.method() === "POST",
    ),
    page.getByRole("button", { name: "Test & apply", exact: true }).click(),
  ]);
  expect(save.postDataJSON()).toMatchObject({
    expected_version: "0",
    llm: { provider: "openai", model: "gpt-6-luna" },
    embedding: {
      provider: "compatible",
      model: "fixture-model",
      dimensions: 8,
      base_url: "http://provider-smoke:8080/v1",
    },
    keys: { compatible: "fixture-embedding-key", openai: "fixture-llm-key" },
  });
});

test("environment reset requires confirmation and submits the current configuration version", async ({ page }) => {
  await installApi(page);
  await page.addInitScript(() => {
    Object.assign(window, { __LOOM_AUTH_DISABLED: true, __LOOM_DEFAULT_NAMESPACE: "bench" });
  });
  await page.route("**/v1/admin/models", (route) =>
    route.fulfill({
      json: {
        version: "5",
        source: "dashboard",
        llm_provider: "openai",
        llm_model: "gpt-6-luna",
        embedding_provider: "compatible",
        embedding_model: "fixture-model",
        embedding_dimensions: 8,
        embedding_base_url: "http://provider-smoke:8080/v1",
        configured_keys: { openai: true, compatible: true },
        requires_setup: false,
        llm_overrides: {},
        verification_models: null,
        pending: null,
      },
    }),
  );
  await page.route("**/v1/admin/models/reset", (route) =>
    route.fulfill({ json: { config_id: "fixture-reset", state: "queued" } }),
  );
  await page.goto("/me/settings?tab=models");
  await page.getByRole("button", { name: "Use environment configuration", exact: true }).click();
  await expect(page.getByText(/This does not reload a changed .env file/)).toBeVisible();
  const [reset] = await Promise.all([
    page.waitForRequest(
      (request) => new URL(request.url()).pathname === "/v1/admin/models/reset" && request.method() === "POST",
    ),
    page.getByRole("button", { name: "Confirm environment configuration", exact: true }).click(),
  ]);
  expect(reset.postDataJSON()).toEqual({ expected_version: "5" });
});
