// Real dashboard gestures against the disposable Docker server. No route mocks.
import { chromium, expect } from "@playwright/test";

const [base, action, model, dimensions, key] = process.argv.slice(2);
const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  page.setDefaultTimeout(30_000);
  await page.goto(`${base}/settings`);
  await page.getByRole("tab", { name: "Models & keys", exact: true }).click();
  let endpoint;
  let button;
  if (action === "reset") {
    await page.getByRole("button", { name: "Use environment configuration", exact: true }).click();
    endpoint = "/v1/admin/models/reset";
    button = page.getByRole("button", { name: "Confirm environment configuration", exact: true });
  } else if (action === "retry") {
    endpoint = "/v1/admin/models/retry";
    button = page.getByRole("button", { name: "Retry rebuild", exact: true });
  } else {
    await expect(page.getByRole("heading", { name: "Models & keys", exact: true })).toBeVisible({ timeout: 30_000 });
    await page.getByRole("combobox", { name: "LLM provider", exact: true }).selectOption("openai");
    await page.getByLabel("LLM model", { exact: true }).fill("gpt-6-luna");
    await page.getByRole("combobox", { name: "Embedding provider", exact: true }).selectOption("compatible");
    await page.getByLabel("Embedding model", { exact: true }).fill(model);
    await page.getByLabel("Embedding API base URL", { exact: true }).fill("http://provider-smoke:8080/v1");
    if (key) {
      await page.getByLabel("Custom embedding provider API key", { exact: true }).fill(key);
      await page.getByLabel("OpenAI API key", { exact: true }).fill("docker-smoke-llm-key");
    }
    await page.getByText("Advanced models per job", { exact: true }).click();
    await page.getByLabel("Embedding dimensions", { exact: true }).fill(dimensions);
    endpoint = "/v1/admin/models";
    button = page.getByRole("button", { name: "Test & apply", exact: true });
  }
  const [response] = await Promise.all([
    page.waitForResponse(
      (response) => new URL(response.url()).pathname === endpoint && response.request().method() === "POST",
      { timeout: 60_000 },
    ),
    button.click(),
  ]);
  if (!response.ok()) throw new Error(`Dashboard action failed: HTTP ${response.status()}`);
  console.log(JSON.stringify(await response.json()));
} finally {
  await browser.close();
}
