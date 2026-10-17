import { expect, test, type Page } from "@playwright/test";
import { violations } from "./axe";
import { installApi } from "./fixtures";

// Recall Lab scores a query as a Click UI Table (SPEC-LISTS A7): one row per result, the
// score drawn as Click UI's table bar, the row count in the toolbar. At desktop and phone
// width it is axe clean and never scrolls the page sideways; a row opens the memory.

const SHOTS = process.env.RECALL_SHOTS;

const hit = (i: number) => ({
  memory_key: `runbooks/deploy/step-${i}-with-a-rather-long-key-name`,
  memory_type: i % 2 ? "procedural" : "semantic",
  content_excerpt: `Drain lane ${i} before re-pointing the ingest, then replay the backlog from the last checkpoint.`,
  distance: 0.2 + i * 0.05,
  similarity: 0.82 - i * 0.05,
  rank_score: 0.9 - i * 0.12,
  confidence: 1,
  source: "fact",
  score_components: { semantic: 0.6 - i * 0.05, entity_ann_boost_effective: i % 2 ? 0 : 0.06, recency: 0.02 },
});

async function open(page: Page) {
  await installApi(page, { memoryRows: 4 });
  // Registered after installApi, so these answer first.
  await page.route("**/v1/memory.search", (r) =>
    r.fulfill({ json: { results: [0, 1, 2, 3, 4].map(hit), embedding_model: "text-embedding-3-small" } }),
  );
  await page.route("**/v1/memory.list", (r) =>
    r.fulfill({ json: { entries: [], total: 0, today: 0, truncated: false } }),
  );
  await page.goto("/recall");
  await expect(page.locator("main h1").first()).toBeVisible();
  await page.getByLabel("Recall query").fill("deploy");
  const table = page.getByRole("table", { name: "Recall scores" });
  await expect(table.locator("tbody tr")).toHaveCount(5);
  await page.waitForLoadState("networkidle");
  return table;
}

async function sideways(page: Page): Promise<number> {
  return page.evaluate(() => {
    const main = document.getElementById("main");
    const doc = document.documentElement.scrollWidth - window.innerWidth;
    return Math.max(doc, main ? main.scrollWidth - main.clientWidth : 0);
  });
}

for (const width of [1400, 375]) {
  test.describe(`${width} px`, () => {
    test.use({ viewport: { width, height: 900 } });

    test("scores are a table with bars, axe clean and no sideways scroll", async ({ page }) => {
      const table = await open(page);
      const bars = table.locator(".recall-bar > span");
      await expect(bars).toHaveCount(5);
      expect(await bars.first().evaluate((el) => getComputedStyle(el).backgroundColor)).toBe("rgb(223, 223, 223)");
      expect(await sideways(page)).toBeLessThanOrEqual(0);
      // Nor does the table inside its own box: on a phone a card never runs past the edge.
      expect(
        await table.evaluate((t) => t.getBoundingClientRect().width - (t.parentElement?.clientWidth ?? 0)),
      ).toBeLessThanOrEqual(1);
      expect(await violations(page)).toEqual([]);
      if (SHOTS) await page.screenshot({ path: `${SHOTS}/recall-${width}.png`, fullPage: true });
    });
  });
}

test("the row count and embedder sit in the toolbar, and a row opens the memory", async ({ page }) => {
  const table = await open(page);
  await expect(page.getByRole("search", { name: "Score a query" })).toContainText("5 rows · text-embedding-3-small");
  await table.getByRole("button", { name: /step-0/ }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("dialog")).toBeVisible();
});
