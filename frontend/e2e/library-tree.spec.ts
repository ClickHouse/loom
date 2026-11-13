import { expect, test, type Page } from "@playwright/test";
import { installApi } from "./fixtures";
import { violations } from "./axe";

// The Library's tree of places beside the documents, from 1280 px: it opens a place, shows
// and hides the collections inside one, makes one inline, and the collections inside the
// one shown are folder chips on its path's line.

const COLLECTIONS = [
  { namespace: "col-me-1a2b", parent: "", personal: true, role: "admin", description: "", title: "" },
  { namespace: "col-eng", parent: "", personal: false, role: "admin", description: "", title: "Engineering" },
  { namespace: "col-runbooks", parent: "col-eng", personal: false, role: "writer", description: "", title: "Runbooks" },
  { namespace: "col-rfcs", parent: "col-eng", personal: false, role: "writer", description: "", title: "RFCs" },
  { namespace: "col-legal", parent: "", personal: false, role: "viewer", description: "", title: "Legal" },
];

async function open(page: Page, path: string) {
  await installApi(page, { knowledgeRows: 4, collections: COLLECTIONS });
  await page.goto(path);
  await expect(page.locator("main h1").first()).toBeVisible();
}

test.describe("1440 px", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("the tree lists the places and opens one; the select steps aside", async ({ page }) => {
    await open(page, "/library");
    const tree = page.getByRole("navigation", { name: "Library places" });
    await expect(tree.getByRole("link", { name: "Namespace bench" })).toHaveAttribute("aria-current", "page");
    await expect(page.getByLabel("Show the documents of")).toHaveCount(0);
    await expect(page).toHaveTitle("Namespace bench · Library · Loom");
    await tree.getByRole("link", { name: "Engineering" }).click();
    await expect(page).toHaveURL(/collection=col-eng/);
    await expect(page).toHaveTitle("Engineering · Library · Loom");
    // Opening it shows what is inside, in the tree and as chips on the path's line.
    await expect(tree.getByRole("link", { name: "Runbooks" })).toBeVisible();
    const chips = page.getByRole("navigation", { name: "Collections in Engineering" });
    await expect(chips.getByRole("link")).toHaveText(["RFCs", "Runbooks"]);
    await chips.getByRole("link", { name: "Runbooks" }).click();
    await expect(page).toHaveURL(/collection=col-runbooks/);
    await expect(page.getByRole("navigation", { name: "Collection path" })).toContainText("Engineering");
    await expect(page.getByRole("navigation", { name: "Collection path" })).toContainText("Runbooks");
    await page.goBack();
    await expect(page).toHaveURL(/collection=col-eng/);
    expect(await violations(page)).toEqual([]);
  });

  test("a show button hides and shows the collections inside, and the browser keeps it", async ({ page }) => {
    await open(page, "/library");
    const tree = page.getByRole("navigation", { name: "Library places" });
    await expect(tree.getByRole("link", { name: "Runbooks" })).toHaveCount(0);
    // One name either way; aria-expanded says which.
    const toggle = tree.getByRole("button", { name: "Collections in Engineering", exact: true });
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(tree.getByRole("link", { name: "Runbooks" })).toBeVisible();
    await page.reload();
    await expect(tree.getByRole("link", { name: "Runbooks" })).toBeVisible();
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(tree.getByRole("link", { name: "Runbooks" })).toHaveCount(0);
  });

  test("My files names the browser tab after itself", async ({ page }) => {
    await open(page, "/library?collection=col-me-1a2b");
    await expect(
      page.getByRole("navigation", { name: "Library places" }).getByRole("link", { name: "My files" }),
    ).toHaveAttribute("aria-current", "page");
    await expect(page).toHaveTitle("My files · Library · Loom");
  });

  test("New collection makes one inline inside the collection shown, and opens it", async ({ page }) => {
    await installApi(page, { knowledgeRows: 2, collections: COLLECTIONS });
    const q4 = {
      namespace: "col-q4",
      parent: "col-eng",
      personal: false,
      role: "admin",
      description: "",
      title: "Q4 planning",
    };
    let asked: Record<string, unknown> | null = null;
    await page.route("**/v1/collection.create", async (route) => {
      asked = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({ json: { namespace: "col-q4", parent: "col-eng", created: true, title: "Q4 planning" } });
    });
    // The list names the new collection once it is made.
    await page.route("**/v1/collection.list", (route) =>
      route.fulfill({ json: { collections: asked ? [...COLLECTIONS, q4] : COLLECTIONS, next_cursor: null } }),
    );
    await page.goto("/library?collection=col-eng");
    const tree = page.getByRole("navigation", { name: "Library places" });
    await tree.getByRole("button", { name: "New collection" }).click();
    const field = page.getByRole("textbox", { name: "New collection in Engineering" });
    await expect(field).toBeFocused();
    expect(await violations(page)).toEqual([]);
    await field.fill("Q4 planning");
    await page.getByRole("button", { name: "Create" }).click();
    await expect.poll(() => asked).toMatchObject({ parent: "col-eng", title: "Q4 planning", derive_name: true });
    await expect(tree.getByRole("button", { name: "New collection" })).toBeFocused();
    await expect(page).toHaveURL(/collection=col-q4/);
    await expect(tree.getByRole("link", { name: "Q4 planning" })).toHaveAttribute("aria-current", "page");
    await expect(page).toHaveTitle("Q4 planning · Library · Loom");
  });
});

