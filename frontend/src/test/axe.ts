import axe from "axe-core";

/**
 * Run axe-core over a rendered subtree and return every violation as one
 * line (`rule (impact): help — target`), so a failing assertion names the
 * rule and the element instead of printing a results object.
 *
 * `color-contrast` is off: jsdom loads no stylesheet, so every colour is
 * the default and the check would measure nothing. Contrast is a browser
 * question and belongs to the Playwright job.
 */
export async function axeViolations(root: Element): Promise<string[]> {
  const results = await axe.run(root, {
    rules: { "color-contrast": { enabled: false } },
  });
  return results.violations.map(
    (v) => `${v.id} (${v.impact ?? "n/a"}): ${v.help} — ${v.nodes.map((n) => JSON.stringify(n.target)).join(", ")}`,
  );
}

/**
 * Every `<input>`, `<select>` and `<textarea>` under `root` that has no
 * accessible name: no `aria-label`, no `aria-labelledby`, no `<label htmlFor>` pointing
 * at its id, no wrapping `<label>`. Hidden inputs are skipped. Returned as
 * `tag#id[placeholder]` so the failure names the control. axe's `label` rule catches the
 * same thing; this is the explicit assertion so the test's intent is readable and the
 * failure list is short.
 */
export function unlabeledControls(root: ParentNode): string[] {
  const out: string[] = [];
  root.querySelectorAll<HTMLElement>("input, select, textarea").forEach((el) => {
    if (el instanceof HTMLInputElement && el.type === "hidden") return;
    // Radix's form bubble beside a Checkbox, RadioGroup or Switch: hidden from assistive tech.
    if (el.getAttribute("aria-hidden") === "true" && el.tabIndex === -1) return;
    if (el.getAttribute("aria-label")?.trim()) return;
    if (el.getAttribute("aria-labelledby")?.trim()) return;
    if (el.id && root.querySelector(`label[for="${el.id}"]`)) return;
    if (el.closest("label")) return;
    out.push(`${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ""}[${el.getAttribute("placeholder") ?? ""}]`);
  });
  return out;
}
