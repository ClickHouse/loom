import { expect, test, type Page, type Route } from "@playwright/test";
import { installApi } from "./fixtures";
import { violations } from "./axe";

// All documents at the top of the Library's places: one list of every place the person
// reads, each row linking to where it lives; what the read left out said above the rows
// (a place it could not read, a server that reads one place per call); and the search
// scoped to the collection shown or everywhere.

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
  entry("col-runbooks", "col-eng", "writer", "Runbooks"),
  entry("col-legal", "", "viewer", "Legal"),
];

const doc = (namespace: string, source_uri: string, title: string, hoursAgo: number) => ({
  namespace,
  source_uri,
  title,
  description: "",
  tags: ["runbook"],
  ingested_at: new Date(Date.now() - hoursAgo * 3_600_000).toISOString(),
  chunk_count: 4,
  superseded: false,
  has_blob: true,
  blob_size: 18_000,
  blob_content_type: "text/markdown",
  original_filename: `${source_uri.split("/").at(-1)}`,
});
const DOCS = [
  doc("col-runbooks", "runbooks/drain-stalled-lane.md", "Drain a stalled ingest lane", 2),
  doc("bench", "notes/vector-index.md", "Notes on the vector index", 5),
  doc("col-eng", "rfcs/storage-tiering.md", "Storage tiering RFC", 26),
  doc("col-me-e2e", "me/first-week.md", "Onboarding: the first week", 50),
  doc("col-legal", "legal/msa-template.md", "Services agreement template", 80),
];

type ListBody = { namespace: string; namespaces?: string[] };

/** Answer `knowledge.list` with the documents of the places a call names; `refuse` names
 *  places that answer 403, `oneAtATime` plays a server that ignores `namespaces`. */
async function listDocs(page: Page, opts: { refuse?: string[]; oneAtATime?: boolean } = {}) {
  await page.route("**/v1/knowledge.list", async (route: Route) => {
    const body = route.request().postDataJSON() as ListBody;
    const places = opts.oneAtATime ? [body.namespace] : [body.namespace, ...(body.namespaces ?? [])];
    if (places.some((p) => opts.refuse?.includes(p))) {
      await route.fulfill({
        status: 403,
        json: { detail: { code: "AUTH_INSUFFICIENT", message: "not a reader of this namespace" } },
      });
      return;
    }
    const rows = DOCS.filter((d) => places.includes(d.namespace)).map((d) =>
      opts.oneAtATime ? { ...d, namespace: undefined } : d,
    );
    await route.fulfill({
      json: {
        rows,
        next_cursor: null,
        ...(opts.oneAtATime && body.namespaces?.length ? { ignored_args: ["namespaces"] } : {}),
      },
    });
  });
}

async function open(page: Page, path: string, opts: Parameters<typeof listDocs>[1] = {}) {
  await installApi(page, { knowledgeRows: 4, collections: COLLECTIONS });
  await listDocs(page, opts);
  await page.goto(path);
  await expect(page.locator("main h1").first()).toBeVisible();
}

const tree = (page: Page) => page.getByRole("navigation", { name: "Library places" });
const row = (page: Page, title: string) => page.locator("main [data-doc-row]").filter({ hasText: title });

test.describe("1440 px", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("All documents tops the tree and lists every place's documents, each linked to its place", async ({ page }) => {
    await open(page, "/library");
    await tree(page).getByRole("link", { name: "All documents" }).click();
    await expect(page).toHaveURL(/place=all/);
    await expect(page).toHaveTitle("All documents · Library · Loom");
    await expect(tree(page).getByRole("link", { name: "All documents" })).toHaveAttribute("aria-current", "page");
    await expect(page.locator("main [data-doc-row]")).toHaveCount(5);
    await expect(page.getByText(/5 documents · 20 chunks · .* · 5 places/)).toBeVisible();
    expect(await violations(page)).toEqual([]);
    // A viewer's row takes no selection; a writer's does.
    await expect(row(page, "Services agreement template").getByRole("checkbox")).toHaveCount(0);
    await expect(row(page, "Drain a stalled ingest lane").getByRole("checkbox")).toHaveCount(1);
    await row(page, "Drain a stalled ingest lane").getByRole("link", { name: "Runbooks" }).click();
    await expect(page).toHaveURL(/collection=col-runbooks/);
    // The collection is the place now: All documents leaves the address.
    expect(new URL(page.url()).searchParams.get("place")).toBeNull();
    await page.goBack();
    await expect(page).toHaveURL(/place=all/);
  });

  test("a place the read is refused is named, and the rest are shown", async ({ page }) => {
    await open(page, "/library?place=all", { refuse: ["col-legal"] });
    await expect(page.getByText("Not shown, as you can't read it: Legal.")).toBeVisible();
    await expect(page.locator("main [data-doc-row]")).toHaveCount(4);
    await expect(page.getByText(/4 of 5 places/)).toBeVisible();
  });

  test("a server that reads one place per call is said to show only the first", async ({ page }) => {
    await open(page, "/library?place=all", { oneAtATime: true });
    await expect(page.getByText(/^Only Namespace bench is shown: this server lists one place per read/)).toBeVisible();
    await expect(page.locator("main [data-doc-row]")).toHaveCount(1);
    await expect(page.getByText(/1 of 5 places/)).toBeVisible();
  });

  test("a collection's search reads it alone, or every place once Everywhere is picked", async ({ page }) => {
    await open(page, "/library?collection=col-eng");
    await expect(page.getByRole("group", { name: "Search in" })).toHaveCount(0);
    await page.getByLabel("Search library").fill("the");
    const scope = page.getByRole("group", { name: "Search in" });
    await expect(scope.getByRole("button", { name: "This collection" })).toBeVisible();
    await expect(page.locator("main [data-doc-row]")).toHaveCount(0);
    await scope.getByRole("button", { name: "Everywhere" }).click();
    await expect(row(page, "Notes on the vector index")).toBeVisible();
    await expect(row(page, "Notes on the vector index").getByRole("link", { name: "Namespace bench" })).toBeVisible();
    expect(await violations(page)).toEqual([]);
  });
});

test.describe("375 px with touch", () => {
  test.use({ viewport: { width: 375, height: 812 }, hasTouch: true, isMobile: true });

  test("the place picker offers All documents, and the list fits the screen", async ({ page }) => {
    await open(page, "/library");
    await page.getByRole("button", { name: "Show the documents of" }).tap();
    await page.locator("[cui-select-item]").filter({ hasText: "All documents" }).tap();
    await expect(page).toHaveURL(/place=all/);
    await expect(page.locator("main [data-doc-row]")).toHaveCount(5);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(375);
    const link = row(page, "Storage tiering RFC").getByRole("link", { name: "Engineering" });
    const box = await link.boundingBox();
    expect(box && box.x + box.width).toBeLessThanOrEqual(375);
    expect(await violations(page)).toEqual([]);
  });
});
