import type { Page } from "@playwright/test";
import * as fs from "node:fs";
import { createRequire } from "node:module";

// axe-core on the rendered page: WCAG 2.0–2.2 A and AA rules, and two best-practice rules
// the dashboard holds too (a popover or sheet is a dialog with a name; headings follow one
// outline). Each violation carries its rule, impact and the first nodes it hit.
const AXE = fs.readFileSync(createRequire(import.meta.url).resolve("axe-core/axe.min.js"), "utf8");
const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

export interface Violation {
  id: string;
  impact: string | null;
  nodes: string[];
}

export async function violations(page: Page): Promise<Violation[]> {
  // The entry fade changes contrast while it runs: audit the settled page.
  await page.evaluate(async () => {
    const entering = document.querySelector(".animate-view-in");
    await Promise.allSettled(entering?.getAnimations().map((animation) => animation.finished) ?? []);
  });
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
    const wcag = await axe.run(document, { runOnly: { type: "tag", values: tags } });
    const names = await axe.run(document, { runOnly: { type: "rule", values: ["aria-dialog-name", "heading-order"] } });
    return [...wcag.violations, ...names.violations].map((v) => ({
      id: v.id,
      impact: v.impact,
      nodes: v.nodes.slice(0, 5).map((n) => n.target.join(" ")),
    }));
  }, TAGS);
}
