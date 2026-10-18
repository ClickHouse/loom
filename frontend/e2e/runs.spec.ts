import { expect, test, type Page } from "@playwright/test";
import { violations } from "./axe";
import { installApi } from "./fixtures";

// The Runs ledger: grouped by session every table keeps its column header, and a run's
// replay opens clean.

async function open(page: Page, path: string) {
  await installApi(page, { memoryRows: 8, knowledgeRows: 4, runRows: 6, profileRows: 3 });
  await page.goto(path);
  await expect(page.locator("main h1").first()).toBeVisible();
  await page.waitForLoadState("networkidle");
}

test("grouped runs label every column and draw the header over the first session only", async ({ page }) => {
  await open(page, "/runs");
  await page.getByRole("button", { name: "Group by session" }).click();
  const tables = page.locator("main table");
  await expect(tables.nth(1)).toBeVisible();
  const heights: number[] = [];
  for (const table of await tables.all()) {
    await expect(table.getByRole("columnheader", { name: "Goal" })).toBeAttached();
    heights.push((await table.locator("thead").boundingBox())?.height ?? 0);
  }
  expect(heights[0]).toBeGreaterThan(20);
  expect(heights.slice(1).every((h) => h <= 1)).toBe(true);
  expect(await violations(page)).toEqual([]);
});

test("a run's replay opens and has no WCAG A/AA violations", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await open(page, "/runs");
  await page.locator("main table tbody button").first().click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText("recalled keys")).toBeVisible();
  expect(errors).toEqual([]);
  expect(await violations(page)).toEqual([]);
});

test("on a phone the status and agent filters sit under the 48 px toolbar, nothing clipped", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 800 });
  await open(page, "/runs");
  const toolbar = page.getByRole("search", { name: "Search and filters" });
  expect((await toolbar.boundingBox())?.height).toBe(48);
  const statuses = page.getByLabel("Filter runs by status");
  await expect(statuses).toBeVisible();
  expect(await toolbar.getByLabel("Filter runs by status").count()).toBe(0);
  for (const control of [statuses, page.getByRole("button", { name: "Agent" })]) {
    const box = await control.boundingBox();
    expect(box && box.x >= 0 && box.x + box.width <= 375).toBe(true);
  }
  // Nothing in the row scrolls sideways, and neither does the page.
  const sideways = await statuses.evaluate((el) => {
    for (let n = el.parentElement; n && n !== document.body; n = n.parentElement) {
      if (n.scrollWidth > n.clientWidth + 1) return true;
    }
    return false;
  });
  expect(sideways).toBe(false);
});

test("the runs intro keeps its info icon on the sentence's first line", async ({ page }) => {
  await open(page, "/runs");
  const intro = page.getByText(/^One row per agent execution/);
  const icon = page.getByRole("button", { name: "About runs" });
  const [text, info] = [await intro.boundingBox(), await icon.boundingBox()];
  expect(text && info).toBeTruthy();
  expect(info!.y - text!.y).toBeLessThan(12);
});

const runList = (page: Page, match: (body: Record<string, unknown>) => boolean) =>
  page.waitForRequest((r) => r.url().endsWith("/v1/run.list") && match(r.postDataJSON() as Record<string, unknown>));

