import { expect, test, type Page } from "@playwright/test";
import { violations } from "./axe";
import { CODE_TRACKER_ONLY_CLAIMS, CODE_TRACKER_ONLY_MINE, installApi } from "./fixtures";

// The assistant edge (the launcher's column at the window's right from md), the window the
// panel expands to from 1024 px (over the page, with no address or history change), and an
// /assistant address, which lands on the landing page and opens the assistant over it.

const launcher = (page: Page) => page.locator('[data-focus-key="assistant-launcher"]');
const panel = (page: Page) => page.locator("[data-assistant-panel]");
const rect = (page: Page, selector: string) =>
  page
    .locator(selector)
    .first()
    .evaluate((el) => {
      const r = el.getBoundingClientRect();
      return { left: r.left, top: r.top, width: r.width, right: r.right, bottom: r.bottom };
    });

async function onMemories(page: Page, width: number, height = 900) {
  await page.setViewportSize({ width, height });
  await installApi(page);
  await page.goto("/memories");
  await expect(page.locator("main h1").first()).toBeVisible();
}

for (const width of [768, 1280, 1920]) {
  test(`at ${width} px the edge's button is a side tab below the header, its ring whole`, async ({ page }) => {
    await onMemories(page, width);
    const edge = await rect(page, ".assistant-edge");
    expect(edge).toMatchObject({ width: 48, right: width });
    const button = await rect(page, ".assistant-edge-button");
    const header = await rect(page, "header");
    expect(button.top).toBeGreaterThan(header.bottom);
    await launcher(page).focus();
    await page.keyboard.press("Shift+Tab");
    await page.keyboard.press("Tab");
    await expect(launcher(page)).toBeFocused();
    // The ring is drawn inside the button, so the window's edge never cuts it.
    const ring = await launcher(page).evaluate((el) => getComputedStyle(el).outlineOffset);
    expect(ring).toBe("-2px");
    expect(await violations(page)).toEqual([]);
  });
}

test("a click at the window's last pixel, level with the button, opens the panel", async ({ page }) => {
  await onMemories(page, 1280);
  const button = await rect(page, ".assistant-edge-button");
  await page.mouse.click(1279, button.top + (button.bottom - button.top) / 2);
  await expect(panel(page)).toHaveAttribute("data-open", "");
});

test("at 1280 px with the sidebar open the panel docks beside the page, which keeps 592 px", async ({ page }) => {
  await onMemories(page, 1280);
  const edge = await rect(page, ".assistant-edge");
  const main = await rect(page, "main");
  await launcher(page).click();
  await expect(panel(page)).toHaveAttribute("data-mode", "push");
  const docked = await rect(page, "[data-assistant-panel]");
  const after = await rect(page, "main");
  // The edge and the page's corner stay; the page gives up exactly the panel's width.
  expect(await rect(page, ".assistant-edge")).toEqual(edge);
  expect({ left: after.left, top: after.top }).toEqual({ left: main.left, top: main.top });
  expect(main.width - after.width).toBeCloseTo(docked.width, 0);
  expect(after.width).toBeGreaterThanOrEqual(592);
  expect(docked.right).toBeCloseTo(edge.left, 0);
  await expect(launcher(page)).toHaveAttribute("aria-expanded", "true");
  // Expand and Collapse move neither the edge nor <main>.
  await page.getByRole("button", { name: "Expand the assistant" }).click();
  expect(await rect(page, ".assistant-edge")).toEqual(edge);
  expect(await rect(page, "main")).toEqual(after);
  await page.getByRole("button", { name: "Collapse the assistant" }).click();
  expect(await rect(page, "main")).toEqual(after);
  await page.getByRole("button", { name: "Close the assistant" }).click();
  expect(await rect(page, "main")).toEqual(main);
  expect(await rect(page, ".assistant-edge")).toEqual(edge);
});

test("Expand leaves the address and the history alone, and plays no motion", async ({ page }) => {
  await onMemories(page, 1400);
  await launcher(page).click();
  const url = page.url();
  const length = await page.evaluate(() => history.length);
  await page.getByRole("button", { name: "Expand the assistant" }).click();
  await expect(panel(page)).toHaveAttribute("data-mode", "expanded");
  await expect(panel(page)).toHaveAttribute("role", "dialog");
  expect(page.url()).toBe(url);
  expect(await page.evaluate(() => history.length)).toBe(length);
  expect(await panel(page).evaluate((el) => el.getAnimations().length)).toBe(0);
  const box = await rect(page, "[data-assistant-panel]");
  expect(box).toMatchObject({ left: 24, top: 0, right: 1400 });
  expect(await violations(page)).toEqual([]);
});

test("Esc and a click on the scrim strip collapse the window; into the overlay with no motion", async ({ page }) => {
  await onMemories(page, 1100);
  await launcher(page).click();
  await expect(panel(page)).toHaveAttribute("data-mode", "overlay");
  await page.waitForFunction(() => document.querySelector("[data-assistant-panel]")?.getAnimations().length === 0);
  await page.getByRole("button", { name: "Expand the assistant" }).click();
  await page.keyboard.press("Escape");
  await expect(panel(page)).toHaveAttribute("data-mode", "overlay");
  expect(await panel(page).evaluate((el) => el.getAnimations().length)).toBe(0);
  await page.getByRole("button", { name: "Expand the assistant" }).click();
  await page.mouse.click(10, 450);
  await expect(panel(page)).toHaveAttribute("data-mode", "overlay");
  await expect(panel(page)).toHaveAttribute("data-open", "");
});

