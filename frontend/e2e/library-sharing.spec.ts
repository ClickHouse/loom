import { expect, test, type Page } from "@playwright/test";
import { boxOf, installApi } from "./fixtures";
import { violations } from "./axe";

// A collection's header says who can see it and offers Share to its admins, which opens the
// members dialog in place; closing it leaves the person in the Library. A viewer sees who
// can see it without Share, and My files say they are the person's alone.

const at = "2026-09-17T14:50:28Z";
const grant = (subject: string, role = "viewer") => ({
  subject,
  role,
  status: "active",
  granted_at: at,
  granted_by: "e2e@example.com",
});
const entry = (namespace: string, parent: string, role: string, title: string, personal = false) => ({
  namespace,
  parent,
  personal,
  role,
  description: "",
  title,
});
const COLLECTIONS = [
  entry("col-me-e2e", "", "admin", "", true),
  entry("col-eng", "", "admin", "Engineering"),
  entry("col-runbooks", "col-eng", "admin", "Runbooks"),
  entry("col-legal", "", "viewer", "Legal"),
];
const GRANTS: Record<string, ReturnType<typeof grant>[]> = {
  "col-eng": [grant("e2e@example.com", "admin"), grant("group:eng"), grant("dev@example.com", "writer")],
  "col-runbooks": [grant("e2e@example.com", "admin"), grant("sam@example.com", "writer")],
  "col-legal": [grant("lee@example.com", "admin"), grant("e2e@example.com")],
};

async function open(
  page: Page,
  collection: string,
  grants: (ns: string) => unknown = (ns) => GRANTS[ns] ?? [],
  viewer = false,
) {
  await installApi(page, { knowledgeRows: 4, collections: COLLECTIONS, viewer });
  await page.route("**/v1/grants.list", async (route) => {
    const ns = String((route.request().postDataJSON() as { namespace?: string }).namespace ?? "");
    const rows = grants(ns);
    if (rows === "unavailable") {
      await route.fulfill({ status: 503, json: { detail: { code: "UNAVAILABLE", message: "down" } } });
      return;
    }
    await route.fulfill({ json: { rows } });
  });
  await page.goto(`/library?collection=${collection}`);
}

const header = (page: Page) => page.getByRole("region", { name: "Who can see it" });
const button = (page: Page, name: string) => page.getByRole("main").getByRole("button", { name, exact: true });

test("Share opens the members dialog in place, and closing it leaves the person in the Library", async ({ page }) => {
  await open(page, "col-runbooks");
  await expect(header(page)).toHaveText(
    "You, group eng, sam@example.com and 1 more · 2 people here · Inherited from Engineering",
  );
  expect(await violations(page)).toEqual([]);
  await button(page, "Share").click();
  const dialog = page.getByRole("dialog", { name: "Members of Runbooks" });
  await expect(dialog.getByText("sam@example.com")).toBeVisible();
  // Who else can see it, through the collection it sits in: read-only, from the header's read.
  const through = dialog.getByRole("region", { name: "Also can see it, through Engineering" });
  await expect(through.getByText("group:eng")).toBeVisible();
  await expect(through.getByLabel(/^Role for /)).toHaveCount(0);
  expect(await violations(page)).toEqual([]);
  await dialog.getByRole("button", { name: "Close" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page).toHaveURL(/collection=col-runbooks/);
  await expect(header(page)).toBeVisible();
});

test("a viewer sees who can see it, and See who in place of Share", async ({ page }) => {
  await open(page, "col-legal", undefined, true);
  await expect(header(page)).toContainText("You and lee@example.com");
  await expect(button(page, "Share")).toHaveCount(0);
  await button(page, "See who").click();
  const dialog = page.getByRole("dialog", { name: "Members of Legal" });
  await expect(dialog.getByRole("button", { name: "Add member" })).toHaveAttribute("aria-disabled", "true");
});

test("My files say they are the person's alone, with no Share", async ({ page }) => {
  await open(page, "col-me-e2e");
  await expect(header(page)).toContainText("Only you");
  await expect(button(page, "Share")).toHaveCount(0);
  await expect(button(page, "See who")).toHaveCount(0);
});

test("a failed read says so, and Retry reads again", async ({ page }) => {
  let failed = false;
  await open(page, "col-eng", (ns) => {
    if (failed) return GRANTS[ns] ?? [];
    failed = true;
    return "unavailable";
  });
  await expect(header(page).getByRole("alert")).toContainText("Couldn't read who can see it");
  await header(page).getByRole("button", { name: "Retry" }).click();
  await expect(header(page)).toContainText("You, group eng and dev@example.com");
});

test.describe("375 px", () => {
  test.use({ viewport: { width: 375, height: 812 }, hasTouch: true, isMobile: true });

  test("the header says who can see it and keeps Share inside the page", async ({ page }) => {
    await open(page, "col-runbooks");
    await expect(header(page)).toContainText("You, group eng, sam@example.com and 1 more");
    const share = button(page, "Share");
    await expect(share).toBeVisible();
    const box = await boxOf(share);
    expect(box.x + box.width).toBeLessThanOrEqual(375);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(375);
  });
});
