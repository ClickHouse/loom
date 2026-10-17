import { expect, test, type Page } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { boxOf, installApi, isRecord } from "./fixtures";

// Patterns mode: the toggle exists only when `tracker.binding` names a tracker namespace;
// the board is a grid with roving focus; a cell lists its issues; the tracker graph draws
// 97288's caused_by edge from PR 90220.

const SHOTS = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "../e2e-screenshots");

async function shot(page: Page, name: string): Promise<void> {
  fs.mkdirSync(SHOTS, { recursive: true });
  const file = path.join(SHOTS, `${name}.png`);
  await page.screenshot({ path: file });
  await test.info().attach(name, { path: file, contentType: "image/png" });
}

function collectErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  return errors;
}

test("the Patterns toggle is absent when the namespace has no QA binding", async ({ page }) => {
  const calls = await installApi(page);
  const errors = collectErrors(page);
  await page.goto("/graph");
  await expect(page.getByRole("heading", { name: "Pick an entity to walk from" })).toBeVisible();
  await expect.poll(() => calls.qa.filter((c) => c.op === "binding").length).toBe(1);
  expect(calls.qa[0]).toMatchObject({ op: "binding", namespace: "bench" });
  await expect(page.getByRole("button", { name: "Patterns" })).toHaveCount(0);
  await expect(page.getByText("Patterns are available on QA-bound namespaces.")).toBeVisible();
  // A deep link into the mode on an unbound namespace keeps the mode and
  // offers the tracker sources the caller reads — no board, no grid, and no silent drop
  // to the walk.
  await page.goto("/graph?mode=patterns");
  await expect(page.getByRole("heading", { name: "Which tracker source?" })).toBeVisible();
  await expect(page.getByRole("grid")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Pick an entity to walk from" })).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("on a bound namespace the toggle opens the board, the grid roves focus, a cell lists issues and the tracker graph draws 97288 ← 90220", async ({
  page,
}) => {
  const calls = await installApi(page, { trackerBound: true });
  const errors = collectErrors(page);
  await page.goto("/graph");
  const toggle = page.getByRole("button", { name: "Patterns" });
  await expect(toggle).toBeVisible();
  await expect(toggle).toHaveAttribute("aria-pressed", "false");
  expect(calls.qa.filter((c) => c.op === "answer")).toHaveLength(0);

  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-pressed", "true");
  await expect(page).toHaveURL(/mode=patterns/);
  const grid = page.getByRole("grid");
  await expect(grid).toBeVisible();
  await expect.poll(() => calls.qa.filter((c) => c.op === "answer").length).toBe(2);
  expect(
    calls.qa
      .filter((c) => c.op === "answer")
      .map((c) => c.question)
      .sort(),
  ).toEqual(["bug board", "test gaps"]);
  expect(calls.qa.filter((c) => c.op === "answer").every((c) => c.namespace === "tracker-bench")).toBe(true);
  await expect(grid.getByRole("rowheader")).toHaveCount(3);
  await expect(grid.getByRole("rowheader").first()).toHaveText(/MergeTree/);
  await shot(page, "08-patterns-board");

  // Roving focus: one tab stop among the cells; arrows move it; ↵ selects.
  const cells = grid.locator("[data-cell]");
  await expect(cells).toHaveCount(5);
  const tabStops = () =>
    cells.evaluateAll((els) => els.filter((e) => e instanceof HTMLElement && e.tabIndex === 0).length);
  expect(await tabStops()).toBe(1);
  await cells.first().focus();
  await page.keyboard.press("ArrowRight");
  const focused = () => page.evaluate(() => document.activeElement?.getAttribute("aria-label") ?? null);
  expect(await focused()).toMatch(/^MergeTree low/);
  expect(await tabStops()).toBe(1);
  await page.keyboard.press("ArrowLeft");
  expect(await focused()).toMatch(/^MergeTree medium/);
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/component=MergeTree&severity=medium/);
  const inspector = page.getByRole("complementary", { name: "Inspector" });
  await expect(inspector.getByRole("heading", { name: "Cell" })).toBeVisible();
  await expect.poll(() => calls.qa.filter((c) => c.op === "answer").length).toBe(3);
  // The cell's severity rides in the anchors; the server filtered the read, so the two
  // medium rows are the list and no "N of M shown" line appears (the 300-row ceiling was
  // not reached).
  expect(calls.qa.filter((c) => c.op === "answer")[2]).toMatchObject({
    anchors: { components: ["MergeTree"], severities: ["medium"], words: ["risk"] },
  });
  const list = inspector.getByRole("list", { name: "Issues in this cell" });
  await expect(list.getByRole("listitem")).toHaveCount(2);
  await expect(inspector.getByText(/of 27 shown/)).toHaveCount(0);
  await expect(inspector.getByText("incidents, escalations: restricted — not available with this token")).toBeVisible();
  await expect(inspector.getByRole("heading", { name: /Incidents/ })).toHaveCount(0);
  await shot(page, "09-patterns-cell");

  // An issue: its record, then the tracker graph with the culprit PR on the left.
  await list.getByRole("button", { name: /97288/ }).click();
  await expect(page).toHaveURL(/issue=97288/);
  await expect(inspector.getByRole("heading", { name: "Issue" })).toBeVisible();
  await expect(inspector.getByText("named by graph")).toBeVisible();
  await expect.poll(() => calls.qa.filter((c) => c.op === "item").length).toBe(2);
  expect(calls.qa.filter((c) => c.op === "item")[0]).toMatchObject({ numbers: [97288] });
  await page.getByRole("tab", { name: /tracker graph/i }).click();
  const svg = page.locator("svg.tracker-graph-svg");
  await expect(svg).toBeVisible();
  const edge = svg.locator('.tracker-edge[data-rel="caused_by"][data-from="90220"][data-to="97288"]');
  await expect(edge).toHaveCount(1);
  await expect(svg.locator('.tracker-edge[data-rel="fixed_by"]')).toHaveCount(4);
  await expect(svg.locator('.graph-node[data-id="90220"][data-role="cause"]')).toHaveCount(1);
  await expect(svg.locator('.graph-node[data-id="90220"] .tracker-named')).toHaveText("named by graph");
  await shot(page, "10-patterns-tracker-graph");

  // Hovering the culprit lights it and its edge on the DOM; nothing is read.
  const before = calls.qa.length;
  const box = await boxOf(svg.locator('.graph-node[data-id="90220"] circle').first());
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 2 });
  await expect(svg).toHaveAttribute("data-hover", "");
  await expect(edge).toHaveAttribute("data-hot", "");
  await expect(page.locator(".graph-readout")).toContainText("#90220");
  expect(calls.qa.length).toBe(before);

  // Back leaves the mode; the walk shell is where it was.
  await page.goBack();
  await expect(toggle).toHaveAttribute("aria-pressed", "false");
  await expect(page.getByRole("heading", { name: "Pick an entity to walk from" })).toBeVisible();
  expect(errors).toEqual([]);
});

