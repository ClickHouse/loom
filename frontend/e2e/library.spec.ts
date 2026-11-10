import { expect, test, type Page } from "@playwright/test";
import { boxOf, installApi, MIN_TARGET_PX } from "./fixtures";

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

// A collection deep in the tree, with collections inside it: on a phone the path is one
// 44 px back button in the select's row, the select lists the collections inside, and the
// first document still starts within the list pages' chrome budget (y ≤ 360 at 375 × 812).
test.describe("a collection's path on a phone with a touch pointer", () => {
  test.use({ viewport: { width: 375, height: 812 }, hasTouch: true, isMobile: true });

  test("is one back button, and the first document starts within the budget", async ({ page }) => {
    await installApi(page, { knowledgeRows: 4 });
    const entry = (namespace: string, parent: string, title: string) => ({
      namespace,
      parent,
      personal: false,
      role: "writer",
      description: "",
      title,
    });
    const collections = [
      entry("col-team", "", "Team handbook and onboarding"),
      entry("col-q3", "col-team", "Quarter three planning"),
      entry("col-interviews", "col-q3", "Customer interviews"),
      entry("col-notes", "col-interviews", "Interview notes"),
      ...Array.from({ length: 8 }, (_, i) => entry(`col-notes-${i}`, "col-notes", `Session ${i + 1} notes`)),
    ];
    await page.route("**/v1/collection.list", (r) => r.fulfill({ json: { collections, next_cursor: null } }));
    await page.goto("/library?collection=col-notes");
    const back = page.getByRole("button", { name: "Back to Customer interviews" });
    await expect(back).toBeVisible();
    const box = await boxOf(back);
    expect(box.height).toBeGreaterThanOrEqual(MIN_TARGET_PX);
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(375);
    await expect(page.getByRole("region", { name: /Collections/ })).toHaveCount(0);
    const first = page.locator("main [data-doc-row]").first();
    await expect(first).toBeVisible();
    expect(await first.evaluate((el) => el.getBoundingClientRect().top)).toBeLessThanOrEqual(360);
    await back.click();
    await expect(page).toHaveURL(/collection=col-interviews/);
  });

  test("goes back to the namespace when the parent is one the person cannot read", async ({ page }) => {
    await installApi(page, { knowledgeRows: 4 });
    const collections = [
      {
        namespace: "col-shared",
        parent: "col-hidden",
        personal: false,
        role: "viewer",
        description: "",
        title: "Shared",
      },
    ];
    await page.route("**/v1/collection.list", (r) => r.fulfill({ json: { collections, next_cursor: null } }));
    await page.goto("/library?collection=col-shared");
    const back = page.getByRole("button", { name: /^Back to Namespace / });
    await expect(back).toBeVisible();
    const box = await boxOf(back);
    expect(box.height).toBeGreaterThanOrEqual(MIN_TARGET_PX);
    await back.tap();
    await expect(page).not.toHaveURL(/collection=/);
  });
});
