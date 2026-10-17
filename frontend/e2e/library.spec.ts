import { expect, test, type Page } from "@playwright/test";
import { installApi } from "./fixtures";

// The Library's cards and rows: a phone-width card keeps its parts inside it, and the row
// actions keep their space before a hover and show on a touch screen.

async function open(page: Page, path: string) {
  await installApi(page, { memoryRows: 8, knowledgeRows: 4, runRows: 6, profileRows: 3 });
  await page.goto(path);
  await expect(page.locator("main h1").first()).toBeVisible();
  await page.waitForLoadState("networkidle");
}

test("library row actions keep their space before a hover, and a touch screen shows them", async ({ browser }) => {
  for (const touch of [false, true]) {
    const context = await browser.newContext({
      hasTouch: touch,
      isMobile: touch,
      viewport: { width: 1400, height: 900 },
    });
    const page = await context.newPage();
    await open(page, "/library");
    const row = page.locator("main [data-doc-row]").first();
    const preview = row.getByRole("button", { name: /^Preview / });
    const before = (await preview.boundingBox())?.width;
    if (touch) {
      await expect(row.getByRole("button", { name: "Delete document" })).toBeVisible();
    } else {
      await row.hover();
      await expect(row.getByRole("button", { name: "Delete document" })).toBeVisible();
      expect((await preview.boundingBox())?.width).toBe(before);
    }
    await context.close();
  }
});

test.describe("375 px", () => {
  test.use({ viewport: { width: 375, height: 812 } });

  test("a library card keeps its metadata and tags inside the card", async ({ page }) => {
    await open(page, "/library");
    const cards = page.locator("main [data-doc-row]");
    await expect(cards.first()).toBeVisible();
    const outside = await page.evaluate(() =>
      [...document.querySelectorAll("main [data-doc-row]")].flatMap((card) => {
        const right = card.getBoundingClientRect().right;
        return [...card.querySelectorAll("button, span")]
          .filter((e) => e.getBoundingClientRect().right > right + 0.5)
          .map((e) => e.textContent);
      }),
    );
    expect(outside).toEqual([]);
  });
});
