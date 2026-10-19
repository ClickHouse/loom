import { expect, test, type Page } from "@playwright/test";
import { installApi, MIN_TARGET_PX } from "./fixtures";
import { violations } from "./axe";

// The Tasks page as a person works it, at desktop and phone width: the inbox is a rail or
// a Select, the tiles and the toolbar fit, a triage decision is one click (Accept) or a
// reasoned close (Won't do… from the row's menu), and the focus goes on to the next task.

const TRIAGE = "Task 1: a regression the public bot found";
const TODO = "Task 2: a regression the public bot found";
const ID1 = "00000000-0000-4000-8000-000000000001";

async function open(page: Page, width: number) {
  await page.setViewportSize({ width, height: width < 768 ? 812 : 900 });
  await installApi(page);
  const sent: Array<{ op: string; body: Record<string, unknown> }> = [];
  for (const op of ["task.accept", "task.wont_do"])
    await page.route(`**/v1/${op}`, async (route) => {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      sent.push({ op, body });
      const status = op === "task.accept" ? "todo" : "wont_do";
      await route.fulfill({ json: { task: { task_id: body.task_id, status, title: TRIAGE }, seq: 2 } });
    });
  await page.goto("/tasks");
  await expect(page.getByRole("row", { name: TRIAGE })).toBeVisible();
  return sent;
}

for (const width of [1400, 375]) {
  test.describe(`at ${width} px`, () => {
    test("the scope, the tiles and the toolbar fit the width, and the page is axe-clean", async ({ page }) => {
      await open(page, width);
      if (width < 768) {
        await expect(page.getByRole("button", { name: "Inbox" })).toBeVisible();
        await expect(page.getByRole("navigation", { name: "Inboxes" })).toHaveCount(0);
      } else {
        await expect(page.getByRole("navigation", { name: "Inboxes" })).toBeVisible();
      }
      if (width < 768) {
        // A phone has no tiles: they, the kind and the poster are one Filters menu.
        await expect(page.getByRole("group", { name: "Needs you" })).toHaveCount(0);
        await page.getByRole("button", { name: "Filters" }).click();
        const menu = page.getByRole("menu", { name: "Filters" });
        await expect(menu.getByRole("menuitemradio", { name: /^Triage · decide \(\d+\)$/ })).toBeVisible();
        await expect(menu.getByRole("menuitemradio", { name: "Other posters" })).toBeVisible();
        expect(await violations(page)).toEqual([]);
        await page.keyboard.press("Escape");
        await expect(page.getByRole("button", { name: "Filters" })).toBeFocused();
      } else {
        // Four tiles on one row, never a zero dot or a second row, their labels whole.
        const tiles = page.getByRole("group", { name: "Needs you" }).getByRole("button");
        const tops = await tiles.evaluateAll((els) => els.map((e) => Math.round(e.getBoundingClientRect().top)));
        expect(new Set(tops).size).toBe(1);
        const cut = await tiles.evaluateAll((els) =>
          els.some((e) => [...e.querySelectorAll<HTMLElement>(".lbl")].some((l) => l.scrollWidth > l.clientWidth)),
        );
        expect(cut).toBe(false);
        await expect(page.getByRole("button", { name: "Posted by" })).toBeVisible();
      }
      await expect(page.getByLabel("Search tasks")).toBeVisible();
      // No sideways scroll on the page.
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      // The keys live in the help dialog and the tooltips, not on the page.
      await expect(page.locator("main kbd")).toHaveCount(0);
      expect(await violations(page)).toEqual([]);
    });

    test("Accept is one click; the focus goes on to the next task", async ({ page }) => {
      const sent = await open(page, width);
      await page.getByRole("button", { name: `Accept ${TRIAGE}` }).click();
      await expect.poll(() => sent.map((s) => s.op)).toEqual(["task.accept"]);
      expect(sent[0].body.task_id).toBe(ID1);
      await expect(page.getByRole("row", { name: TODO })).toBeFocused();
      await expect(page.getByText(/Accepted “Task 1/)).toBeVisible();
    });

    test("Won't do… from the row's menu asks for a reason, and the focus goes on to the next task", async ({
      page,
    }) => {
      const sent = await open(page, width);
      await page.getByRole("button", { name: `Actions for ${TRIAGE}` }).click();
      await expect(page.getByRole("menu")).toBeVisible();
      expect(await violations(page)).toEqual([]);
      await page.getByRole("menuitem", { name: "Won't do…" }).click();
      const dialog = page.getByRole("dialog", { name: "Won't do" });
      await expect(dialog).toBeVisible();
      await expect(dialog.getByRole("button", { name: "Won't do" })).toBeDisabled();
      await dialog.getByLabel(/Reason/).fill("duplicate of #12");
      await dialog.getByRole("button", { name: "Won't do" }).click();
      await expect(dialog).toBeHidden();
      await expect.poll(() => sent.map((s) => s.op)).toEqual(["task.wont_do"]);
      await expect(page.getByRole("row", { name: TODO })).toBeFocused();
    });

    test("a search that matches nothing says so, and Clear filters brings the rows back", async ({ page }) => {
      await open(page, width);
      await page.getByLabel("Search tasks").fill("nothing like this");
      await expect(page.getByText('No tasks match "nothing like this"')).toBeVisible();
      await page.getByRole("button", { name: "Clear filters" }).click();
      await expect(page.getByRole("row", { name: TRIAGE })).toBeVisible();
    });
  });
}

