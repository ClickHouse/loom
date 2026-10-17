import { expect, test, type Page } from "@playwright/test";
import { violations } from "./axe";
import { CODE_NS, installApi, NAMESPACE } from "./fixtures";

// The header namespace picker with memory namespaces, code indexes and tracker records:
// the code and tracker groups fold, the open picker is axe-clean at desktop and phone
// width, and Up / Down rove over the options and the group buttons.

const row = (namespace: string, kind: string, role: string, description = "") => ({
  namespace,
  role,
  status: "active",
  visibility: "private",
  kind,
  description,
  joined_at: "2026-01-01T00:00:00Z",
  is_new: false,
});

const MINE = [
  row(NAMESPACE, "memory", "admin"),
  row("team-notes", "memory", "writer"),
  row("ops-runbooks", "memory", "viewer"),
  row(CODE_NS, "code", "jwt", "ClickHouse/ClickHouse @ master"),
  row("code-loom", "code", "jwt", "acme/loom @ main"),
  row("code-docs", "code", "jwt", "acme/docs @ main"),
  row("tracker-clickhouse", "qa", "viewer", "ClickHouse issues and PRs"),
];

async function openPicker(page: Page) {
  await installApi(page, { mine: MINE, claims: MINE.map((r) => r.namespace) });
  await page.goto("/memories");
  await expect(page.locator("main h1").first()).toBeVisible();
  await page.waitForLoadState("networkidle");
  await page.getByRole("button", { name: /^Namespace: / }).click();
  const dialog = page.getByRole("dialog", { name: "Namespaces" });
  await expect(dialog).toBeVisible();
  return dialog;
}

for (const width of [1400, 375]) {
  test(`the open picker with code and tracker groups is axe-clean at ${width} px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const dialog = await openPicker(page);
    const code = dialog.getByRole("button", { name: /^Code indexes/ });
    await expect(code).toHaveAttribute("aria-expanded", "false");
    await code.click();
    await expect(dialog.getByRole("button", { name: /ClickHouse\/ClickHouse @ master/ })).toBeVisible();
    expect(await violations(page)).toEqual([]);
  });
}

test("Up and Down rove from the search field through the options and the group buttons", async ({ page }) => {
  const dialog = await openPicker(page);
  const search = dialog.getByRole("textbox", { name: "Search namespaces" });
  await expect(search).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(dialog.getByRole("button", { name: new RegExp(`\\b${NAMESPACE}\\b`) })).toBeFocused();
  await page.keyboard.press("End");
  await expect(dialog.getByRole("button", { name: /^Tracker records/ })).toBeFocused();
  await page.keyboard.press("Enter");
  await page.keyboard.press("ArrowDown");
  await expect(dialog.getByRole("button", { name: /ClickHouse issues and PRs/ })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(page.getByRole("button", { name: /^Namespace: / })).toBeFocused();
});

test("a search opens the group it matches", async ({ page }) => {
  const dialog = await openPicker(page);
  await dialog.getByRole("textbox", { name: "Search namespaces" }).fill("loom");
  await expect(dialog.getByRole("button", { name: /^Code indexes/ })).toHaveAttribute("aria-expanded", "true");
  await expect(dialog.getByRole("button", { name: /acme\/loom @ main/ })).toBeVisible();
  await expect(dialog.getByRole("button", { name: /^Tracker records/ })).toHaveCount(0);
});
