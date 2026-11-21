import { expect, test } from "@playwright/test";
import { installApi } from "./fixtures";

test("disconnecting while drafting is disabled returns focus to the disclosure", async ({ page }) => {
  await installApi(page, { knowledgeRows: 2 });
  let connected = true;
  await page.route("**/v1/knowledge.query_history.list", (route) =>
    route.fulfill({
      json: {
        enabled: false,
        sources: connected
          ? [{ name: "metrics", source_id: "source", host: "db.example", port: 8443, databases: ["analytics"] }]
          : [],
      },
    }),
  );
  await page.route("**/v1/knowledge.query_history.remove", async (route) => {
    connected = false;
    await route.fulfill({ json: { removed: true } });
  });
  await page.goto("/library");
  const disclosure = page.getByRole("button", { name: "Query history", exact: true });
  await disclosure.click();
  await expect(page.getByRole("button", { name: "Connect query history", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Actions for metrics" }).click();
  await page.getByRole("menuitem", { name: "Disconnect", exact: true }).click();
  await expect(page.getByText("No query-history sources", { exact: true })).toBeVisible();
  await expect(disclosure).toBeFocused();
});

for (const width of [1400, 375]) {
  test(`an admin queues a gap draft and connects query history at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await installApi(page, { knowledgeRows: 2 });
    const stamp = new Date().toISOString();
    const source = {
      name: "W".repeat(100),
      source_id: "source",
      kind: "clickhouse_queries",
      host: "db.example",
      port: 8443,
      secure: true,
      user: "reader",
      databases: ["analytics"],
      cluster: "",
      created_at: stamp,
    };
    let connected = false;
    let draftCalls = 0;
    await page.route("**/v1/knowledge.gaps.list", (route) =>
      route.fulfill({
        json: {
          enabled: true,
          capturing: true,
          built_at: stamp,
          gaps: [{ gap_id: "gap", head: "How is revenue measured?", samples: [], askers: 3, asks: 5, last_at: stamp }],
        },
      }),
    );
    await page.route("**/v1/knowledge.gaps.draft", async (route) => {
      expect(route.request().postDataJSON()).toMatchObject({ gap_id: "gap" });
      draftCalls++;
      await route.fulfill({ json: { candidate_id: "candidate" } });
    });
    await page.route("**/v1/knowledge.query_history.list", (route) =>
      route.fulfill({ json: { enabled: true, sources: connected ? [source] : [] } }),
    );
    await page.route("**/v1/knowledge.query_history.add", async (route) => {
      expect(route.request().postDataJSON()).toMatchObject({
        name: source.name,
        databases: ["analytics"],
        password: "test-credential",
        secure: true,
      });
      connected = true;
      await route.fulfill({ json: source });
    });
    await page.route("**/v1/knowledge.query_history.remove", async (route) => {
      expect(route.request().postDataJSON()).toMatchObject({ name: source.name });
      connected = false;
      await route.fulfill({ json: { removed: true } });
    });
    await page.goto("/library");
    await page.getByRole("button", { name: "Draft definition for How is revenue measured?" }).click();
    await expect(page.getByRole("button", { name: "Draft definition for How is revenue measured?" })).toBeDisabled();
    await expect(page.getByRole("status").filter({ hasText: "Review the draft" })).toBeVisible();
    expect(draftCalls).toBe(1);
    await page.getByRole("button", { name: "Query history", exact: true }).click();
    const connect = page.getByRole("button", { name: "Connect query history", exact: true });
    await connect.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByLabel("Source name", { exact: true })).toBeFocused();
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(connect).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page.getByLabel("Source name", { exact: true })).toBeFocused();
    for (const [label, value] of [
      ["Source name", source.name],
      ["ClickHouse host", "db.example"],
      ["Read-only username", "reader"],
      ["Password", "test-credential"],
      ["Databases (comma separated)", "analytics"],
    ]) {
      await page.getByLabel(label, { exact: true }).fill(value);
    }
    await expect(page.getByLabel("Password", { exact: true })).toHaveAttribute("type", "password");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: `/tmp/gaps-query-history-${width}.png`, fullPage: true });
    await page.getByRole("button", { name: "Connect", exact: true }).click();
    await expect(page.getByLabel("Password", { exact: true })).toHaveCount(0);
    await expect(connect).toBeFocused();
    const name = page.getByText(source.name, { exact: true });
    await expect(name).toBeVisible();
    expect(await name.evaluate((element) => element.getBoundingClientRect().right <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: `/tmp/gaps-query-history-name-${width}.png`, fullPage: true });
    await page.getByRole("button", { name: `Actions for ${source.name}` }).click();
    await page.getByRole("menuitem", { name: "Disconnect", exact: true }).click();
    await expect(page.getByText("No query-history sources", { exact: true })).toBeVisible();
    await expect(connect).toBeFocused();
  });
}
