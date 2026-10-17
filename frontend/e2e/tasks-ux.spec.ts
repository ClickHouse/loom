import { expect, test, type Page } from "@playwright/test";
import { installApi } from "./fixtures";
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
      // Four tiles on one row, never a zero dot or a second row.
      const tops = await page
        .getByRole("group", { name: "Needs you" })
        .getByRole("button")
        .evaluateAll((els) => els.map((e) => Math.round(e.getBoundingClientRect().top)));
      expect(new Set(tops).size).toBe(1);
      await expect(page.getByLabel("Search tasks")).toBeVisible();
      await expect(page.getByRole("button", { name: "Posted by" })).toBeVisible();
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
