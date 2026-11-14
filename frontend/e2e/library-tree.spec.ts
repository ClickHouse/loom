import { expect, test, type Page } from "@playwright/test";
import { installApi } from "./fixtures";
import { violations } from "./axe";

// The Library's tree of places beside the documents, where the page has room: it opens a
// place, shows and hides the collections inside one, makes one inline, and the collections
// inside the one shown are folder chips on its path's line.

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
    const chips = page.getByRole("navigation", { name: "Inside Engineering" });
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

// The show/hide button and a row's empty box are one size, so the titles line up: 24 px for
// a mouse (WCAG 2.5.8's smallest target), 44 px for a thumb.
for (const [pointer, touch, side] of [
  ["a mouse", false, 24],
  ["touch", true, 44],
] as const) {
  test.describe(`1440 px with ${pointer}`, () => {
    test.use({ viewport: { width: 1440, height: 900 }, hasTouch: touch, isMobile: touch });

    test(`the show button and a row's empty box are ${side} px`, async ({ page }) => {
      await open(page, "/library");
      const tree = page.getByRole("navigation", { name: "Library places" });
      const toggle = tree.getByRole("button", { name: "Collections in Engineering", exact: true });
      const box = async (l: ReturnType<Page["locator"]>) => {
        const b = await l.boundingBox();
        return b && [Math.round(b.width), Math.round(b.height)];
      };
      expect(await box(toggle)).toEqual([side, side]);
      expect(await box(tree.locator(".library-tree-twist-space").first())).toEqual([side, side]);
    });
  });
}

// A folder upload nests eight deep: past the fourth level rows share its indent, so the
// deepest title keeps room (139 px measured at 1440 px).
const DEEP = Array.from({ length: 9 }, (_, i) => ({
  namespace: `col-d${i}`,
  parent: i === 0 ? "" : `col-d${i - 1}`,
  personal: false,
  role: "admin",
  description: "",
  title: `Quarterly planning notes ${i}`,
}));

test.describe("1440 px, eight deep", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("the deepest row keeps room for its title", async ({ page }) => {
    await installApi(page, { knowledgeRows: 2, collections: DEEP });
    await page.goto("/library?collection=col-d8");
    const link = page
      .getByRole("navigation", { name: "Library places" })
      .getByRole("link", { name: "Quarterly planning notes 8" });
    await expect(link).toBeVisible();
    expect((await link.boundingBox())?.width ?? 0).toBeGreaterThanOrEqual(100);
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
    await expect(tree.getByRole("link", { name: "Collection 0", exact: true })).toBeAttached();
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

test.describe("1280 px", () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test("the tree is drawn under a classic scrollbar on a long list", async ({ page }) => {
    await installApi(page, { knowledgeRows: 40, collections: COLLECTIONS });
    // A classic 16 px scrollbar on #main, which takes width from it.
    await page.addInitScript(() => {
      document.addEventListener("DOMContentLoaded", () => {
        const style = document.createElement("style");
        style.textContent = "#main::-webkit-scrollbar { width: 16px; }";
        document.head.append(style);
      });
    });
    await page.goto("/library");
    const tree = page.getByRole("navigation", { name: "Library places" });
    await expect(tree).toBeVisible();
    // The list scrolls, so <main>'s scrollbar takes some of its width; the tree stays.
    await expect
      .poll(() =>
        page.evaluate(() => {
          const main = document.getElementById("main")!;
          return main.clientWidth < main.getBoundingClientRect().width;
        }),
      )
      .toBe(true);
    await expect(tree).toBeVisible();
    await expect(page.getByLabel("Show the documents of")).toHaveCount(0);
    // Beside the tree the search keeps its room (277 px drawn).
    expect((await page.getByLabel("Search library").boundingBox())?.width ?? 0).toBeGreaterThanOrEqual(250);
  });
});

// Ask AI beside the page narrows <main>: the tree shows only where the page keeps the width
// it has at 1280 px without the panel.
for (const [width, tree, search] of [
  // The search field's least width, a little under what each draws.
  [1280, false, 120],
  [1440, false, 250],
  [1700, true, 250],
] as const) {
  test.describe(`${width} px with Ask AI beside the page`, () => {
    test.use({ viewport: { width, height: 900 } });

    test(`the tree is ${tree ? "" : "not "}drawn, and the search keeps its room`, async ({ page }) => {
      await page.addInitScript(() =>
        localStorage.setItem("loom_assistant_panel", JSON.stringify({ open: true, width: 420 })),
      );
      await open(page, "/library?collection=col-eng");
      await expect(page.locator("[data-assistant-panel][data-open]")).toBeVisible();
      await expect(page.getByRole("navigation", { name: "Library places" })).toHaveCount(tree ? 1 : 0);
      await expect(page.getByLabel("Show the documents of")).toHaveCount(tree ? 0 : 1);
      await expect
        .poll(async () => (await page.getByLabel("Search library").boundingBox())?.width ?? 0)
        .toBeGreaterThanOrEqual(search);
    });
  });
}

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

  test("a wider window draws the tree and hands it the picker's focus", async ({ page }) => {
    await open(page, "/library?collection=col-runbooks");
    const picker = page.getByLabel("Show the documents of");
    await expect(picker).toContainText("Runbooks");
    // The list open, its search field (portalled outside <main>) holding the focus.
    await picker.click();
    await expect(
      page.getByRole("dialog", { name: "Options" }).getByRole("textbox", { name: "Filter options" }),
    ).toBeFocused();
    await page.setViewportSize({ width: 1440, height: 900 });
    const tree = page.getByRole("navigation", { name: "Library places" });
    await expect(tree.getByRole("link", { name: "Runbooks" })).toBeFocused();
  });

  test("no tree: the select and the chips stand in", async ({ page }) => {
    await open(page, "/library?collection=col-eng");
    await expect(page.getByRole("navigation", { name: "Library places" })).toHaveCount(0);
    await expect(page.getByLabel("Show the documents of")).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Inside Engineering" }).getByRole("link")).toHaveCount(2);
    // The chips list them: no second list below the documents.
    await expect(page.getByRole("region", { name: /Collections/ })).toHaveCount(0);
  });
});