test("at 1920 px a wheel over the window's side area scrolls the transcript", async ({ page }) => {
  await onMemories(page, 1920);
  await launcher(page).click();
  await page.getByRole("button", { name: "Expand the assistant" }).click();
  // A card in the transcript, which then takes the window's height.
  await page.getByRole("button", { name: "What does this namespace hold, and who appears in it?" }).click();
  await expect(page.getByTestId("assistant-card")).toBeVisible();
  const box = page.getByTestId("assistant-transcript");
  // Taller than its box: the scroll has somewhere to go.
  await box.evaluate((el) => {
    const filler = document.createElement("div");
    filler.style.height = "3000px";
    el.firstElementChild?.append(filler);
  });
  const area = await rect(page, "[data-testid='assistant-transcript']");
  await page.mouse.move(area.left + 40, area.top + 100);
  await page.mouse.wheel(0, 400);
  await expect.poll(() => box.evaluate((el) => el.scrollTop)).toBeGreaterThan(0);
});

for (const [mode, width] of [
  ["push", 1400],
  ["overlay", 1100],
] as const) {
  test(`dragging the splitter keeps the panel's edge under the pointer (${mode})`, async ({ page }) => {
    await onMemories(page, width);
    await launcher(page).click();
    await expect(panel(page)).toHaveAttribute("data-mode", mode);
    // Past the overlay's slide-in, which moves the splitter while it plays.
    await expect(panel(page)).not.toHaveAttribute("data-entering", "");
    const splitter = page.getByRole("separator", { name: "Resize the assistant panel" });
    const start = await splitter.boundingBox();
    if (!start) throw new Error("no splitter");
    const y = start.y + start.height / 2;
    await page.mouse.move(start.x + start.width / 2, y);
    await page.mouse.down();
    await page.mouse.move(start.x - 40, y, { steps: 4 });
    await page.mouse.up();
    const after = await rect(page, "[data-assistant-panel]");
    expect(after.left).toBeCloseTo(start.x - 40, -1);
  });
}

test.describe("an /assistant address", () => {
  test("lands on Memories and opens the window over it, the page keeping its width", async ({ page }) => {
    await page.setViewportSize({ width: 1400, height: 900 });
    await installApi(page);
    await page.goto("/assistant");
    await expect(panel(page)).toHaveAttribute("data-mode", "expanded");
    await expect(page).toHaveURL(/\/memories/);
    await expect(page.getByRole("textbox", { name: "Question" })).toBeFocused();
    // No spacer: the page under the window keeps its full width.
    const main = await rect(page, "main");
    const edge = await rect(page, ".assistant-edge");
    expect(main.right).toBeCloseTo(edge.left, 0);
  });

  test("an account that reaches nothing lands on Setup, and nothing opens", async ({ page }) => {
    await installApi(page, { viewer: true, claims: [], mine: [], savedNamespace: null, collections: [] });
    await page.goto("/assistant");
    await expect(page.getByRole("heading", { name: "Settings", exact: true })).toBeVisible();
    await page.waitForTimeout(500);
    await expect(panel(page)).toHaveCount(0);
  });

  test("a namespace the account cannot read opens nothing", async ({ page }) => {
    await installApi(page);
    await page.goto("/assistant?namespace=group-b-shared");
    await expect(page.getByRole("heading", { level: 1, name: "No access to group-b-shared" })).toBeVisible();
    await page.waitForTimeout(500);
    await expect(panel(page)).toHaveCount(0);
  });

  test("on a code namespace it opens over Repositories", async ({ page }) => {
    await installApi(page, {
      viewer: true,
      claims: CODE_TRACKER_ONLY_CLAIMS,
      mine: CODE_TRACKER_ONLY_MINE,
      savedNamespace: null,
    });
    await page.goto("/assistant");
    await expect(page).toHaveURL(/\/repositories/);
    await expect(panel(page)).toHaveAttribute("data-open", "");
  });

  test("an entry an earlier build pushed opens the assistant on Back after a reload", async ({ page }) => {
    await onMemories(page, 1280);
    await page.evaluate(() => history.pushState(null, "", "/assistant"));
    await page.evaluate(() => history.pushState(null, "", "/runs"));
    await page.reload();
    await expect(page.locator("main h1").first()).toHaveText("Runs");
    await page.goBack();
    await expect(panel(page)).toHaveAttribute("data-mode", "expanded");
    // The address names the page shown.
    await expect(page).toHaveURL(/\/memories/);
    await expect(page.locator("main h1").first()).toHaveText("Memories");
  });

  test("Back onto it with the panel docked expands it, the page keeping its width", async ({ page }) => {
    await onMemories(page, 1280);
    await page.evaluate(() => history.pushState(null, "", "/assistant"));
    await page.evaluate(() => history.pushState(null, "", "/runs"));
    await page.reload();
    await expect(page.locator("main h1").first()).toHaveText("Runs");
    await launcher(page).click();
    await expect(panel(page)).toHaveAttribute("data-mode", "push");
    const main = await rect(page, "main");
    await page.goBack();
    await expect(panel(page)).toHaveAttribute("data-mode", "expanded");
    expect((await rect(page, "main")).width).toBeCloseTo(main.width, 0);
  });
});

test("at 1024 px under a finger the edge's button is 44 px wide, its ring whole", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 1024, height: 768 }, hasTouch: true });
  const page = await context.newPage();
  await installApi(page);
  await page.goto("/memories");
  await expect(page.locator("main h1").first()).toBeVisible();
  const button = await rect(page, ".assistant-edge-button");
  expect(button.width).toBe(44);
  expect(button.right).toBeLessThanOrEqual(1024);
  // The ring is drawn inside the 44 px button, so the 47 px column never cuts it.
  await launcher(page).focus();
  expect(await launcher(page).evaluate((el) => getComputedStyle(el).outlineOffset)).toBe("-2px");
  await context.close();
});
