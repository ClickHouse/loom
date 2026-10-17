import { expect, test } from "@playwright/test";
import { installApi, ROUTES } from "./fixtures";

// The shell scrolls only its content: the header, sidebar and phone nav stay put, so the
// document itself never scrolls, on any route, at either width.
for (const width of [1400, 375]) {
  test.describe(`${width} px`, () => {
    test.use({ viewport: { width, height: 900 } });
    for (const path of ROUTES) {
      test(`${path} scrolls only its content`, async ({ page }) => {
        await installApi(page, { memoryRows: 8, knowledgeRows: 4, runRows: 6 });
        await page.goto(path);
        await expect(page.locator("main h1").first()).toBeVisible();
        await page.waitForLoadState("networkidle");
        const doc = await page.evaluate(() => ({
          height: document.documentElement.scrollHeight - window.innerHeight,
          width: document.documentElement.scrollWidth - window.innerWidth,
        }));
        expect(doc).toEqual({ height: 0, width: 0 });
      });
    }
  });
}
