import { expect, test } from "@playwright/test";
import { violations } from "./axe";
import { installApi, NAMESPACE } from "./fixtures";

for (const width of [1400, 375]) {
  test(`budget settings save, cancel and remain accessible at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await installApi(page);
    const info: Record<string, unknown> = {
      found: true,
      namespace: NAMESPACE,
      owner_email: "e2e@example.com",
      description: "",
      tags: [],
      active: true,
      visibility: "private",
      classification: "standard",
      kind: "memory",
      consolidation_enabled: false,
      conflict_threshold: null,
      daily_cost_cap_usd: 5,
      daily_user_cost_cap_usd: 2,
      budget_alert_inbox: "warnings",
      created_at: "",
      updated_at: "",
    };
    const writes: Record<string, unknown>[] = [];
    await page.route("**/v1/namespace.get", (route) => route.fulfill({ json: info }));
    await page.route("**/v1/namespace.register", async (route) => {
      const body = route.request().postDataJSON();
      writes.push(body);
      Object.assign(info, body);
      await route.fulfill({ json: { registered: true, namespace: NAMESPACE, updated_at: "" } });
    });
    await page.goto("/spaces");
    await page.getByRole("button", { name: `Actions for ${NAMESPACE}` }).click();
    await page.getByRole("menuitem", { name: "Settings", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: `Settings · ${NAMESPACE}` });
    await expect(dialog).toBeVisible();
    const cap = dialog.getByLabel("Daily cap per user (USD)");
    await cap.fill("9");
    await cap.press("Escape");
    await expect(dialog).toBeVisible();
    await expect(cap).toHaveValue("2");
    const inbox = dialog.getByLabel("Budget warning inbox");
    await inbox.fill("other-inbox");
    await inbox.press("Escape");
    await expect(dialog).toBeVisible();
    await expect(inbox).toHaveValue("warnings");
    expect(writes).toHaveLength(0);
    await cap.fill("3.5");
    await cap.press("Tab");
    await expect.poll(() => writes.at(-1)?.daily_user_cost_cap_usd).toBe(3.5);
    await expect(cap).toBeEnabled();
    expect(await violations(page)).toEqual([]);
    await cap.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath("budget-settings.png"), fullPage: true });
    await inbox.fill("");
    await inbox.press("Tab");
    await expect.poll(() => writes.at(-1)?.budget_alert_inbox).toBe("");
  });
}
