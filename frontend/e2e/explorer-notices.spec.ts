import { expect, test, type Page } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import {
  boxOf,
  dockAssistant,
  installApi,
  pickOption,
  SMALL_HOP1,
  SMALL_SEED,
  taxonomyWithCommunities,
} from "./fixtures";

// A result that changed nothing still says so where you are looking. An expand's outcome
// is a toast and a line in the inspector's live region; the Communities / Colour facet
// says what computes communities and when; every control reaches 44 × 44 CSS px under a
// coarse pointer.

const SHOTS = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "../e2e-screenshots");
const NODE = ".graph-node[data-id]";
const TOAST = "[data-toast-viewport] [data-toast-kind]";
const NOTICE = "[data-testid=inspector-notice]";

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

// Beside the assistant docked at the push minimum (1280 px, the page keeping 640 px) the
// Explorer's three panes still sit side by side, none under another or the panel.
test("with the assistant docked, the Explorer's panes fit beside it", async ({ page }) => {
  await installApi(page);
  await dockAssistant(page);
  await page.goto(`/graph?seed=${SMALL_SEED}`);
  await expect(page.locator(NODE)).toHaveCount(SMALL_HOP1 + 1);
  const panel = page.locator("[data-assistant-panel]");
  await expect(panel).toHaveAttribute("data-mode", "push");
  const panes = [
    page.getByRole("complementary", { name: "Facets" }),
    page.getByRole("tree", { name: "Walk outline" }),
    page.getByRole("complementary", { name: "Inspector" }),
  ];
  const edge = (await boxOf(panel)).x;
  for (const pane of panes) {
    await expect(pane).toBeVisible();
    const box = await boxOf(pane);
    expect(box.width).toBeGreaterThan(0);
    expect(box.x + box.width).toBeLessThanOrEqual(edge + 1);
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
});

test("an expand's result is a toast and an inspector line — also when it changed nothing", async ({ page }) => {
  const calls = await installApi(page);
  const errors = collectErrors(page);
  await page.goto(`/graph?seed=${SMALL_SEED}`);
  await expect(page.locator(NODE)).toHaveCount(SMALL_HOP1 + 1);
  const tree = page.getByRole("tree", { name: "Walk outline" });
  const rows = tree.getByRole("treeitem");
  await expect(rows).toHaveCount(SMALL_HOP1 + 1);

  // The live region exists, empty, before anything is said.
  const notice = page.locator(NOTICE);
  await expect(notice).toHaveAttribute("aria-live", "polite");
  await expect(notice).toHaveText("");

  // Select the first hop-1 node (it becomes the inspector's subject) by its name, then `e`.
  const first = rows.nth(1);
  await first.getByText(`${SMALL_SEED}-h1-0`, { exact: true }).click();
  await expect(page.getByRole("complementary", { name: "Inspector" })).toContainText(`${SMALL_SEED}-h1-0`);
  await first.focus();
  await page.keyboard.press("e");
  await expect.poll(() => calls.related.length).toBe(1);
  expect(calls.related[0]).toMatchObject({ entity: `${SMALL_SEED}-h1-0`, top_k: 12 });
  await expect(page.locator(NODE)).toHaveCount(SMALL_HOP1 + 1 + 3);
  const toast = page.locator(TOAST);
  await expect(toast).toHaveCount(1);
  await expect(toast).toHaveAttribute("data-toast-kind", "ok");
  await expect(toast).toContainText(`Expanded: +3 under ${SMALL_SEED}-h1-0 · 1 already shown`);
  // Under the selected node the line does not repeat the node's name.
  await expect(notice).toHaveText("Expanded: +3 · 1 already shown");
  await shot(page, "08-expand-added");

  // The next row's neighbours are all in the tree already: nothing changes,
  // and the page still says so — one toast (the first is replaced), the
  // inspector line names the node because another is selected.
  // (Tree order puts h1-0's new children before h1-1: find the row by name.)
  const second = tree.getByRole("treeitem", { name: new RegExp(`^${SMALL_SEED}-h1-1 ·`) });
  await second.focus();
  await page.keyboard.press("e");
  await expect.poll(() => calls.related.length).toBe(2);
  expect(calls.related[1]).toMatchObject({ entity: `${SMALL_SEED}-h1-1` });
  await expect(page.locator(NODE)).toHaveCount(SMALL_HOP1 + 1 + 3);
  await expect(toast).toHaveCount(1);
  await expect(toast).toContainText(`Nothing new under ${SMALL_SEED}-h1-1 within one hop · 3 already shown`);
  await expect(notice).toHaveText(`${SMALL_SEED}-h1-1 — Expanded: 0 new within one hop · 3 already shown`);
  // The status line above the canvas says the same.
  await expect(page.locator("[aria-live=polite]").filter({ hasText: "entities within" })).toContainText(
    "Nothing new under",
  );
  await shot(page, "09-expand-nothing-new");

  // The toast leaves on its own after 4 s; the inspector line stays.
  await expect(toast).toHaveCount(0, { timeout: 8_000 });
  await expect(notice).toHaveText(`${SMALL_SEED}-h1-1 — Expanded: 0 new within one hop · 3 already shown`);
  // Selecting the node the notice is about drops the prefix.
  await second.getByText(`${SMALL_SEED}-h1-1`, { exact: true }).click();
  await expect(notice).toHaveText("Expanded: 0 new within one hop · 3 already shown");
  // No re-walk happened.
  expect(calls.traverse).toHaveLength(1);
  expect(errors).toEqual([]);
});

test("an outline row selects its node; the button at its end selects the edge", async ({ page }) => {
  await installApi(page);
  const errors = collectErrors(page);
  await page.goto(`/graph?seed=${SMALL_SEED}`);
  const row = page.getByRole("tree", { name: "Walk outline" }).getByRole("treeitem").nth(1);
  const inspector = page.getByRole("complementary", { name: "Inspector" });
  // The row's centre is the node, whatever the outline's width.
  await row.click();
  await expect(row).toHaveAttribute("aria-selected", "true");
  const edge = row.getByRole("button", { name: `Select the edge: co-mentioned 20× with ${SMALL_SEED}` });
  await edge.click();
  await expect(row).toHaveAttribute("aria-selected", "false");
  await expect(edge).toHaveAttribute("aria-pressed", "true");
  await expect(inspector).toContainText(`${SMALL_SEED} — ${SMALL_SEED}-h1-0`);
  expect(errors).toEqual([]);
});

// A selected row's action buttons reach the tree's scroll box, which clips its overflow:
// their ring (2 px wide, 2 px out) stays inside that box, on the first row and the last.
test("a selected outline row's buttons show their whole focus ring", async ({ page }) => {
  await installApi(page);
  await page.goto(`/graph?seed=${SMALL_SEED}`);
  const tree = page.getByRole("tree", { name: "Walk outline" });
  for (const row of [tree.getByRole("treeitem").first(), tree.getByRole("treeitem").last()]) {
    await row.click();
    await expect(row).toHaveAttribute("aria-selected", "true");
    const button = row.getByRole("button").first();
    // A key press first, so the focus that follows the click shows the keyboard ring.
    await page.keyboard.press("Shift");
    await button.focus();
    const ring = await button.evaluate((el) => {
      const style = getComputedStyle(el);
      const reach = parseFloat(style.outlineWidth) + parseFloat(style.outlineOffset);
      const b = el.getBoundingClientRect();
      const box = el.closest('[role="tree"]') as HTMLElement;
      const t = box.getBoundingClientRect();
      const top = t.top + box.clientTop;
      const bottom = top + box.clientHeight;
      return {
        visible: el.matches(":focus-visible"),
        inside: b.top - reach >= top - 0.5 && b.bottom + reach <= bottom + 0.5,
        detail: { buttonTop: b.top, buttonBottom: b.bottom, reach, top, bottom },
      };
    });
    expect(ring.visible).toBe(true);
    expect(ring.inside, JSON.stringify(ring.detail)).toBe(true);
  }
});

test("a re-walk drops the expand it started over: the late result lands nowhere", async ({ page }) => {
  const HOLD_MS = 1_500;
  const calls = await installApi(page, { relatedDelayMs: HOLD_MS });
  const errors = collectErrors(page);
  await page.goto(`/graph?seed=${SMALL_SEED}`);
  await expect(page.locator(NODE)).toHaveCount(SMALL_HOP1 + 1);
  const rows = page.getByRole("tree", { name: "Walk outline" }).getByRole("treeitem");
  await rows.nth(1).focus();
  await page.keyboard.press("e");
  await expect.poll(() => calls.related.length).toBe(1);

  // A walk setting changes while `facts.related` is still out: the view re-walks.
  await pickOption(page, "Hops", "3");
  await expect.poll(() => calls.traverse.length).toBe(2);
  await expect(page.locator(NODE)).toHaveCount(SMALL_HOP1 + 1);

  // The held answer arrives after the re-walk: no stray children, no toast, no line.
  await page.waitForTimeout(HOLD_MS + 500);
  await expect(page.locator(NODE)).toHaveCount(SMALL_HOP1 + 1);
  await expect(page.locator(TOAST)).toHaveCount(0);
  await expect(page.locator(NOTICE)).toHaveText("");
  // The node can be expanded again: the aborted expand left no `expanding` behind.
  await rows.nth(1).focus();
  await page.keyboard.press("e");
  await expect.poll(() => calls.related.length).toBe(2);
  await expect(page.locator(NODE)).toHaveCount(SMALL_HOP1 + 1 + 3);
  await expect(page.locator(TOAST)).toHaveCount(1);
  expect(errors).toEqual([]);
});

test("an expand clicked while a re-walk loads is dropped when the walk lands", async ({ page }) => {
  const WALK_MS = 800;
  const RELATED_MS = 2_000;
  const calls = await installApi(page, { traverseDelayMs: WALK_MS, relatedDelayMs: RELATED_MS });
  const errors = collectErrors(page);
  await page.goto(`/graph?seed=${SMALL_SEED}`);
  await expect(page.locator(NODE)).toHaveCount(SMALL_HOP1 + 1);
  const rows = page.getByRole("tree", { name: "Walk outline" }).getByRole("treeitem");
  await rows.nth(1).getByText(`${SMALL_SEED}-h1-0`, { exact: true }).click();
  const inspector = page.getByRole("complementary", { name: "Inspector" });
  await expect(inspector).toContainText(`${SMALL_SEED}-h1-0`);

  // The re-walk is out; the Inspector (which stays up while it loads) starts an expand.
  await pickOption(page, "Hops", "3");
  await expect.poll(() => calls.traverse.length).toBe(2);
  await inspector.getByRole("button", { name: "Expand in place (one hop of neighbours)" }).click();
  await expect.poll(() => calls.related.length).toBe(1);

  // The walk lands first, then the held `facts.related`: the new walk stays as it came.
  await page.waitForTimeout(RELATED_MS + 500);
  await expect(page.locator(NODE)).toHaveCount(SMALL_HOP1 + 1);
  await expect(page.locator(TOAST)).toHaveCount(0);
  await expect(page.locator(NOTICE)).toHaveText("");
  expect(errors).toEqual([]);
});

test("with no communities, the Colour facet says what computes them, unclipped", async ({ page }) => {
  await installApi(page);
  const errors = collectErrors(page);
  await page.goto("/graph?seed=alpha");
  await expect(page.locator(NODE)).toHaveCount(201);
  const facets = page.getByRole("complementary", { name: "Facets" });
  const none = facets.locator("[data-testid=communities-none]");
  await expect(none).toHaveText(
    "No communities yet. They are computed every 6 hours for namespaces walked in the last 14 days; this walk counts.",
  );
  const box = await none.evaluate((el) => ({
    clippedX: el.scrollWidth > el.clientWidth + 1,
    clippedY: el.scrollHeight > el.clientHeight + 1,
    ellipsis: getComputedStyle(el).textOverflow === "ellipsis",
    lines: Math.round(el.getBoundingClientRect().height / parseFloat(getComputedStyle(el).lineHeight)),
    title: el.getAttribute("title"),
  }));
  expect(box).toMatchObject({ clippedX: false, clippedY: false, ellipsis: false, title: null });
  expect(box.lines).toBeGreaterThanOrEqual(3);
  await expect(facets.getByRole("heading", { name: "Communities" })).toHaveCount(0);
  expect(await facets.locator("[title]").count()).toBe(0);
  await shot(page, "10-colour-no-communities");
  expect(errors).toEqual([]);
});

test("with communities, the facet counts them and says when, with the absolute time in a tooltip", async ({ page }) => {
  await installApi(page, { taxonomy: taxonomyWithCommunities() });
  const errors = collectErrors(page);
  await page.goto("/graph?seed=alpha");
  await expect(page.locator(NODE)).toHaveCount(201);
  const facets = page.getByRole("complementary", { name: "Facets" });
  await expect(facets.getByRole("heading", { name: "Communities" })).toBeVisible();
  const line = facets.locator("[data-testid=communities-computed]");
  await expect(line).toHaveText(/^2 communities · computed (2|3)h ago$/);
  await line.locator("span[tabindex]").focus();
  await expect(page.getByRole("tooltip")).toContainText(/^Computed \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC$/);
  await expect(facets.locator("[data-testid=communities-none]")).toHaveCount(0);
  expect(await facets.locator("[title]").count()).toBe(0);
  await shot(page, "11-communities-computed");
  expect(errors).toEqual([]);
});

test.describe("under a coarse pointer", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, deviceScaleFactor: 2 });

  // A selected row grows its action buttons, 44 px targets here: the row keeps its height.
  test("selecting an outline row keeps its height", async ({ page }) => {
    await installApi(page);
    await page.goto(`/graph?seed=${SMALL_SEED}`);
    const rows = page.getByRole("tree", { name: "Walk outline" }).getByRole("treeitem");
    const heights = () => rows.evaluateAll((els) => els.map((e) => e.getBoundingClientRect().height));
    await expect(rows).toHaveCount(SMALL_HOP1 + 1);
    const before = await heights();
    for (const [i, name] of [SMALL_SEED, `${SMALL_SEED}-h1-0`].entries()) {
      await rows.nth(i).getByText(name, { exact: true }).tap();
      await expect(rows.nth(i)).toHaveAttribute("aria-selected", "true");
      expect(await heights(), `row ${i} selected`).toEqual(before);
    }
  });

  test("a Chip, an IconButton and an outline row are 44 px targets", async ({ page }) => {
    await installApi(page, { trackerBound: true });
    const errors = collectErrors(page);
    await page.goto(`/graph?seed=${SMALL_SEED}`);
    await expect(page.locator(NODE)).toHaveCount(SMALL_HOP1 + 1);
    expect(await page.evaluate(() => matchMedia("(pointer: coarse)").matches)).toBe(true);

    const size = async (sel: ReturnType<Page["locator"]>) => {
      const b = await boxOf(sel);
      return { w: Math.round(b.width), h: Math.round(b.height) };
    };
    // Chip: a provenance toggle (the shortest label the Explorer draws).
    const chip = page
      .getByRole("complementary", { name: "Facets" })
      .getByRole("button", { name: "facet", exact: true });
    await chip.scrollIntoViewIfNeeded();
    const chipBox = await size(chip);
    // Outline row: the seed's tree row.
    const row = page.getByRole("tree", { name: "Walk outline" }).getByRole("treeitem").first();
    await row.scrollIntoViewIfNeeded();
    const rowBox = await size(row);
    // IconButton: select a node by its name so the inspector draws its actions.
    const node = page.getByRole("tree", { name: "Walk outline" }).getByRole("treeitem").nth(1);
    await node.getByText(`${SMALL_SEED}-h1-0`, { exact: true }).tap();
    await expect(node).toHaveAttribute("aria-selected", "true");
    const icon = page.getByRole("complementary", { name: "Inspector" }).getByRole("button", { name: "Copy entity" });
    await icon.scrollIntoViewIfNeeded();
    const iconBox = await size(icon);
    // SectionHeading's (i) hint button.
    const hint = page.getByRole("complementary", { name: "Facets" }).getByRole("button", { name: "About Relations" });
    await hint.scrollIntoViewIfNeeded();
    const hintBox = await size(hint);

    const measured = { chip: chipBox, row: rowBox, icon: iconBox, hint: hintBox };
    test.info().annotations.push({ type: "coarse-sizes", description: JSON.stringify(measured) });
    console.log(`coarse-sizes ${JSON.stringify(measured)}`);
    for (const b of [chipBox, iconBox, hintBox]) {
      expect(b.w).toBeGreaterThanOrEqual(44);
      expect(b.h).toBeGreaterThanOrEqual(44);
    }
    expect(rowBox.h).toBeGreaterThanOrEqual(44);
    await shot(page, "12-coarse-explorer");

    // Patterns mode: tab triggers take the shared `[role=tab]` rule; board cells are
    // measured and reported, not asserted here (`patterns.spec.ts` owns them).
    await page.getByRole("button", { name: "Patterns" }).click();
    const tab = page.getByRole("tab", { name: /Board/ });
    await expect(tab).toBeVisible();
    const tabBox = await size(tab);
    expect(tabBox.h).toBeGreaterThanOrEqual(44);
    const cell = page.getByRole("gridcell").filter({ hasNotText: "·" }).first();
    await expect(cell).toBeVisible();
    const cellBox = await size(cell);
    const patterns = { tab: tabBox, cell: cellBox, cellReaches44: cellBox.w >= 44 && cellBox.h >= 44 };
    test.info().annotations.push({ type: "coarse-patterns", description: JSON.stringify(patterns) });
    console.log(`coarse-patterns ${JSON.stringify(patterns)}`);
    await shot(page, "13-coarse-patterns");
    expect(errors).toEqual([]);
  });
});
