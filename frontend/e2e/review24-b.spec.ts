import { expect, test, type Page } from "@playwright/test";
import { installApi } from "./fixtures";

// Graph's bar, the stat tiles, Settings' title, the account menu and the Spaces count:
// each one a case a review found broken.

async function open(page: Page, path: string) {
  await installApi(page, { memoryRows: 8, knowledgeRows: 4, runRows: 6 });
  await page.goto(path);
  await expect(page.locator("main h1").first()).toBeVisible();
  await page.waitForLoadState("networkidle");
}

test("at 375 px the graph's seed field has its own row and shows its whole placeholder", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 800 });
  await open(page, "/graph");
  const field = page.locator("#graph-seed");
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
  await open(page, "/graph");
  const path = page.getByRole("button", { name: "Path", exact: true });
  await expect(path).toHaveAttribute("aria-disabled", "true");
  await path.focus();
  await expect(path).toBeFocused();
  await expect(page.getByRole("tooltip")).toContainText("Type a second entity while a walk is open");
});

for (const width of [375, 1400]) {
  test(`at ${width} px the Sharing stat tiles share one shape and keep their figures inside`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await installApi(page);
    // Seven-figure totals, so a figure that does not fit its tile shows.
    await page.route("**/v1/me/sharing-summary", (route) =>
      route.fulfill({
        json: {
          subject: "",
          org: "",
          window_days: 7,
          totals: { writes: 1234567, recalls: 7654321, cross_namespace_recalls: 1000001, namespaces: 3, agents: 2 },
          namespaces: [],
          agents: [],
          agent_namespaces: [],
        },
      }),
    );
    await page.goto("/flow");
    const tiles = page.locator("[class*='big-stat_size_sm']");
    await expect(tiles).toHaveCount(4);
    const shapes = await tiles.evaluateAll((els) =>
      els.map((el) => {
        const s = getComputedStyle(el);
        const value = getComputedStyle(el.lastElementChild as Element);
        return {
          shape: `${s.paddingTop} ${s.borderTopLeftRadius} ${value.fontSize} ${value.fontWeight}`,
          overflows: el.scrollWidth > el.clientWidth,
        };
      }),
    );
    expect(new Set(shapes.map((s) => s.shape)).size).toBe(1);
    expect(shapes.filter((s) => s.overflows)).toEqual([]);
  });
}

test("Settings titles the document after its open tab", async ({ page }) => {
  await open(page, "/settings");
  await expect(page).toHaveTitle("Tokens · Settings · Loom");
  await page.getByRole("tab", { name: "Memory" }).click();
  await expect(page).toHaveTitle("Memory · Settings · Loom");
});

test("the account menu is named after who is signed in", async ({ page }) => {
  await open(page, "/memories");
  await page.getByRole("button", { name: /^Account: / }).click();
  await expect(page.getByRole("menu", { name: /^Signed in as .+, org / })).toBeVisible();
});

test("the Spaces count is a status message that stays mounted through a search", async ({ page }) => {
  await open(page, "/spaces");
  const status = page.locator("main [role='status']").filter({ hasText: /^\d+ namespaces?$/ });
  await expect(status).toHaveCount(1);
  const handle = await status.elementHandle();
  await page.getByRole("textbox", { name: "Search namespaces" }).fill("zzzz-no-such");
  await expect(status).toHaveText("0 namespaces");
  expect(await handle?.evaluate((el) => el.isConnected)).toBe(true);
});
