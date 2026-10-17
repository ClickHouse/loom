import { expect, test, type Page } from "@playwright/test";
import { installApi } from "./fixtures";

// A count or summary that changes as the reader types is announced from a status region
// that is mounted before the change, so a screen reader hears it: the Library's filter
// count, the memory search summary and the Spaces count.

async function open(page: Page, path: string) {
  await installApi(page, { memoryRows: 8, knowledgeRows: 4, runRows: 6, profileRows: 3 });
  await page.goto(path);
  await expect(page.locator("main h1").first()).toBeVisible();
  await page.waitForLoadState("networkidle");
}

test("the library's filter count is announced from a status region mounted before the search", async ({ page }) => {
  await open(page, "/library");
  // The toolbar shows the count; the region says it when a search or filter moves it, so it
  // is there, empty, before the search.
  const regions = await page.locator("main [role='status']").elementHandles();
  await page.getByRole("textbox", { name: "Search library" }).fill("tag:lane-0");
  await expect
    .poll(async () =>
      (await Promise.all(regions.map((r) => r.evaluate((e) => e.textContent ?? "")))).includes("1 match"),
    )
    .toBe(true);
});

test("the memory search summary is announced from a status region mounted before the search", async ({ page }) => {
  await open(page, "/memories");
  const regions = await page.locator("main [role='status']").elementHandles();
  await page.getByRole("textbox", { name: "Search memories" }).fill("lag");
  await expect
    .poll(async () => {
      const texts = await Promise.all(regions.map((r) => r.evaluate((e) => e.textContent ?? "")));
      return texts.some((t) => /result|match|Searching/i.test(t));
    })
    .toBe(true);
});

test("the Spaces count is a status message that stays mounted through a search", async ({ page }) => {
  await open(page, "/spaces");
  const status = page.locator("main [role='status']").filter({ hasText: /^\d+ namespaces?$/ });
  await expect(status).toHaveCount(1);
  const handle = await status.elementHandle();
  await page.getByRole("textbox", { name: "Search namespaces" }).fill("zzzz-no-such");
  await expect(status).toHaveText("0 namespaces");
  expect(await handle?.evaluate((el) => el.isConnected)).toBe(true);
});
