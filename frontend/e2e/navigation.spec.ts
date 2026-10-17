import { expect, test, type Page } from "@playwright/test";
import { installApi } from "./fixtures";

// The sidebar, the phone tabs and the account menu: the toggle keeps the focus, the
// collapsed rail reads its counts, the current tab is marked, and Settings titles the
// document after its tab.

async function open(page: Page, path: string) {
  await installApi(page, { memoryRows: 8, knowledgeRows: 4, runRows: 6, profileRows: 3 });
  await page.goto(path);
  await expect(page.locator("main h1").first()).toBeVisible();
  await page.waitForLoadState("networkidle");
}

test("the sidebar toggle keeps the focus when Enter collapses and expands the rail", async ({ page }) => {
  await open(page, "/memories");
  const toggle = page.getByRole("button", { name: "Collapse sidebar" });
  await toggle.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("button", { name: "Expand sidebar" })).toBeFocused();
  await expect(page.getByRole("button", { name: "Expand sidebar" })).toHaveAttribute("aria-expanded", "false");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("button", { name: "Collapse sidebar" })).toBeFocused();
  await expect(page.getByRole("button", { name: "Collapse sidebar" })).toHaveAttribute("aria-expanded", "true");
});

test("the collapsed rail reads the review-queue and agents counts in the link names", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("loom_sidebar_collapsed", "1"));
  await installApi(page);
  await page.route("**/v1/conflicts.list", (r) => r.fulfill({ json: { rows: [], total: 3 } }));
  await page.route("**/v1/agents.connected", (r) => r.fulfill({ json: { new_count: 2 } }));
  await page.goto("/memories");
  await expect(page.getByRole("link", { name: "Review queue, 3" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Agents, 2" })).toBeVisible();
});

test("the account menu is named after who is signed in", async ({ page }) => {
  await open(page, "/memories");
  await page.getByRole("button", { name: /^Account: / }).click();
  await expect(page.getByRole("menu", { name: /^Signed in as .+, org / })).toBeVisible();
});

test("Settings titles the document after its open tab", async ({ page }) => {
  await open(page, "/settings");
  await expect(page).toHaveTitle("Tokens · Settings · Loom");
  await page.getByRole("tab", { name: "Memory" }).click();
  await expect(page).toHaveTitle("Memory · Settings · Loom");
});

test.describe("375 px", () => {
  test.use({ viewport: { width: 375, height: 812 } });

  test("the current phone tab has the rail's selected fill", async ({ page }) => {
    await open(page, "/memories");
    const fills = await page.evaluate(() => {
      const tab = document.querySelector<HTMLElement>("nav[aria-label='Sections'] a[aria-current='page']");
      const probe = document.createElement("div");
      probe.style.background = "var(--click-sidebar-main-navigation-item-color-background-active)";
      document.body.append(probe);
      const want = getComputedStyle(probe).backgroundColor;
      probe.remove();
      return { tab: tab ? getComputedStyle(tab).backgroundColor : "", want };
    });
    expect(fills.tab).toBe(fills.want);
  });
});
