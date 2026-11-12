import { expect, test, type Page } from "@playwright/test";
import { dockAssistant, installApi } from "./fixtures";

// Nothing on screen sits over content it hides: a drawer's last section scrolls clear of
// its actions, a menu opened in a drawer draws over it, and a bar that stays at the top
// while a list scrolls is opaque, so the rows passing under it never show through.

async function memories(page: Page) {
  await installApi(page, { memoryRows: 30, knowledgeRows: 30 });
  await page.goto("/memories");
  await expect(page.locator("[data-memory-key]").first()).toBeVisible();
}

for (const [width, docked] of [
  [1400, false],
  [375, false],
  // Beside the assistant docked at the push minimum: the page keeps 592 px.
  [1280, true],
] as const) {
  test.describe(`${width} px${docked ? " with the assistant docked" : ""}`, () => {
    test.use({ viewport: { width, height: 800 } });
    test.beforeEach(async ({ page }) => {
      if (docked) await dockAssistant(page);
    });

    test("the memory drawer's last section scrolls clear of its actions", async ({ page }) => {
      await memories(page);
      await page.locator("[data-memory-key] [data-memory-open]").nth(3).click();
      const footer = page.locator("footer").filter({ has: page.getByRole("button", { name: "Delete" }) });
      await expect(footer).toBeVisible();
      const body = footer.locator("xpath=preceding-sibling::div[1]");
      await body.evaluate((el) => {
        el.scrollTop = el.scrollHeight;
      });
      const last = body.locator(":scope > *").last();
      const [lastBox, footerBox] = [await last.boundingBox(), await footer.boundingBox()];
      expect(lastBox && footerBox && lastBox.y + lastBox.height <= footerBox.y + 1).toBe(true);
    });

    // A menu is lifted over the drawer (styles.css `[data-radix-popper-content-wrapper]`).
    test("a menu opened in the memory drawer draws over it", async ({ page }) => {
      await memories(page);
      await page.locator("[data-memory-key] [data-memory-open]").first().click();
      await page.getByRole("dialog").getByRole("button", { name: "More actions" }).click();
      const menu = page.getByRole("menu", { name: "More actions" });
      await expect(menu).toBeVisible();
      const items = menu.getByRole("menuitem");
      expect(await items.count()).toBeGreaterThan(0);
      for (let i = 0; i < (await items.count()); i++) {
        const onTop = await items.nth(i).evaluate((el) => {
          const r = el.getBoundingClientRect();
          return el.contains(document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2));
        });
        expect(onTop, await items.nth(i).innerText()).toBe(true);
      }
    });

    for (const path of ["/memories", "/library"]) {
      test(`${path}: the search bar stays opaque over the rows that scroll under it`, async ({ page }) => {
        await memories(page);
        if (path !== "/memories") await page.goto(path);
        const bar = page.getByLabel("Search and filters");
        await expect(bar).toBeVisible();
        await page.locator("main").evaluate((m) => m.scrollTo(0, 900));
        const box = await bar.boundingBox();
        expect(box && box.y < 120).toBe(true);
        const bg = await bar.evaluate((el) => getComputedStyle(el).backgroundColor);
        expect(bg).not.toMatch(/rgba\(0, 0, 0, 0\)|transparent/);
        // A selection made down the list shows in the same bar, which keeps its height,
        // so the row just checked stays in view.
        const before = (await bar.boundingBox())?.height;
        // A row's own checkbox (once one is checked, the toolbar leads with its select-all).
        const box8 = page.getByRole("checkbox", { name: /^Select (?!all )/ }).nth(8);
        await box8.click();
        // The visible count; the bar's live region says it too, unseen.
        await expect(bar.getByText(/selected/).first()).toBeVisible();
        expect((await bar.boundingBox())?.height).toBe(before);
        const hit = await box8.evaluate((el) => {
          const r = el.getBoundingClientRect();
          const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
          return !!top && (el === top || el.contains(top) || top.contains(el));
        });
        expect(hit).toBe(true);
      });

      // A row list is one tab stop (useRowNav): its rows are walked with the arrows, and
      // Shift+Tab goes from the current row's controls to its open button.
      test(`${path}: a row reached with the arrows or Shift+Tab is never under the bar`, async ({ page }) => {
        // Keep entry motion running through the traversal, even on a fast browser.
        await page.addInitScript(() => {
          document.addEventListener("DOMContentLoaded", () => {
            const style = document.createElement("style");
            style.textContent = ".animate-view-in { animation-duration: 10s; }";
            document.head.append(style);
          });
        });
        await memories(page);
        if (path !== "/memories") await page.goto(path);
        const bar = page.getByLabel("Search and filters");
        await expect(bar).toBeVisible();
        const entry = page.locator(".animate-view-in");
        const startTime = await entry.evaluate((el) => {
          const animation = el.getAnimations()[0];
          if (!animation) throw new Error("missing view entry animation");
          animation.currentTime = 1_000;
          return animation.startTime;
        });
        await expect(entry).not.toHaveCSS("transform", "none");
        await page.locator("main").evaluate((m) => m.scrollTo(0, m.scrollHeight));
        await page.locator("main .list-row [data-row-open]").last().focus();
        const opacity = await entry.evaluate((el) => Number(getComputedStyle(el).opacity));
        expect(opacity).toBeGreaterThan(0);
        expect(opacity).toBeLessThan(1);
        for (let i = 0; i < 12; i++) {
          await page.keyboard.press(i % 3 === 2 ? "Shift+Tab" : "ArrowUp");
          if (i % 3 === 2) await page.keyboard.press("Tab");
          const covered = await page.evaluate(() => {
            const el = document.activeElement as HTMLElement | null;
            const bar = document.querySelector("search");
            if (!el || !bar || !el.closest("main .list-row")) return false;
            const r = el.getBoundingClientRect();
            return r.top < bar.getBoundingClientRect().bottom - 1;
          });
          expect(covered).toBe(false);
        }
        await page.evaluate(() => (document.activeElement as HTMLElement).blur());
        expect(await entry.evaluate((el) => el.getAnimations()[0]?.startTime)).toBe(startTime);
      });
    }
  });
}

