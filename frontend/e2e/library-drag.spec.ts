import { expect, test, type Locator, type Page, type Route } from "@playwright/test";
import { installApi } from "./fixtures";

// Dragging in the Library's tree at 1440 px: documents from the list onto a place that takes
// them, and a collection into another; each drop opens the Move dialog set to that place.

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
  entry("col-ops", "", "admin", "Operations"),
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

/** The documents the server holds, which a move changes; the calls it was sent. */
async function serve(page: Page) {
  const docs = [
    doc("col-eng", "rfcs/storage-tiering.md", "Storage tiering RFC", 2),
    doc("col-eng", "rfcs/compaction.md", "Compaction RFC", 5),
    doc("col-eng", "notes/oncall.md", "On-call notes", 26),
  ];
  const moves: unknown[] = [];
  const nests: unknown[] = [];
  await installApi(page, { knowledgeRows: 1, collections: COLLECTIONS });
  await page.route("**/v1/knowledge.list", async (route: Route) => {
    const body = route.request().postDataJSON() as { namespace: string; namespaces?: string[] };
    const places = [body.namespace, ...(body.namespaces ?? [])];
    await route.fulfill({ json: { rows: docs.filter((d) => places.includes(d.namespace)), next_cursor: null } });
  });
  await page.route("**/v1/knowledge.move", async (route: Route) => {
    const body = route.request().postDataJSON() as { to_namespace: string; source_uris: string[] };
    moves.push(body);
    for (const d of docs) if (body.source_uris.includes(d.source_uri)) d.namespace = body.to_namespace;
    await route.fulfill({
      json: { found: true, results: body.source_uris.map((source_uri) => ({ source_uri, moved: true, reason: "" })) },
    });
  });
  await page.route("**/v1/collection.move", async (route: Route) => {
    const body = route.request().postDataJSON() as { namespace: string; parent: string };
    nests.push(body);
    await route.fulfill({ json: { namespace: body.namespace, parent: body.parent, moved: true } });
  });
  return { moves, nests };
}

const tree = (page: Page) => page.getByRole("navigation", { name: "Library places" });
const place = (page: Page, name: string) =>
  tree(page)
    .locator(".library-tree-row")
    .filter({ has: page.getByRole("link", { name, exact: true }) });
const row = (page: Page, title: string) => page.locator("main [data-doc-row]").filter({ hasText: title });

/** Press on `from`, start the drag, and stop over `to` with the button still down. */
async function holdOver(page: Page, from: Locator, to: Locator) {
  const a = await from.boundingBox();
  if (!a) throw new Error("no source box");
  await page.mouse.move(a.x + 40, a.y + a.height / 2);
  await page.mouse.down();
  await page.mouse.move(a.x + 60, a.y + a.height / 2 + 8, { steps: 4 });
  await expect(to).toHaveAttribute("data-drop", "ok");
  const b = await to.boundingBox();
  if (!b) throw new Error("no target box");
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: 8 });
  // Chrome sends dragover at most every 50 ms: a last small move lands one on the target.
  await expect(async () => {
    await page.mouse.move(b.x + b.width / 2 + 1, b.y + b.height / 2);
    await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
    await expect(to).toHaveAttribute("data-drop", "over", { timeout: 200 });
  }).toPass();
}

test.describe("1440 px", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("a document dragged onto a collection moves there through Move to…, and leaves the list", async ({ page }) => {
    const { moves } = await serve(page);
    await page.goto("/library?collection=col-eng");
    await expect(row(page, "Storage tiering RFC")).toBeVisible();
    await holdOver(page, row(page, "Storage tiering RFC"), place(page, "Operations"));
    // Only the places Move to… lists take it: not its own, not one the person only reads.
    await expect(place(page, "My files")).toHaveAttribute("data-drop", "ok");
    await expect(place(page, "Engineering")).not.toHaveAttribute("data-drop");
    await expect(place(page, "Legal")).not.toHaveAttribute("data-drop");
    await expect(place(page, "All documents")).not.toHaveAttribute("data-drop");
    await page.mouse.up();
    const dialog = page.getByRole("dialog", { name: "Move 1 document" });
    await expect(dialog.getByText("From Engineering")).toBeVisible();
    await expect(dialog.getByText("They take Operations's sharing: whoever reads it reads them.")).toBeVisible();
    await dialog.getByRole("button", { name: "Move", exact: true }).click();
    await expect(page.getByText("Moved 1 document to Operations")).toBeVisible();
    await expect(row(page, "Storage tiering RFC")).toHaveCount(0);
    await expect(row(page, "Compaction RFC")).toBeVisible();
    expect(moves).toEqual([
      expect.objectContaining({ to_namespace: "col-ops", source_uris: ["rfcs/storage-tiering.md"] }),
    ]);
  });

  test("a selection dragged by one of its rows moves together", async ({ page }) => {
    const { moves } = await serve(page);
    await page.goto("/library?collection=col-eng");
    await row(page, "Storage tiering RFC").getByRole("checkbox").click();
    await row(page, "Compaction RFC").getByRole("checkbox").click();
    await holdOver(page, row(page, "Compaction RFC"), place(page, "My files"));
    await page.mouse.up();
    const dialog = page.getByRole("dialog", { name: "Move 2 documents" });
    await dialog.getByRole("button", { name: "Move", exact: true }).click();
    await expect(page.getByText("Moved 2 documents to My files")).toBeVisible();
    await expect(page.locator("main [data-doc-row]")).toHaveCount(1);
    expect(moves).toHaveLength(1);
  });

  test("a collection dragged onto another nests inside it, never inside itself", async ({ page }) => {
    const { nests } = await serve(page);
    await page.goto("/library?collection=col-runbooks");
    await expect(place(page, "Runbooks")).toBeVisible();
    await holdOver(page, place(page, "Runbooks"), place(page, "Operations"));
    await expect(place(page, "Engineering")).not.toHaveAttribute("data-drop");
    await expect(place(page, "Runbooks")).not.toHaveAttribute("data-drop");
    await expect(tree(page).getByText("Drop here for the top level")).toHaveAttribute("data-drop", "ok");
    await page.mouse.up();
    const dialog = page.getByRole("dialog", { name: "Move Runbooks" });
    await expect(dialog.getByRole("button", { name: /Inside/ })).toContainText("Operations");
    await dialog.getByRole("button", { name: "Move", exact: true }).click();
    await expect(page.getByText("Moved Runbooks into Operations")).toBeVisible();
    expect(nests).toEqual([expect.objectContaining({ namespace: "col-runbooks", parent: "col-ops" })]);
  });
});
