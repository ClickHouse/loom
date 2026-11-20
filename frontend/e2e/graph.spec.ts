import { expect, test, type Page } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { installApi, NAMESPACE } from "./fixtures";

// The Graph view's gesture model and hover invariants: nothing is read before a gesture,
// a deep link is one read, hovering every node shifts no layout and moves no canvas, Walk
// walks, Back returns. Its bar fits a phone, and Path says why it is unavailable.

const SHOTS = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "../e2e-screenshots");
const NODE = ".graph-node[data-id]";
const SVG = "svg.graph-svg";
const WALK_NODES = 201; // 200 nodes + the seed

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

test("opening /graph reads nothing until a gesture", async ({ page }) => {
  const calls = await installApi(page);
  const errors = collectErrors(page);
  await page.goto("/graph");
  await expect(page.getByRole("heading", { name: "Pick an entity to walk from" })).toBeVisible();
  // The starter chips in the canvas card come from the taxonomy; once they
  // are drawn the mount effects have run. (The rail lists the same entities,
  // so the chip is looked up inside the fixed-aspect card.)
  const card = page.locator('[class*="aspect-"]');
  await expect(card.getByRole("button", { name: "alpha", exact: true })).toBeVisible();
  expect(await card.getByRole("button").count()).toBeGreaterThanOrEqual(3);
  await expect(page.getByRole("button", { name: "Walk" })).toBeDisabled();
  await page.waitForTimeout(500);
  expect(calls.traverse).toHaveLength(0);
  expect(calls.search).toHaveLength(0);
  expect(calls.facts).toHaveLength(0);
  await shot(page, "01-empty-state");
  expect(errors).toEqual([]);
});

test("a ?seed= deep link walks exactly once, from its own seed", async ({ page }) => {
  const calls = await installApi(page);
  const errors = collectErrors(page);
  await page.goto("/graph?seed=alpha");
  await expect(page.locator(NODE)).toHaveCount(WALK_NODES);
  await page.waitForTimeout(500);
  expect(calls.traverse).toHaveLength(1);
  expect(calls.traverse[0]).toMatchObject({ seed_entity: "alpha", namespace: "bench", max_nodes: 200 });
  expect(calls.search).toHaveLength(0);
  await shot(page, "02-deep-link");
  expect(errors).toEqual([]);
});

