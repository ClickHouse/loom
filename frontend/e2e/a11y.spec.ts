import { expect, test, type Page } from "@playwright/test";
import * as fs from "node:fs";
import { createRequire } from "node:module";
import { installApi } from "./fixtures";

// axe-core on the rendered dashboard: every route at desktop and phone width, and the shell's
// dialog and menu open. WCAG 2.0–2.2 A and AA rules; any violation fails, with its rule,
// impact and the first nodes it hit. Contrast is only measurable here, not in jsdom.
const AXE = fs.readFileSync(createRequire(import.meta.url).resolve("axe-core/axe.min.js"), "utf8");
const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

const ROUTES = [
  "/memories",
  "/recall",
  "/settings",
  "/repositories",
  "/flow",
  "/library",
  "/conversations",
  "/graph",
  "/spaces",
  "/profile",
  "/runs",
  "/tasks",
  "/admin/review_queue",
  "/admin/health",
  "/admin/guardrails",
  "/admin/policies",
  "/admin/activity",
  "/admin/usage",
  "/admin/agents",
  "/admin/users",
];

interface Violation {
  id: string;
  impact: string | null;
  nodes: string[];
}

async function violations(page: Page): Promise<Violation[]> {
  await page.addScriptTag({ content: AXE });
  return page.evaluate(async (tags) => {
    // The injected axe global; the specs carry no axe types.
    const axe = (
      window as unknown as {
        axe: {
          run: (
            ctx: Document,
            opts: object,
          ) => Promise<{
            violations: Array<{ id: string; impact: string | null; nodes: Array<{ target: string[] }> }>;
          }>;
        };
      }
    ).axe;
    const res = await axe.run(document, { runOnly: { type: "tag", values: tags } });
    return res.violations.map((v) => ({
      id: v.id,
      impact: v.impact,
      nodes: v.nodes.slice(0, 5).map((n) => n.target.join(" ")),
    }));
  }, TAGS);
}

async function open(page: Page, path: string) {
  await installApi(page, { memoryRows: 8, knowledgeRows: 4, runRows: 6 });
  await page.goto(path);
  await expect(page.locator("main h1").first()).toBeVisible();
  await page.waitForLoadState("networkidle"); // late rows, counts and badges
  await page.waitForTimeout(300); // their entry animation
}

for (const width of [1400, 375]) {
  test.describe(`${width} px`, () => {
    test.use({ viewport: { width, height: 900 } });
    for (const path of ROUTES) {
      test(`${path} has no WCAG A/AA violations`, async ({ page }) => {
        await open(page, path);
        expect(await violations(page)).toEqual([]);
      });
    }
  });
}

test("the Help dialog has no WCAG A/AA violations", async ({ page }) => {
  await open(page, "/memories");
  await page.keyboard.press("?");
  await expect(page.getByRole("dialog")).toBeVisible();
  expect(await violations(page)).toEqual([]);
});

test("the New menu has no WCAG A/AA violations", async ({ page }) => {
  await open(page, "/memories");
  await page.getByRole("button", { name: "New" }).click();
  await expect(page.getByRole("menu")).toBeVisible();
  expect(await violations(page)).toEqual([]);
});
