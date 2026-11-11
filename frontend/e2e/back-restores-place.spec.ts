import { expect, test, type Page } from "@playwright/test";
import { installApi, NAMESPACE } from "./fixtures";
import { violations } from "./axe";

// Reading something keeps you where you are, and Back puts you back. A memory's
// entity opens a popover in the drawer; "Open in Graph" is a new history entry that offers
// "Back to memory <key>"; Back restores the list's scroll, reopens the drawer on that memory
// and focuses the entity chip. The open memory is the URL, so a reload reopens it. Run at
// desktop and phone widths.

const ENTITY = "rate limiting";

/** memory.get, memory.related and facts.related for a memory that names two entities. */
async function entityApi(page: Page) {
  await page.route("**/v1/memory.get", async (route) => {
    const body = route.request().postDataJSON() as { memory_key: string };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        found: true,
        memory_key: body.memory_key,
        value: `The public API allows 100 requests a minute (${body.memory_key}).`,
        memory_type: "semantic",
        confidence: 1,
        version: 1,
        pinned: false,
        tags: ["api"],
        created_at: "2026-09-19T00:00:00Z",
        updated_at: "2026-09-19T00:00:00Z",
        status: "active",
        agent: "alert-bot",
        entities: [ENTITY, "redis"],
        recall_count: 0,
        last_recalled_at: null,
      }),
    });
  });
  await page.route("**/v1/memory.related", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        results: [
          { memory_key: "alerts/2026-09-19/0003", shared_entities: [ENTITY], overlap_count: 1 },
          { memory_key: "alerts/2026-09-19/0004", shared_entities: ["redis"], overlap_count: 1 },
        ],
        source_entities: [ENTITY, "redis"],
        reason: "",
      }),
    }),
  );
  await page.route("**/v1/facts.related", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        entity: ENTITY,
        related: [
          { entity: "token bucket", co_occur: 4, role: "both" },
          { entity: "redis", co_occur: 2, role: "both" },
        ],
      }),
    }),
  );
}

const mainScroll = (page: Page) => page.locator("main#main").evaluate((m) => m.scrollTop);

