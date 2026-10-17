import { expect, test, type Page } from "@playwright/test";
import { violations } from "./axe";
import { installApi } from "./fixtures";

// The Memories, Library and Conversations lists against SPEC-LISTS, in the browser: the
// chrome over the first row stays inside its budget (C10), the selection takes a range and
// clears on Escape (C6), a row list is one tab stop whose ring is drawn on the row (C7), a
// Library row's More menu shows without a hover (C2), and Insights holds the usage audit
// and the mentor (A1).

const now = new Date().toISOString();
const THREADS = Array.from({ length: 6 }, (_, i) => ({
  thread_id: `t-${i}`,
  agent: ["claude-code", "cursor"][i % 2],
  msg_count: 3 + i,
  first_seen: now,
  last_seen: now,
  last_role: "assistant",
  last_content_excerpt: "Summarised the change and pushed the branch.",
  title: i === 0 ? "" : `Refactor the ingest lane ${i}`,
}));

async function open(page: Page, path: string) {
  await installApi(page, { memoryRows: 12, knowledgeRows: 6, profileRows: 3 });
  await page.route("**/v1/conversation.threads", (route) => route.fulfill({ json: { threads: THREADS } }));
  await page.goto(path);
  await expect(page.locator("main .list-row").first()).toBeVisible();
}

const BUDGET = [
  { width: 1400, height: 900, top: 300 },
  { width: 375, height: 812, top: 360 },
];

for (const { width, height, top } of BUDGET) {
  test.describe(`${width} px`, () => {
    test.use({ viewport: { width, height } });

    // SPEC-LISTS C10: the first row starts at or above y = 300 on a desktop, 360 on a phone.
    for (const path of ["/memories", "/library", "/conversations"]) {
      test(`${path}: the first row starts at or above y = ${top}`, async ({ page }) => {
        await open(page, path);
        const box = await page.locator("main .list-row").first().boundingBox();
        expect(box?.y ?? Infinity).toBeLessThanOrEqual(top);
      });
    }

    test("a Shift-click selects the range, and Escape clears the selection", async ({ page }) => {
      await open(page, "/memories");
      const boxes = page.locator("main .list-row").getByRole("checkbox");
      await boxes.nth(0).click();
      await boxes.nth(2).click({ modifiers: ["Shift"] });
      const bar = page.getByRole("search", { name: "Search and filters" });
      await expect(bar).toContainText("3 selected");
      await expect(boxes.nth(1)).toBeChecked();
      await page.keyboard.press("Escape");
      await expect(bar).not.toContainText("selected");
      await expect(boxes.nth(1)).not.toBeChecked();
    });

    test("a library row's More menu is there without a hover, and the row keeps its title", async ({ page }) => {
      await open(page, "/library");
      const row = page.locator("main [data-doc-row]").first();
      const more = row.getByRole("button", { name: /^Actions for / });
      await expect(more).toBeVisible();
      expect((await row.getByRole("button", { name: /^Preview / }).boundingBox())?.width ?? 0).toBeGreaterThan(200);
      await more.click();
      await expect(page.getByRole("menuitem", { name: "Copy URI" })).toBeVisible();
      expect(await violations(page)).toEqual([]);
    });
  });
}

test("a row list is one tab stop, the arrows move through it, and the ring is drawn on the row", async ({ page }) => {
  await open(page, "/library");
  const opens = page.locator("main [data-row-open]");
  await opens.first().focus();
  await page.keyboard.press("ArrowDown");
  await expect(opens.nth(1)).toBeFocused();
  expect(await opens.evaluateAll((els) => els.filter((e) => (e as HTMLElement).tabIndex === 0).length)).toBe(1);
  // Focus from the keyboard: the row draws the ring, around its meta and its menu too.
  await page.keyboard.press("ArrowDown");
  const ring = await page
    .locator("main .list-row")
    .nth(2)
    .evaluate((el) => getComputedStyle(el).outlineStyle);
  expect(ring).toBe("solid");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("dialog")).toBeVisible();
});

test("Insights holds the usage audit and the mentor, and the list is a tab away", async ({ page }) => {
  await installApi(page, { memoryRows: 12 });
  await page.goto("/memories?section=insights");
  await expect(page.getByRole("tab", { name: "Insights" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("button", { name: "Usage audit", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Mentor", exact: true })).toBeVisible();
  await expect(page.locator("main .list-row")).toHaveCount(0);
  expect(await violations(page)).toEqual([]);
  await page.getByRole("tab", { name: "Memories" }).click();
  await expect(page).toHaveURL(/\/memories$/);
  await expect(page.locator("main .list-row").first()).toBeVisible();
  await page.goBack();
  await expect(page.getByRole("button", { name: "Usage audit", exact: true })).toBeVisible();
});

test("the Profile's sections are a list with a selection toolbar", async ({ page }) => {
  await open(page, "/profile");
  const boxes = page.locator("main .list-row").getByRole("checkbox");
  await boxes.nth(0).click();
  await boxes.nth(2).click({ modifiers: ["Shift"] });
  await expect(page.getByRole("search", { name: "Search and filters" })).toContainText("3 selected");
  expect(await violations(page)).toEqual([]);
});