// On a phone the pager stays one row (Click UI's own Pagination stacks into
// a column), and a row's checkbox takes a 44 px tap.
test.describe("phone", () => {
  test.use({ viewport: { width: 375, height: 800 }, hasTouch: true, isMobile: true });

  test("the memories pager is one row", async ({ page }) => {
    await installApi(page, { memoryRows: 130 });
    await page.goto("/memories");
    const pager = page.getByRole("navigation", { name: "Memories pages" });
    await pager.scrollIntoViewIfNeeded();
    // Both arrows measured in one frame: the page may still be scrolling to the pager.
    const gap = await pager.evaluate((nav) => {
      const top = (name: string) => nav.querySelector(`[aria-label="${name}"]`)?.getBoundingClientRect().top ?? NaN;
      return Math.abs(top("Previous page") - top("Next page"));
    });
    expect(gap, "the arrows sit on one row").toBeLessThan(4);
  });

  test("a row's checkbox takes a 44 px tap", async ({ page }) => {
    await memories(page);
    // A row's own checkbox: once one is checked, the toolbar leads with its select-all.
    const box = page.getByRole("checkbox", { name: /^Select alerts\// }).first();
    await box.scrollIntoViewIfNeeded();
    const r = await box.boundingBox();
    if (!r) throw new Error("no checkbox");
    // A tap 12 px left of the box's edge still lands on it.
    await page.touchscreen.tap(r.x - 12, r.y + r.height / 2);
    await expect(box).toBeChecked();
  });
});

// At 375 px a Library row's body keeps its width beside its actions, so titles,
// descriptions and tag labels stay in view.
test.describe("phone library", () => {
  test.use({ viewport: { width: 375, height: 800 } });
  test("a library row keeps its title readable", async ({ page }) => {
    await installApi(page, { knowledgeRows: 6 });
    await page.goto("/library");
    const row = page.locator("[data-doc-row]").first();
    await expect(row).toBeVisible();
    const title = row.getByRole("button", { name: /^Preview / });
    const box = await title.boundingBox();
    expect(box?.width ?? 0, "the row's body has room").toBeGreaterThan(200);
  });
});

// A click anywhere on a row opens it, the hover-tinted padding included.
test("a click on a row's padding opens it, on Memories and in the Library", async ({ page }) => {
  await memories(page);
  const row = page.locator("[data-card]").first();
  const box = await row.boundingBox();
  if (!box) throw new Error("no row");
  await page.mouse.click(box.x + box.width - 6, box.y + box.height - 4);
  await expect(page.locator("footer").filter({ has: page.getByRole("button", { name: "Delete" }) })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.goto("/library");
  const doc = page.locator("[data-doc-row]").first();
  const d = await doc.boundingBox();
  if (!d) throw new Error("no document row");
  await page.mouse.click(d.x + d.width / 2, d.y + d.height - 4);
  await expect(page.getByRole("dialog")).toBeVisible();
});