test.describe("1400 px", () => {
  test.use({ viewport: { width: 1400, height: 900 } });

  test("a column header sorts the runs on the server, from the keyboard, and says how", async ({ page }) => {
    await installApi(page, { runRows: 120 });
    await page.goto("/runs");
    const table = page.getByRole("table", { name: "Runs", exact: true });
    await expect(table.locator("tbody tr")).toHaveCount(50);
    const took = table.getByRole("columnheader", { name: "Took" });
    await expect(table.getByRole("columnheader", { name: "Started" })).toHaveAttribute("aria-sort", "descending");
    await expect(took).toHaveAttribute("aria-sort", "none");
    const asked = runList(page, (b) => b.order_by === "duration_desc" && !b.offset);
    await took.getByRole("button", { name: "Took" }).focus();
    await page.keyboard.press("Enter");
    await asked;
    await expect(took).toHaveAttribute("aria-sort", "descending");
    // Longest first: the fixture's durations grow with the run's index.
    await expect(table.locator("tbody tr").first()).toContainText("run-0119");
    expect(await violations(page)).toEqual([]);
  });

  test("the search reaches the server and the toolbar counts the matches", async ({ page }) => {
    await installApi(page, { runRows: 120 });
    await page.goto("/runs");
    const table = page.getByRole("table", { name: "Runs", exact: true });
    await expect(table.locator("tbody tr")).toHaveCount(50);
    const asked = runList(page, (b) => b.q === "RUN-001");
    await page.getByRole("textbox", { name: "Search runs" }).fill("RUN-001");
    await asked;
    // run-0010 to run-0019.
    await expect(table.locator("tbody tr")).toHaveCount(10);
    await expect(page.getByRole("search", { name: "Search and filters" })).toContainText("10 matches");
    await expect(page.getByRole("navigation", { name: "Runs pages" })).toHaveCount(0);
  });

  test("grouped by session, each session is a heading", async ({ page }) => {
    await open(page, "/runs");
    await page.getByRole("button", { name: "Group by session" }).click();
    await expect(page.getByRole("heading", { level: 2, name: "s-0" })).toBeVisible();
    expect(await violations(page)).toEqual([]);
  });
});

test.describe("375 px", () => {
  test.use({ viewport: { width: 375, height: 812 } });

  test("runs are two-line list rows, not stacked table cards", async ({ page }) => {
    await open(page, "/runs");
    await expect(page.locator("main table")).toHaveCount(0);
    const rows = page.getByRole("list", { name: "Runs" }).getByRole("listitem");
    await expect(rows).toHaveCount(6);
    const first = rows.first();
    // Line 1 the goal and its status; line 2 agent · started · took.
    await expect(first.getByRole("button", { name: /^Triage lane \d alert burst$/ })).toBeVisible();
    await expect(first).toContainText(
      /(Completed|Active|Failed)(oncall|alert-bot)·Sep 20(, 2026)?( \(Sep 20, 2026, \d\d:\d\d UTC\))?·took \d+s/,
    );
    expect((await first.boundingBox())?.height).toBeLessThan(80);
    const sideways = await page.evaluate(() => {
      const main = document.getElementById("main");
      return Math.max(
        document.documentElement.scrollWidth - window.innerWidth,
        main ? main.scrollWidth - main.clientWidth : 0,
      );
    });
    expect(sideways).toBeLessThanOrEqual(0);
    // One tab stop; the arrow keys move between rows; Enter opens the replay.
    await first.getByRole("button").first().focus();
    await page.keyboard.press("ArrowDown");
    await expect(rows.nth(1).locator("[data-row-open]")).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.keyboard.press("Escape");
    expect(await violations(page)).toEqual([]);
  });

  test("grouped by session on a phone, the headings hold their rows", async ({ page }) => {
    await open(page, "/runs");
    await page.getByRole("button", { name: "Group by session" }).click();
    await expect(page.getByRole("heading", { level: 2, name: "s-0" })).toBeVisible();
    await expect(page.getByRole("list", { name: "Runs in session s-0" })).toBeVisible();
    expect(await violations(page)).toEqual([]);
  });
});

// Self-review: on a phone the first run starts within the list pages' chrome budget
// (y ≤ 360 at 375 × 812); the description waits behind its info button.
test("at 375 px the first run starts within the chrome budget", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await installApi(page, { runRows: 60 });
  await page.goto("/runs");
  const first = page.locator("main .list-row").first();
  await expect(first).toBeVisible();
  expect(await first.evaluate((el) => el.getBoundingClientRect().top)).toBeLessThanOrEqual(360);
});
