import { expect, test } from "@playwright/test";
import { violations } from "./axe";
import { installApi, NAMESPACE } from "./fixtures";

// A link names its namespace (`?namespace=`). One the account can read opens for the tab,
// and the saved pick stays; one it cannot read opens a page that says so, axe-clean, with
// the picker in the header; Continue opens the page on the namespace the picker shows.

const JOINED = {
  namespace: "t-acme-shared",
  role: "writer",
  status: "active",
  visibility: "private",
  kind: "memory",
  description: "Acme",
  joined_at: "2026-01-02T00:00:00Z",
  is_new: false,
};
const OWN = { ...JOINED, namespace: NAMESPACE, role: "admin", description: "" };

test("a link to a namespace the account can read opens it, and the address keeps naming it", async ({ page }) => {
  await installApi(page, { mine: [OWN, JOINED] });
  await page.goto(`/memories?namespace=${JOINED.namespace}`);
  await expect(page.getByRole("button", { name: `Namespace: ${JOINED.namespace}` })).toBeVisible();
  await expect(page.locator("main h1").first()).toHaveText("Memories");
  await expect(page).toHaveURL(new RegExp(`/memories\\?namespace=${JOINED.namespace}$`));
  expect(await page.evaluate(() => localStorage.getItem("loom_namespace_v1"))).toBe(NAMESPACE);
});

for (const width of [1400, 375]) {
  test(`a link to a namespace the account cannot read says so, axe-clean at ${width} px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await installApi(page);
    await page.goto("/memories?namespace=t-other-shared");
    await expect(page.getByRole("heading", { level: 1, name: "No access to t-other-shared" })).toBeVisible();
    await expect(page.getByRole("button", { name: `Namespace: ${NAMESPACE}` })).toBeVisible();
    expect(await violations(page)).toEqual([]);
  });
}

test("Continue opens the page on the namespace the picker shows, with the focus on its heading", async ({ page }) => {
  await installApi(page);
  await page.goto("/memories?namespace=t-other-shared");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.locator("main h1").first()).toHaveText("Memories");
  await expect(page.locator("main h1").first()).toBeFocused();
  await expect(page).toHaveURL(new RegExp(`/memories\\?namespace=${NAMESPACE}$`));
});
