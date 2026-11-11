import { expect, test } from "@playwright/test";
import { installApi, ROUTES } from "./fixtures";

// The shell scrolls only its content: the header, sidebar and phone nav stay put, so the
// document itself never scrolls, on any route, at either width. Each route's title and
// tab rows are checked here too, on the same render, and the heading type below. On
// /assistant the page is the assistant panel, in <main>'s place.
const PAGE = "main:not([hidden]), [data-assistant-panel][data-mode='expanded']";
for (const width of [1400, 375]) {
  test.describe(`${width} px`, () => {
    test.use({ viewport: { width, height: 900 } });
    for (const path of ROUTES) {
      test(`${path} scrolls only its content`, async ({ page }) => {
        await installApi(page, { memoryRows: 8, knowledgeRows: 4, runRows: 6 });
        await page.goto(path);
        await expect(page.locator(PAGE).locator("h1").first()).toBeVisible();
        await page.waitForLoadState("networkidle");
        const doc = await page.evaluate(() => ({
          height: document.documentElement.scrollHeight - window.innerHeight,
          width: document.documentElement.scrollWidth - window.innerWidth,
        }));
        expect(doc).toEqual({ height: 0, width: 0 });
        // Each page titles the document after itself (WCAG 2.4.2), naming its tab when a
        // page has several, so no two routes share a title.
        const h1 = (await page.locator(PAGE).locator("h1").first().textContent())?.trim();
        // A page's tab is a link (aria-current) or a Click UI tab (aria-selected).
        const current = page.locator("main [aria-current='page'], main [role='tab'][aria-selected='true']");
        const tab = (await current.count()) > 0 ? (await current.first().textContent())?.trim() : undefined;
        await expect(page).toHaveTitle(tab && tab !== h1 ? `${tab} · ${h1} · Loom` : `${h1} · Loom`);
        // Click UI draws an icon, a badge and an ellipsis as a <div>: none sits in a <p>.
        const blocksInText = await page.evaluate(() =>
          [...document.querySelectorAll("p div")].map((d) => d.closest("p")?.textContent?.slice(0, 60)),
        );
        expect(blocksInText).toEqual([]);
        // Tab labels keep to one line; a tab row wider than the page scrolls in its own box.
        const tabs = await page.evaluate(() =>
          [...document.querySelectorAll("main [role='tablist']")].map((list) => ({
            inside: list.getBoundingClientRect().right <= window.innerWidth,
            wrapped: [...list.querySelectorAll("[role='tab']")]
              .filter((tab) => {
                // A label that breaks has more than one line box: more than one rect for its text.
                const text = document.createTreeWalker(tab, NodeFilter.SHOW_TEXT);
                for (let node = text.nextNode(); node; node = text.nextNode()) {
                  const range = document.createRange();
                  range.selectNodeContents(node);
                  if ([...range.getClientRects()].filter((r) => r.width > 0).length > 1) return true;
                }
                return false;
              })
              .map((tab) => tab.textContent),
          })),
        );
        for (const t of tabs) expect(t).toEqual({ inside: true, wrapped: [] });
        // Every page is titled by PageHeader: a 20 px h1 with no icon.
        const title = await page
          .locator(PAGE)
          .locator("h1")
          .first()
          .evaluate((h) => ({
            header: !!h.closest("[data-page-header]"),
            icon: !!h.querySelector("svg"),
            size: getComputedStyle(h).fontSize,
          }));
        expect(title).toEqual({ header: true, icon: false, size: "20px" });
      });
    }
  });
}

test("headings use Click UI's title type, the xl step at 27 px", async ({ page }) => {
  await installApi(page);
  await page.goto("/memories");
  await expect(page.locator("main h1").first()).toBeVisible();
  const type = await page.evaluate(() => {
    const out: Record<string, string> = {};
    for (const tag of ["h1", "h2", "h3"]) {
      const h = document.createElement(tag);
      h.textContent = "x";
      document.querySelector("main")?.append(h);
      const cs = getComputedStyle(h);
      out[tag] = `${cs.fontWeight} ${cs.fontSize}/${cs.lineHeight}`;
    }
    out.xl = getComputedStyle(document.documentElement).getPropertyValue("--text-xl--line-height").trim();
    return out;
  });
  expect(type).toEqual({ h1: "700 20px/30px", h2: "700 18px/27px", h3: "600 16px/24px", xl: "27px" });
});
