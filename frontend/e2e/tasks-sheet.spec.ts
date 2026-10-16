import { expect, test } from "@playwright/test";
import { installApi } from "./fixtures";

// The task detail at every width. Under 1180 px it is a modal sheet: a dialog that takes
// the focus, keeps it, closes on Escape (clearing ?task=) and hands the focus back to the
// row. Wider, it is the side panel, and Escape closes that too.

const TITLE = "Task 2: a regression the public bot found";

for (const width of [1000, 375]) {
  test(`at ${width} px the task opens as a dialog that Escape closes`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 });
    await installApi(page);
    await page.goto("/tasks");
    const row = page.getByRole("row", { name: TITLE });
    await row.focus();
    await page.keyboard.press("Enter");

    const dialog = page.getByRole("dialog", { name: TITLE });
    await expect(dialog).toBeVisible();
    await expect(dialog).toBeFocused();
    await expect(page).toHaveURL(/[?&]task=/);
    // The focus stays inside while the sheet is open.
    for (let i = 0; i < 12; i++) await page.keyboard.press("Tab");
    expect(await dialog.evaluate((d) => d.contains(document.activeElement))).toBe(true);

    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect(page).not.toHaveURL(/[?&]task=/);
    await expect(row).toBeFocused();
  });
}

test("at 1400 px the task opens beside the list and Escape closes it", async ({ page }) => {
  await page.setViewportSize({ width: 1400, height: 900 });
  await installApi(page);
  await page.goto("/tasks");
  const row = page.getByRole("row", { name: TITLE });
  await row.click();
  const panel = page.getByRole("complementary", { name: "Task detail" });
  await expect(panel).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.getByRole("button", { name: "More actions" }).focus();
  // The first Escape dismisses the focused button's tooltip (WCAG 1.4.13), the second the panel.
  await expect(page.getByRole("tooltip")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("tooltip")).toBeHidden();
  await expect(panel).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(panel).toBeHidden();
  await expect(page).not.toHaveURL(/[?&]task=/);
  await expect(row).toBeFocused();
});