test.describe("1440 px under a reload bar", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("the tree's New collection stays in view", async ({ page }) => {
    await page.clock.install();
    const many = Array.from({ length: 40 }, (_, i) => ({
      namespace: `col-c${i}`,
      parent: "",
      personal: false,
      role: "admin",
      description: "",
      title: `Collection ${i}`,
    }));
    await installApi(page, { knowledgeRows: 2, collections: many });
    // The server's build changes after the page booted: the reload bar shows above the header.
    let build = "aaaaaaaaa1";
    await page.route("**/v1/health", (route) => route.fulfill({ json: { status: "ok", build_commit: build } }));
    await page.goto("/library");
    const tree = page.getByRole("navigation", { name: "Library places" });
    await expect(tree.getByRole("link", { name: "Collection 39" })).toBeAttached();
    await page.clock.runFor(10_000);
    build = "bbbbbbbbb2";
    await page.clock.runFor(6 * 60_000);
    await expect(page.getByTestId("reload-bar")).toBeVisible();
    await tree.evaluate((el) => {
      el.scrollTop = el.scrollHeight;
    });
    await expect(tree.getByRole("button", { name: "New collection" })).toBeInViewport({ ratio: 1 });
  });
});

test.describe("1100 px", () => {
  test.use({ viewport: { width: 1100, height: 800 } });

  test("New collection's dialog opens the collection it made", async ({ page }) => {
    await installApi(page, { knowledgeRows: 2, collections: COLLECTIONS });
    const q4 = { namespace: "col-q4", parent: "col-eng", personal: false, role: "admin", description: "", title: "Q4" };
    let made = false;
    await page.route("**/v1/collection.create", async (route) => {
      made = true;
      await route.fulfill({ json: { namespace: "col-q4", parent: "col-eng", created: true, title: "Q4" } });
    });
    await page.route("**/v1/collection.list", (route) =>
      route.fulfill({ json: { collections: made ? [...COLLECTIONS, q4] : COLLECTIONS, next_cursor: null } }),
    );
    await page.goto("/library?collection=col-eng");
    await page.getByRole("button", { name: "New collection" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByRole("textbox", { name: "Title" }).fill("Q4");
    await dialog.getByRole("button", { name: "Create" }).click();
    // The select names Q4 only once the list read after the create lists it.
    await expect(page.getByLabel("Show the documents of")).toContainText("Q4");
    await expect(page.getByRole("navigation", { name: "Collection path" })).toContainText("Engineering");
    // The list read before the create never sent the page back to the namespace.
    expect(page.url()).toContain("collection=col-q4");
    await expect(page.getByText(/is not listed yet/)).toHaveCount(0);
  });

  test("no tree: the select and the chips stand in", async ({ page }) => {
    await open(page, "/library?collection=col-eng");
    await expect(page.getByRole("navigation", { name: "Library places" })).toHaveCount(0);
    await expect(page.getByLabel("Show the documents of")).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Collections in Engineering" }).getByRole("link")).toHaveCount(2);
    // The chips list them: no second list below the documents.
    await expect(page.getByRole("region", { name: /Collections/ })).toHaveCount(0);
  });
});
