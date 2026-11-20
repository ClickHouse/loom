import { expect, test, type Page } from "@playwright/test";
import { installApi } from "./fixtures";

// A count or summary that changes as the reader types is announced from a status region
// that is mounted before the change, so a screen reader hears it: the Library's filter
// count, the memory search summary and the Namespaces in your org count.

async function open(page: Page, path: string) {
  await installApi(page, { memoryRows: 8, knowledgeRows: 4, runRows: 6, profileRows: 3 });
  await page.goto(path);
  await expect(page.locator("main h1").first()).toBeVisible();
  await page.waitForLoadState("networkidle");
}

test("the library's filter count is announced from a status region mounted before the search", async ({ page }) => {
  await open(page, "/library");
  // The toolbar's count is a live region mounted with the bar, so it is there before the
  // search and says the count when a search or filter moves it.
  const regions = await page.locator("main [role='status'], main [aria-live]").elementHandles();
  await page.getByRole("textbox", { name: "Search library" }).fill("tag:lane-0");
  await expect
    .poll(async () =>
      (await Promise.all(regions.map((r) => r.evaluate((e) => e.textContent ?? "")))).includes("1 match"),
    )
    .toBe(true);
});

// One live region says a search's result: the toolbar's count, mounted with the bar. A second
// region saying the same (a page's own summary) would read every search twice.
test("the memory search is announced once, from the toolbar's count, mounted before the search", async ({ page }) => {
  await open(page, "/memories");
  const regions = await page.locator("main [role='status'], main [aria-live]").elementHandles();
  await page.getByRole("textbox", { name: "Search memories" }).fill("lag");
  await expect
    .poll(async () => {
      const texts = await Promise.all(regions.map((r) => r.evaluate((e) => e.textContent ?? "")));
      return texts.filter((t) => /match|Searching/i.test(t)).length;
    })
    .toBe(1);
});

test("the Namespaces in your org count is said from one live region that stays mounted through a search", async ({
  page,
}) => {
  await open(page, "/spaces");
  const live = page.locator("main [role='status'], main [aria-live]").filter({ hasText: /^\d+ namespaces?$/ });
  await expect(live).toHaveCount(1);
  const handle = await live.elementHandle();
  await page.getByRole("textbox", { name: "Search namespaces" }).fill("zzzz-no-such");
  await expect(live).toHaveText("0 namespaces");
  expect(await handle?.evaluate((el) => el.isConnected)).toBe(true);
});