for (const width of [1400, 375]) {
  test.describe(`at ${width} px`, () => {
    test.use({ viewport: { width, height: 760 } });

    test("an entity opens in place, Open in Graph pushes, and Back restores scroll, drawer and focus", async ({
      page,
    }, testInfo) => {
      await installApi(page, { memoryRows: 60 });
      await entityApi(page);
      await page.goto("/memories", { waitUntil: "domcontentloaded" });
      await expect(page.locator("[data-memory-open]").first()).toBeVisible();

      // Down the list, then open a card that is on screen there.
      await page.locator("main#main").evaluate((m) => m.scrollTo({ top: 1200 }));
      expect(await mainScroll(page)).toBeGreaterThan(600);
      const key = await page.evaluate(() => {
        const main = document.getElementById("main")!.getBoundingClientRect();
        const cards = [...document.querySelectorAll<HTMLElement>("[data-memory-key]")];
        const seen = cards.find((c) => {
          const r = c.getBoundingClientRect();
          return r.top > main.top + 120 && r.bottom < main.bottom;
        });
        return seen?.getAttribute("data-memory-key") ?? "";
      });
      expect(key).not.toBe("");
      await page.locator(`[data-memory-key="${key}"] [data-memory-open]`).click();
      const drawer = page.getByRole("dialog", { name: key });
      await expect(drawer).toBeVisible();
      expect(new URL(page.url()).searchParams.get("memory")).toBe(key);

      // The popover: what the entity is, where else it is mentioned, its neighbours.
      await drawer.getByRole("link", { name: `Entity ${ENTITY}` }).click();
      const pop = page.getByRole("dialog", { name: `Entity ${ENTITY}` });
      await expect(pop.getByText("Mentioned in 2 memories")).toBeVisible();
      await expect(pop.getByText("token bucket")).toBeVisible();
      await expect(pop.getByRole("button", { name: "alerts/2026-09-19/0003" })).toBeFocused();
      const box = await pop.boundingBox();
      expect(box && box.width <= 360 && box.x >= 0 && box.x + box.width <= width).toBe(true);
      expect(await violations(page)).toEqual([]);
      await page.screenshot({ path: testInfo.outputPath(`entity-popover-${width}.png`) });

      // Escape closes only the popover and hands the focus back.
      await page.keyboard.press("Escape");
      await expect(pop).toBeHidden();
      await expect(drawer).toBeVisible();
      await expect(drawer.getByRole("link", { name: `Entity ${ENTITY}` })).toBeFocused();

      // Open in Graph: a pushed entry with the way back above the toolbar.
      const left = await mainScroll(page);
      expect(left).toBeGreaterThan(600);
      await drawer.getByRole("link", { name: `Entity ${ENTITY}` }).click();
      await pop.getByRole("link", { name: "Open in Graph" }).click();
      await expect(page).toHaveURL(new RegExp(`/graph\\?namespace=${NAMESPACE}&seed=rate\\+limiting$`));
      const back = page.getByRole("button", { name: `Back to memory ${key}` });
      await expect(back).toBeVisible();
      await expect(page.getByRole("heading", { level: 1, name: "Graph" })).toBeFocused();
      await expect(page.locator("#loom-nav-status")).toHaveText(`Graph, walked from ${ENTITY}`);
      await page.screenshot({ path: testInfo.outputPath(`graph-back-${width}.png`) });

      // Back: the list where it was, the drawer on that memory, the focus on the chip.
      await back.click();
      await expect(page).toHaveURL(new RegExp(`/memories\\?namespace=${NAMESPACE}&memory=${encodeURIComponent(key)}$`));
      await expect(page.getByRole("dialog", { name: key })).toBeVisible();
      await expect(page.getByRole("link", { name: `Entity ${ENTITY}` })).toBeFocused();
      await expect.poll(() => mainScroll(page)).toBe(left);

      // A reload reopens the same memory.
      await page.reload({ waitUntil: "domcontentloaded" });
      await expect(page.getByRole("dialog", { name: key })).toBeVisible();
    });

    test("the drawer footer is one row: Edit, Ask about this, More and Delete; two on a phone", async ({
      page,
    }, testInfo) => {
      await installApi(page, { memoryRows: 3 });
      await entityApi(page);
      await page.goto("/memories?memory=alerts%2F2026-09-19%2F0001", { waitUntil: "domcontentloaded" });
      const drawer = page.getByRole("dialog", { name: "alerts/2026-09-19/0001" });
      const footer = drawer.locator("footer");
      await expect(footer.getByRole("button", { name: "More actions" })).toBeVisible();
      const h = await footer.evaluate((f) => f.getBoundingClientRect().height);
      // A phone-wide drawer wraps the footer, Ask about this on the first row beside Edit.
      expect(h).toBeLessThanOrEqual(width < 640 ? 110 : 60);
      const top = async (name: string) => (await footer.getByRole("button", { name }).boundingBox())?.y;
      expect(await top("Ask the assistant about this memory")).toBe(await top("Edit"));
      await footer.getByRole("button", { name: "More actions" }).click();
      const menu = page.getByRole("menu", { name: "More actions" });
      await expect(menu.getByRole("menuitem", { name: "Copy", exact: true })).toBeVisible();
      // Every item reads in full on a phone.
      const cut = await menu
        .getByRole("menuitem")
        .evaluateAll((items) => items.filter((i) => i.scrollWidth > i.clientWidth + 1).map((i) => i.textContent));
      expect(cut).toEqual([]);
      expect(await violations(page)).toEqual([]);
      await page.screenshot({ path: testInfo.outputPath(`drawer-more-${width}.png`) });
    });
  });
}

test("a g-jump focuses the new page's h1 and says where it went", async ({ page }) => {
  await installApi(page, { memoryRows: 3, knowledgeRows: 2 });
  await page.goto("/memories", { waitUntil: "domcontentloaded" });
  await expect(page.locator("[data-memory-open]").first()).toBeVisible();
  await page.locator("main#main").focus();
  await page.keyboard.press("g");
  await page.keyboard.press("l");
  await expect(page).toHaveURL(new RegExp(`/library\\?namespace=${NAMESPACE}$`));
  await expect(page.getByRole("heading", { level: 1, name: "Library" })).toBeFocused();
  await expect(page.locator("#loom-nav-status")).toHaveText("Library");
  await expect(page).toHaveTitle("Library · Loom");
  // Back is not a new navigation: the Memories h1 takes the focus, the list at its top.
  await page.goBack();
  await expect(page.getByRole("heading", { level: 1, name: "Memories" })).toBeFocused();
});

// Every row list keys its rows (data-focus-key), so Back focuses the row that had it.
test("Back after a g-jump focuses the Library row that had the focus", async ({ page }) => {
  await installApi(page, { memoryRows: 3, knowledgeRows: 5 });
  await page.goto("/library", { waitUntil: "domcontentloaded" });
  const open = page.locator("[data-doc-row]").nth(2).locator("[data-row-open]");
  await expect(open).toBeVisible();
  await open.focus();
  await page.keyboard.press("g");
  await page.keyboard.press("m");
  await expect(page).toHaveURL(new RegExp(`/memories\\?namespace=${NAMESPACE}$`));
  await page.goBack();
  await expect(page.locator("[data-doc-row]").nth(2).locator("[data-row-open]")).toBeFocused();
});
