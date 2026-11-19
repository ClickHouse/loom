import { expect, test } from "@playwright/test";
import { boxOf, installApi, MIN_TARGET_PX } from "./fixtures";
import { violations } from "./axe";

for (const width of [1400, 375]) {
  test.describe(`${width} px connected accounts`, () => {
    test.use({ viewport: { width, height: 900 }, hasTouch: width === 375 });

    test("a viewer can review and disconnect their own account", async ({ page }) => {
      await installApi(page, { viewer: true });
      let connected = true;
      let disconnects = 0;
      await page.route("**/v1/connection.list", (route) =>
        route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            connections: ["gdrive", "notion", "linear"].map((kind) => ({
              kind,
              connected: kind === "gdrive" && connected,
              account_email: kind === "gdrive" && connected ? "reader@example.com" : null,
              scopes: ["scope-not-for-display"],
              connect_url: `/settings/connected-accounts?kind=${kind}`,
            })),
          }),
        }),
      );
      await page.route("**/v1/connection.disconnect", (route) => {
        disconnects++;
        connected = false;
        return route.fulfill({ status: 200, contentType: "application/json", body: '{"disconnected":true}' });
      });
      await page.goto("/settings/connected-accounts?kind=linear");
      await expect(page.getByText("reader@example.com")).toBeVisible();
      await expect(page.getByRole("heading", { name: "Linear", exact: true })).toBeFocused();
      await expect(page.getByText("scope-not-for-display")).toHaveCount(0);
      expect(await violations(page)).toEqual([]);
      const main = page.locator("main");
      expect(await main.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
      if (width === 375) {
        for (const button of await main.getByRole("button").all()) {
          const box = await boxOf(button);
          expect(box.width).toBeGreaterThanOrEqual(MIN_TARGET_PX);
          expect(box.height).toBeGreaterThanOrEqual(MIN_TARGET_PX);
        }
      }
      await page.getByRole("button", { name: "Disconnect Google Drive" }).click();
      const confirm = page.getByRole("dialog", { name: "Disconnect Google Drive?", exact: true });
      await expect(confirm.getByText("This stops your document sources that use this connection.")).toBeVisible();
      await expect(confirm.getByRole("button", { name: "Cancel", exact: true })).toBeFocused();
      await confirm.getByRole("button", { name: "Cancel", exact: true }).click();
      expect(disconnects).toBe(0);
      await page.getByRole("button", { name: "Disconnect Google Drive" }).click();
      await confirm.getByRole("button", { name: "Disconnect", exact: true }).click();
      await expect(page.getByRole("button", { name: "Connect Google Drive", exact: true })).toBeVisible();
      await expect(page.getByText("reader@example.com")).toHaveCount(0);
      await expect(page.getByRole("heading", { name: "Google Drive", exact: true })).toBeFocused();
      expect(disconnects).toBe(1);
    });
  });
}