test("a deep link naming an issue opens on its tracker graph, and picking an issue switches to it", async ({
  page,
}) => {
  const calls = await installApi(page, { trackerBound: true });
  const errors = collectErrors(page);
  await page.goto("/graph?mode=patterns&component=MergeTree&severity=medium&issue=97288");
  const graphTab = page.getByRole("tab", { name: /tracker graph/i });
  await expect(graphTab).toHaveAttribute("aria-selected", "true");
  const svg = page.locator("svg.tracker-graph-svg");
  await expect(svg.locator('.tracker-edge[data-rel="caused_by"][data-from="90220"][data-to="97288"]')).toHaveCount(1);
  await expect.poll(() => calls.qa.filter((c) => c.op === "item").length).toBe(2);
  // Back to the board by picking a cell; picking an issue from its list returns to the graph.
  await page.getByRole("tab", { name: /Board/ }).click();
  const grid = page.getByRole("grid");
  // The deep link already selected MergeTree × medium, so pick another cell
  // first (a second click on the selected cell clears it), then come back.
  await grid.locator('[data-cell][aria-label^="MergeTree low"]').click();
  await expect(page.getByRole("tab", { name: /Board/ })).toHaveAttribute("aria-selected", "true");
  await grid.locator('[data-cell][aria-label^="MergeTree medium"]').click();
  await expect(page).toHaveURL(/component=MergeTree&severity=medium/);
  const inspector = page.getByRole("complementary", { name: "Inspector" });
  await inspector.getByRole("list", { name: "Issues in this cell" }).getByRole("button", { name: /97288/ }).click();
  await expect(graphTab).toHaveAttribute("aria-selected", "true");
  await shot(page, "11-patterns-issue-deeplink");
  expect(errors).toEqual([]);
});

