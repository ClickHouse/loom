import { expect, test, type Page } from "@playwright/test";
import { installApi } from "./fixtures";

// Nothing on screen sits over content it hides: a drawer's last section scrolls clear of
// its actions, and a bar that stays at the top while a list scrolls is opaque, so the
// rows passing under it never show through.

async function memories(page: Page) {
  await installApi(page, { memoryRows: 30, knowledgeRows: 30 });
  await page.goto("/memories");
  await expect(page.locator("[data-memory-key]").first()).toBeVisible();
}

for (const width of [1400, 375]) {
  test.describe(`${width} px`, () => {
    test.use({ viewport: { width, height: 800 } });

    test("the memory drawer's last section scrolls clear of its actions", async ({ page }) => {
      await memories(page);
      await page.locator("[data-memory-key] [data-memory-open]").nth(3).click();
      const footer = page.locator("footer").filter({ has: page.getByRole("button", { name: "Delete" }) });
      await expect(footer).toBeVisible();
      const body = footer.locator("xpath=preceding-sibling::div[1]");
      await body.evaluate((el) => {
        el.scrollTop = el.scrollHeight;
      });
      const last = body.locator(":scope > *").last();
      const [lastBox, footerBox] = [await last.boundingBox(), await footer.boundingBox()];
      expect(lastBox && footerBox && lastBox.y + lastBox.height <= footerBox.y + 1).toBe(true);
    });

    for (const path of ["/memories", "/library"]) {
      test(`${path}: the search bar stays opaque over the rows that scroll under it`, async ({ page }) => {
        await memories(page);
        if (path !== "/memories") await page.goto(path);
        const bar = page.getByLabel("Search and filters");
        await expect(bar).toBeVisible();
        await page.locator("main").evaluate((m) => m.scrollTo(0, 900));
        const box = await bar.boundingBox();
        expect(box && box.y < 120).toBe(true);
        const bg = await bar.evaluate((el) => getComputedStyle(el).backgroundColor);
        expect(bg).not.toMatch(/rgba\(0, 0, 0, 0\)|transparent/);
        // A selection made down the list shows in the same bar, not over the rows.
        await page.getByRole("checkbox").nth(8).click();
        await expect(bar.getByText(/selected/)).toBeVisible();
      });
    }
  });
}