test("at 1400 px the open panel's actions are in view without scrolling", async ({ page }) => {
  await open(page, 1400);
  await page.getByRole("row", { name: TRIAGE }).click();
  const panel = page.getByRole("complementary", { name: "Task detail" });
  const accept = panel.getByRole("group", { name: "Actions" }).getByRole("button", { name: "Accept" });
  await expect(accept).toBeInViewport();
  await expect(panel.getByRole("button", { name: "Won't do…" })).toBeInViewport();
});

/** The outline offset of the focused element, and whether it shows the keyboard ring. */
async function focusRing(page: Page): Promise<{ visible: boolean; offset: string }> {
  return page.evaluate(() => {
    const el = document.activeElement as HTMLElement;
    return { visible: el.matches(":focus-visible"), offset: getComputedStyle(el).outlineOffset };
  });
}

// A lane clips its overflow and inbox items stack flush, so a row's and an inbox's ring is
// drawn inside the box.
test("a task row and an inbox take an inset focus ring", async ({ page }) => {
  await open(page, 1400);
  await page.getByRole("row", { name: TRIAGE }).focus();
  expect(await focusRing(page), "a row").toEqual({ visible: true, offset: "-2px" });
  await page.getByRole("navigation", { name: "Inboxes" }).getByRole("button").first().focus();
  expect(await focusRing(page), "an inbox").toEqual({ visible: true, offset: "-2px" });
});

// On a phone the first task starts within the list pages' chrome budget (y ≤ 360 at
// 375 × 812): the scope badge and the List/Board switch give way. Under a touch pointer,
// where every control is 44 px, the tiles and the filters are one Filters menu on the
// inbox select's row.
test.describe("a phone with a touch pointer", () => {
  test.use({ hasTouch: true, isMobile: true });

  test("at 375 px the first task starts within the chrome budget", async ({ page }) => {
    await open(page, 375);
    const top = await page.getByRole("row", { name: TRIAGE }).evaluate((el) => el.getBoundingClientRect().top);
    expect(top).toBeLessThanOrEqual(360);
    for (const name of ["Inbox", "Filters"]) {
      const box = await page.getByRole("button", { name, exact: true }).boundingBox();
      if (!box) throw new Error(`${name} has no box`);
      expect(box.height, `${name} is 44 px tall`).toBeGreaterThanOrEqual(MIN_TARGET_PX);
      expect(box.x, `${name} starts on the screen`).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width, `${name} ends on the screen`).toBeLessThanOrEqual(375);
    }
  });
});