test("on a restricted tracker namespace a cell's incidents and escalations are served, not named as restricted", async ({
  page,
}) => {
  const calls = await installApi(page, { trackerRestricted: true });
  const errors = collectErrors(page);
  await page.goto("/graph?mode=patterns&component=MergeTree&severity=medium");
  const facets = page.getByRole("complementary", { name: "Facets" });
  await expect(facets.getByText("tracker-bench-private")).toBeVisible();
  await expect(facets.getByText("restricted", { exact: true })).toBeVisible();
  const inspector = page.getByRole("complementary", { name: "Inspector" });
  await expect(inspector.getByRole("heading", { name: "Cell" })).toBeVisible();
  await expect
    .poll(
      () =>
        calls.qa.filter((c) => c.op === "answer" && isRecord(c.anchors) && Array.isArray(c.anchors.components)).length,
    )
    .toBe(1);
  expect(calls.qa.filter((c) => c.op === "answer").every((c) => c.namespace === "tracker-bench-private")).toBe(true);
  await expect(inspector.getByRole("list", { name: "Issues in this cell" }).getByRole("listitem")).toHaveCount(2);
  // The restricted class as rows: one incident line (ref, title, severity, state · opened, error type) …
  await expect(inspector.getByRole("heading", { name: /Incidents/ })).toBeVisible();
  const incidents = inspector.getByRole("list", { name: "Incidents in this component" });
  await expect(incidents.getByRole("listitem")).toHaveCount(1);
  await expect(incidents).toContainText("#9");
  await expect(incidents).toContainText("Logical error in MergeTreeData::loadDataParts");
  await expect(incidents).toContainText("critical");
  await expect(incidents).toContainText("open · opened 2026-09-01");
  await expect(incidents).toContainText("LOGICAL_ERROR");
  // … an honest empty for the escalations' window, and no "restricted" line.
  await expect(inspector.getByRole("heading", { name: /Escalations/ })).toBeVisible();
  await expect(inspector.getByText("None in the last 90 days.")).toBeVisible();
  await expect(inspector.getByText(/restricted — not available with this token/)).toHaveCount(0);
  await shot(page, "12-patterns-restricted-cell");
  expect(errors).toEqual([]);
});

test("a deep link to 119601 draws 116490 below as a related issue — not a duplicate — and the inspector lists it", async ({
  page,
}) => {
  const calls = await installApi(page, { trackerBound: true });
  const errors = collectErrors(page);
  await page.goto("/graph?mode=patterns&issue=119601");
  const svg = page.locator("svg.tracker-graph-svg");
  await expect(svg).toBeVisible();
  // The fixture graph relates 116490 to 119601 (`related_to`); the picture draws it below
  // the seed and labels the edge for what it is.
  await expect(svg.locator('.graph-node[data-id="116490"][data-role="linked"]')).toHaveCount(1);
  await expect(svg.locator('.tracker-edge[data-rel="related_to"][data-from="119601"][data-to="116490"]')).toHaveCount(
    1,
  );
  await expect(svg.locator('.tracker-edge[data-rel="related_to"]')).toHaveCount(2);
  await expect(svg.locator('.tracker-edge[data-rel="duplicate_of"]')).toHaveCount(0);
  await expect(svg.locator('.tracker-edge[data-to="116490"] .graph-edge-note')).toHaveText("related to");
  await expect.poll(() => calls.qa.filter((c) => c.op === "answer" && c.question === "duplicates").length).toBe(1);
  expect(calls.qa.find((c) => c.op === "answer" && c.question === "duplicates")).toMatchObject({
    anchors: { numbers: [119601], words: ["similar"] },
  });
  // the second tracker.item read fetched the linked issues' titles
  await expect.poll(() => calls.qa.filter((c) => c.op === "item").length).toBe(2);
  expect(calls.qa.filter((c) => c.op === "item")[1]).toMatchObject({ numbers: [116490, 116481] });
  const inspector = page.getByRole("complementary", { name: "Inspector" });
  await expect(inspector.getByRole("heading", { name: "Issue" })).toBeVisible();
  await expect(inspector.getByRole("button", { name: /#116490 .*related, not a duplicate/ })).toBeVisible();
  await expect(inspector.getByText("duplicates", { exact: true })).toHaveCount(0);
  await shot(page, "13-patterns-related-119601");
  expect(errors).toEqual([]);
});

test.describe("under a coarse pointer", () => {
  // A board cell can render at 72x27 CSS px on a phone; a finger needs 44 px
  // (`--spacing-hit`). Playwright's hasTouch + isMobile make Chromium answer `pointer:
  // coarse`.
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });

  test("every board cell is at least 44 px tall and the mouse layout is not", async ({ page }) => {
    await installApi(page, { trackerBound: true });
    const errors = collectErrors(page);
    await page.goto("/graph?mode=patterns");
    expect(await page.evaluate(() => matchMedia("(pointer: coarse)").matches)).toBe(true);
    const cells = page.getByRole("grid").locator("[data-cell]");
    await expect(cells).toHaveCount(5);
    const heights = await cells.evaluateAll((els) => els.map((e) => e.getBoundingClientRect().height));
    expect(heights.length).toBe(5);
    // Rounded: a cell at 44 px can measure 43.9998 after sub-pixel layout.
    for (const h of heights) expect(Math.round(h)).toBeGreaterThanOrEqual(44);
    // the cell still selects on a tap
    await cells.first().tap();
    await expect(page).toHaveURL(/component=MergeTree&severity=medium/);
    expect(errors).toEqual([]);
  });
});