test.describe("hovering every node", () => {
  // A height that leaves the page within a scrollbar's width of
  // overflowing: the geometry that exposes a hover flicker loop (1400×860, classic
  // scrollbars — see playwright.config.ts).
  test.use({ viewport: { width: 1400, height: 860 } });

  test("causes no layout shift and leaves the canvas where it was", async ({ page }) => {
    const calls = await installApi(page);
    const errors = collectErrors(page);
    await page.goto("/graph?seed=alpha");
    await expect(page.locator(NODE)).toHaveCount(WALK_NODES);
    await page.waitForTimeout(500);

    const before = await page.evaluate((svgSel) => {
      const svg = document.querySelector(svgSel);
      if (!svg) throw new Error(`no ${svgSel}`);
      const r = svg.getBoundingClientRect();
      const parent = svg.parentElement;
      if (!parent) throw new Error(`${svgSel} has no parent`);
      window.__ls = { entries: 0, value: 0 };
      window.__mut = 0;
      new PerformanceObserver((list) => {
        for (const e of list.getEntries()) {
          window.__ls.entries++;
          // lib.dom has no LayoutShift entry type; a layout-shift entry carries `value`.
          window.__ls.value += (e as PerformanceEntry & { value: number }).value;
        }
      }).observe({ type: "layout-shift", buffered: false });
      new MutationObserver((ms) => {
        window.__mut += ms.length;
      }).observe(parent, { attributes: true, childList: true, subtree: true, characterData: true });
      return { x: r.x, y: r.y, width: r.width, height: r.height };
    }, SVG);

    // The hit circle is the first circle of every node; its centre is the node's.
    const centres = await page.locator(NODE).evaluateAll((els) =>
      els.map((el) => {
        const circle = el.querySelector("circle");
        if (!circle) throw new Error("graph node without a circle");
        const r = circle.getBoundingClientRect();
        return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
      }),
    );
    expect(centres).toHaveLength(WALK_NODES);

    let maxShift = 0;
    let hoverSeen = 0;
    let hotSeen = 0;
    let readoutSeen = 0;
    for (let i = 0; i < centres.length; i++) {
      await page.mouse.move(centres[i].x, centres[i].y, { steps: 2 });
      const s = await page.evaluate(
        ([svgSel, b0]) => {
          const svg = document.querySelector(svgSel);
          if (!svg) throw new Error(`no ${svgSel}`);
          const r = svg.getBoundingClientRect();
          const readout = document.querySelector<HTMLElement>(".graph-readout");
          return {
            shift:
              Math.abs(r.x - b0.x) +
              Math.abs(r.y - b0.y) +
              Math.abs(r.width - b0.width) +
              Math.abs(r.height - b0.height),
            hover: svg.hasAttribute("data-hover"),
            hot: svg.querySelectorAll("[data-hot]").length,
            readout: !!readout && !readout.hidden && readout.textContent !== "",
          };
        },
        [SVG, before] as const,
      );
      maxShift = Math.max(maxShift, s.shift);
      if (s.hover) hoverSeen++;
      if (s.hot > 0) hotSeen++;
      if (s.readout) readoutSeen++;
      if (i === 60) await shot(page, "03-hover-readout");
    }
    await page.mouse.move(5, 5);
    await page.waitForTimeout(300);

    const after = await page.evaluate((svgSel) => {
      const svg = document.querySelector(svgSel);
      if (!svg) throw new Error(`no ${svgSel}`);
      const r = svg.getBoundingClientRect();
      const readout = document.querySelector<HTMLElement>(".graph-readout");
      return {
        rect: { x: r.x, y: r.y, width: r.width, height: r.height },
        layoutShifts: window.__ls,
        mutations: window.__mut,
        hover: svg.hasAttribute("data-hover"),
        hot: svg.querySelectorAll("[data-hot]").length,
        readoutHidden: readout ? readout.hidden : null,
      };
    }, SVG);

    const measured = { hovered: centres.length, hoverSeen, hotSeen, readoutSeen, maxShift, ...after };
    test.info().annotations.push({ type: "hover-pass", description: JSON.stringify(measured) });
    console.log(`hover-pass ${JSON.stringify(measured)}`);

    // The two invariants: zero layout-shift entries and an unmoved canvas.
    expect(after.layoutShifts.entries).toBe(0);
    expect(after.layoutShifts.value).toBe(0);
    expect(maxShift).toBe(0);
    expect(after.rect).toEqual(before);
    // The hover was real: the svg carried data-hover, a lit chain and a
    // filled readout for every one of the 200 non-seed nodes (the seed
    // lights nothing), and left clean.
    expect(hoverSeen).toBe(WALK_NODES - 1);
    expect(hotSeen).toBe(WALK_NODES - 1);
    expect(readoutSeen).toBe(WALK_NODES - 1);
    expect(after.hover).toBe(false);
    expect(after.hot).toBe(0);
    expect(after.readoutHidden).toBe(true);
    // Hovering reads nothing.
    expect(calls.traverse).toHaveLength(1);
    expect(calls.search).toHaveLength(0);
    await shot(page, "04-after-hover");
    expect(errors).toEqual([]);
  });
});

test("the Walk button walks, and Back restores the previous seed", async ({ page }) => {
  const calls = await installApi(page);
  const errors = collectErrors(page);
  await page.goto("/graph?seed=alpha");
  await expect(page.locator(NODE)).toHaveCount(WALK_NODES);
  await expect.poll(() => calls.traverse.length).toBe(1);

  const seedInput = page.getByLabel("Seed entity");
  await seedInput.fill("beta");
  await expect(page.getByRole("button", { name: "Walk" })).toBeEnabled();
  await page.getByRole("button", { name: "Walk" }).click();
  await expect.poll(() => calls.traverse.length).toBe(2);
  expect(calls.traverse[1]).toMatchObject({ seed_entity: "beta" });
  await expect(page).toHaveURL(new RegExp(`/graph\\?namespace=${NAMESPACE}&seed=beta$`));
  await expect(page.locator(`${NODE}[data-id="s"]`)).toHaveText("beta");
  await expect(page.locator(NODE)).toHaveCount(WALK_NODES);
  await shot(page, "05-walked-to-beta");

  await page.goBack();
  await expect(page).toHaveURL(new RegExp(`/graph\\?namespace=${NAMESPACE}&seed=alpha$`));
  await expect.poll(() => calls.traverse.length).toBe(3);
  expect(calls.traverse[2]).toMatchObject({ seed_entity: "alpha" });
  await expect(seedInput).toHaveValue("alpha");
  await expect(page.locator(`${NODE}[data-id="s"]`)).toHaveText("alpha");
  await expect(page.locator(NODE)).toHaveCount(WALK_NODES);
  await shot(page, "06-back-to-alpha");
  expect(errors).toEqual([]);
});

