import { expect, test } from "@playwright/test";
import { installApi } from "./fixtures";

// The Sharing page's stat tiles share one shape and keep seven-figure totals inside.

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
          // Three namespaces, so the namespace tile is drawn too.
          namespaces: ["team", "ops", "notes"].map((namespace) => ({
            namespace,
            role: "admin",
            visibility: "private",
            member_count: 1,
            writes_7d: 1,
            recalls_7d: 1,
          })),
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
