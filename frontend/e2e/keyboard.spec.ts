import { expect, test, type Page } from "@playwright/test";
import { installApi } from "./fixtures";

// Keys that close and keys that pass: Escape closes the profile editor and hands the focus
// back to its opener, and Escape on the memory list is left to the page when nothing is
// selected.

async function open(page: Page, path: string) {
  await installApi(page, { memoryRows: 8, knowledgeRows: 4, runRows: 6, profileRows: 3 });
  await page.goto(path);
  await expect(page.locator("main h1").first()).toBeVisible();
  await page.waitForLoadState("networkidle");
}

/** The name of the focused element, or "body". */
const focused = (page: Page) =>
  page.evaluate(() => {
    const el = document.activeElement as HTMLElement | null;
    if (!el || el === document.body) return "body";
    return el.getAttribute("aria-label") || el.textContent?.trim() || el.tagName;
  });

test("a profile editor takes the focus, Escape and Cancel close it, and the opener gets the focus back", async ({
  page,
}) => {
  await open(page, "/profile");
  const edit = page.getByRole("button", { name: /^Edit / }).first();
  const name = (await edit.getAttribute("aria-label")) ?? "";

  await edit.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("textbox", { name: "Section text" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("textbox", { name: "Section text" })).toHaveCount(0);
  await expect(page.getByRole("button", { name })).toBeFocused();

  await page.keyboard.press("Enter");
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("button", { name })).toBeFocused();

  await page.keyboard.press("Enter");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("button", { name })).toBeFocused();

  // Typed text is dropped only on a yes: "Keep editing" returns to the text, "Discard"
  // closes the editor and returns to the opener.
  await page.keyboard.press("Enter");
  const text = page.getByRole("textbox", { name: "Section text" });
  await text.pressSequentially(" and more");
  await page.keyboard.press("Escape");
  await page.getByRole("dialog", { name: "Discard changes?" }).getByRole("button", { name: "Keep editing" }).click();
  await expect(text).toBeFocused();
  await expect(text).toHaveValue(/^ and more/);
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Discard" }).click();
  await expect(text).toHaveCount(0);
  await expect(page.getByRole("button", { name })).toBeFocused();

  const add = page.getByRole("button", { name: "New section" });
  await add.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("textbox", { name: "Section label" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("textbox", { name: "Section label" })).toHaveCount(0);
  expect(await focused(page)).toBe("New section");
});

test("Escape on the memory list is left alone while nothing is selected", async ({ page }) => {
  await open(page, "/memories");
  const prevented = await page.evaluate(() => {
    const e = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    document.body.dispatchEvent(e);
    return e.defaultPrevented;
  });
  expect(prevented).toBe(false);
});
