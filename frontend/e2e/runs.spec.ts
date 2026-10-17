import { expect, test, type Page } from "@playwright/test";
import { violations } from "./axe";
import { installApi } from "./fixtures";

// The Runs ledger: grouped by session every table keeps its column header, and a run's
// replay opens clean.

async function open(page: Page, path: string) {
  await installApi(page, { memoryRows: 8, knowledgeRows: 4, runRows: 6, profileRows: 3 });
  await page.goto(path);
  await expect(page.locator("main h1").first()).toBeVisible();
  await page.waitForLoadState("networkidle");
}

test("grouped runs label every column and draw the header over the first session only", async ({ page }) => {
  await open(page, "/runs");
  await page.getByRole("button", { name: "Group by session" }).click();
  const tables = page.locator("main table");
  await expect(tables.nth(1)).toBeVisible();
  const heights: number[] = [];
  for (const table of await tables.all()) {
    await expect(table.getByRole("columnheader", { name: "Goal" })).toBeAttached();
    heights.push((await table.locator("thead").boundingBox())?.height ?? 0);
  }
  expect(heights[0]).toBeGreaterThan(20);
  expect(heights.slice(1).every((h) => h <= 1)).toBe(true);
  expect(await violations(page)).toEqual([]);
});

test("a run's replay opens and has no WCAG A/AA violations", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await open(page, "/runs");
  await page.locator("main table tbody button").first().click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText("recalled keys")).toBeVisible();
  expect(errors).toEqual([]);
  expect(await violations(page)).toEqual([]);
});

test("on a phone the status and agent filters sit under the 48 px toolbar, nothing clipped", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 800 });
  await open(page, "/runs");
  const toolbar = page.getByRole("search", { name: "Search and filters" });
  expect((await toolbar.boundingBox())?.height).toBe(48);
  const statuses = page.getByLabel("Filter runs by status");
  await expect(statuses).toBeVisible();
  expect(await toolbar.getByLabel("Filter runs by status").count()).toBe(0);
  for (const control of [statuses, page.getByRole("button", { name: "Agent" })]) {
    const box = await control.boundingBox();
    expect(box && box.x >= 0 && box.x + box.width <= 375).toBe(true);
  }
  // Nothing in the row scrolls sideways, and neither does the page.
  const sideways = await statuses.evaluate((el) => {
    for (let n = el.parentElement; n && n !== document.body; n = n.parentElement) {
      if (n.scrollWidth > n.clientWidth + 1) return true;
    }
    return false;
  });
  expect(sideways).toBe(false);
});

test("the runs intro keeps its info icon on the sentence's first line", async ({ page }) => {
  await open(page, "/runs");
  const intro = page.getByText(/^One row per agent execution/);
  const icon = page.getByRole("button", { name: "About runs" });
  const [text, info] = [await intro.boundingBox(), await icon.boundingBox()];
  expect(text && info).toBeTruthy();
  expect(info!.y - text!.y).toBeLessThan(12);
});