// The Explorer's own entity box is the first input Tab reaches in the page, after the
// header's search (which stays in the tab order: Tab is the keyboard's only way to it),
// and the Facets pane clips no heading: every section heading is one line whose hint is a
// tooltip, never an ellipsis.
test("the entity box is the page's first input in the tab order and no facet heading truncates", async ({ page }) => {
  await installApi(page);
  const errors = collectErrors(page);
  await page.goto("/graph?seed=alpha");
  await expect(page.locator(NODE)).toHaveCount(WALK_NODES);

  let firstInput: { tag: string; label: string | null } | null = null;
  for (let i = 0; i < 40 && !firstInput; i++) {
    await page.keyboard.press("Tab");
    firstInput = await page.evaluate(() => {
      const el = document.activeElement;
      if (!(el instanceof HTMLElement) || (el.tagName !== "INPUT" && el.tagName !== "TEXTAREA")) return null;
      if (!el.closest("main")) return null;
      return { tag: el.tagName, label: el.getAttribute("aria-label") };
    });
  }
  expect(firstInput).not.toBeNull();
  if (!firstInput) throw new Error("no text input reached by Tab");
  expect(firstInput.label).toContain("Seed entity");
  await expect(page.getByLabel("Search Loom")).not.toHaveAttribute("tabindex", "-1");

  const facets = page.getByRole("complementary", { name: "Facets" });
  const headings = facets.getByRole("heading");
  expect(await headings.count()).toBeGreaterThanOrEqual(5);
  // A heading is not truncated when its scroll width fits its box.
  const clipped = await headings.evaluateAll((els) =>
    els
      .filter((el) => el.scrollWidth > el.clientWidth + 1 || /\btruncate\b/.test(el.className))
      .map((el) => el.textContent),
  );
  expect(clipped).toEqual([]);
  // The hint is a tooltip on a focusable (i), reachable by keyboard.
  await facets.getByRole("button", { name: "About Relations" }).focus();
  await expect(page.getByRole("tooltip")).toContainText("Relations on this walk's typed edges");
  await shot(page, "07-tab-order-and-facets");
  expect(errors).toEqual([]);
});

test("at 375 px the graph's seed field has its own row and shows its whole placeholder", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 800 });
  await installApi(page);
  await page.goto("/graph");
  const field = page.locator("#graph-seed");
  await expect(field).toBeVisible();
  const fit = await field.evaluate((input: HTMLInputElement) => {
    const form = input.closest("form") as HTMLFormElement;
    const style = getComputedStyle(input);
    const ctx = document.createElement("canvas").getContext("2d") as CanvasRenderingContext2D;
    ctx.font = `${style.fontSize} ${style.fontFamily}`;
    const text = ctx.measureText(input.placeholder).width;
    const pad = Number.parseFloat(style.paddingLeft) + Number.parseFloat(style.paddingRight);
    return {
      share: input.closest(".relative")!.getBoundingClientRect().width / form.getBoundingClientRect().width,
      placeholderFits: text + pad <= input.clientWidth,
    };
  });
  expect(fit.share).toBeGreaterThan(0.95);
  expect(fit.placeholderFits).toBe(true);
});

test("Path is reachable while unavailable and says why", async ({ page }) => {
  await installApi(page);
  await page.goto("/graph");
  const path = page.getByRole("button", { name: "Path", exact: true });
  await expect(path).toHaveAttribute("aria-disabled", "true");
  await path.focus();
  await expect(path).toBeFocused();
  await expect(page.getByRole("tooltip")).toContainText("Type a second entity while a walk is open");
});
