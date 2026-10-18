import { expect, test, type Page } from "@playwright/test";
import * as fs from "node:fs";
import { violations } from "./axe";
import { codeReposFixture, installApi, trackerSourcesFixture } from "./fixtures";

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
  await installApi(page, {
    memoryRows: 12,
    knowledgeRows: 6,
    profileRows: 3,
    codeRepos: codeReposFixture(),
    trackerSources: trackerSourcesFixture(),
  });
  await page.route("**/v1/conversation.threads", (route) => route.fulfill({ json: { threads: THREADS } }));
  await page.goto(path);
  await expect(page.locator("main .list-row").first()).toBeVisible();
}

// A phone is measured with a touch pointer, where every control is 44 px (review: the
// budgets passed with a mouse and failed with a thumb).
const BUDGET = [
  { width: 1400, height: 900, top: 300, touch: false },
  { width: 375, height: 812, top: 360, touch: true },
];

for (const { width, height, top, touch } of BUDGET) {
  test.describe(`${width} px`, () => {
    test.use({ viewport: { width, height }, hasTouch: touch, isMobile: touch });

    // SPEC-LISTS C10: the first row starts at or above y = 300 on a desktop, 360 on a phone.
    for (const path of ["/memories", "/library", "/conversations", "/profile", "/spaces", "/repositories"]) {
      test(`${path}: the first row starts at or above y = ${top}`, async ({ page }) => {
        await open(page, path);
        const box = await page.locator("main .list-row").first().boundingBox();
        expect(box?.y ?? Infinity).toBeLessThanOrEqual(top);
      });
    }

    // Review: on a phone the Memories and Repositories placeholders were cut mid-word.
    for (const path of ["/memories", "/library", "/conversations", "/profile", "/spaces", "/repositories"]) {
      test(`${path}: the search's placeholder fits its field`, async ({ page }) => {
        await open(page, path);
        const field = page.locator("main search input").first();
        const fit = await field.evaluate((el) => {
          const input = el as HTMLInputElement;
          const css = getComputedStyle(input);
          const ctx = document.createElement("canvas").getContext("2d")!;
          ctx.font = `${css.fontWeight} ${css.fontSize} ${css.fontFamily}`;
          const room = input.clientWidth - parseFloat(css.paddingLeft) - parseFloat(css.paddingRight);
          return { text: ctx.measureText(input.placeholder).width, room, placeholder: input.placeholder };
        });
        expect(fit.text, `"${fit.placeholder}"`).toBeLessThanOrEqual(fit.room);
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

// Review: a row's placeholder size (content-visibility) was 76 px while a memory is drawn
// taller, so the scrollbar jumped as rows came into view. Each list's is its drawn height.
for (const path of ["/memories", "/library", "/conversations"]) {
  test(`${path}: a row's placeholder size is its drawn height`, async ({ page }) => {
    await open(page, path);
    const row = page.locator("main .list-row").first();
    const { drawn, intrinsic } = await row.evaluate((el) => ({
      drawn: el.getBoundingClientRect().height,
      intrinsic: parseFloat(getComputedStyle(el).containIntrinsicHeight.replace("auto", "")),
    }));
    expect(Math.abs(drawn - intrinsic), `drawn ${drawn}, placeholder ${intrinsic}`).toBeLessThanOrEqual(8);
  });
}

// Review: only the open buttons and checkboxes left the tab order; every tag chip and menu
// trigger stayed a tab stop (151 on Memories, 241 on the Library). Only the current row's
// controls are tab stops now.
for (const path of ["/memories", "/library", "/conversations", "/profile", "/spaces", "/repositories"]) {
  test(`${path}: no row but the current one has a tab stop`, async ({ page }) => {
    await open(page, path);
    await page.waitForTimeout(300);
    const outside = await page.evaluate(() => {
      const rows = [...document.querySelectorAll("main .list-row")];
      const current = rows.find((r) => r.querySelector<HTMLElement>("[data-row-open]")?.tabIndex === 0);
      const focusable = 'a[href], button, input, select, textarea, [tabindex], [role="button"], [role="checkbox"]';
      return rows
        .filter((r) => r !== current)
        .flatMap((r) => [...r.querySelectorAll<HTMLElement>(focusable)])
        .filter((el) => el.tabIndex >= 0 && !(el as HTMLButtonElement).disabled)
        .map((el) => el.getAttribute("aria-label") ?? el.textContent);
    });
    expect(outside).toEqual([]);
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

// Review: the Kbd and Recall Lab bar rules had raw 4 px and 2 px radii beside Click UI's
// (the computed values match, so the stylesheet itself is read).
test("styles.css draws the Kbd and the score bar on radius tokens", () => {
  const css = fs.readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
  const rule = (selector: string) => css.slice(css.indexOf(`${selector} {`)).split("}")[0];
  for (const selector of [".kbd", ".recall-bar", ".recall-bar > span"]) {
    expect(rule(selector), selector).toMatch(/border-radius: var\(--/);
  }
});
