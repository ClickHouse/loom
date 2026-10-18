import { expect, test, type Page } from "@playwright/test";
import { installApi } from "./fixtures";

// The Library's cards and rows: a phone-width card keeps its parts inside it, and the row's
// More menu shows before a hover and on a touch screen.

async function open(page: Page, path: string) {
  await installApi(page, { memoryRows: 8, knowledgeRows: 4, runRows: 6, profileRows: 3 });
  await page.goto(path);
  await expect(page.locator("main h1").first()).toBeVisible();
  await page.waitForLoadState("networkidle");
}

// A row's actions are one More menu, always visible: a pointer and a
// touch screen both see it before any hover.
test("a library row's More menu shows without a hover, with a pointer or a touch screen", async ({ browser }) => {
  for (const touch of [false, true]) {
    const context = await browser.newContext({
      hasTouch: touch,
      isMobile: touch,
      viewport: { width: 1400, height: 900 },
    });
    const page = await context.newPage();
    await open(page, "/library");
    const row = page.locator("main [data-doc-row]").first();
    await expect(row.getByRole("button", { name: /^Actions for / })).toBeVisible();
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

// A sort other than newest first runs over the loaded documents only; while the cursor
// holds more, the count says so instead of reading as the whole library.
test("a sort over a partly loaded library says it covers the loaded documents", async ({ page }) => {
  await installApi(page, {});
  const rows = ["Charlie", "Alpha", "Bravo"].map((title, i) => ({
    source_uri: `docs/${title.toLowerCase()}.md`,
    title,
    description: "",
    tags: [],
    ingested_at: `2026-09-2${i}T12:00:00Z`,
    chunk_count: 2,
    superseded: false,
    has_blob: false,
  }));
  await page.route("**/v1/knowledge.list", (route) => route.fulfill({ json: { rows, next_cursor: "more" } }));
  await page.goto("/library");
  const bar = page.getByRole("search", { name: "Search and filters" });
  await expect(bar).toContainText("3 shown");
  await page.getByLabel("Sort documents").first().click();
  await page.getByRole("dialog", { name: "Options" }).getByText("Title (A–Z)").click();
  await expect(bar).toContainText("The first 3, sorted");
});
