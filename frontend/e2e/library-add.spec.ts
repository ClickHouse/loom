import { expect, test, type Page } from "@playwright/test";
import { installApi } from "./fixtures";

const COLLECTIONS = [
  { namespace: "col-me-e2e", parent: "", personal: true, role: "admin", title: "", description: "" },
  { namespace: "col-runbooks", parent: "", personal: false, role: "writer", title: "Runbooks", description: "" },
  { namespace: "policies", parent: "", personal: false, role: "writer", title: "Reference", description: "" },
  { namespace: "col-legal", parent: "", personal: false, role: "viewer", title: "Legal", description: "" },
];
const doc = (namespace: string, source_uri: string, title: string) => ({
  namespace,
  source_uri,
  title,
  description: "",
  tags: [],
  ingested_at: "2026-10-12T12:00:00Z",
  chunk_count: 3,
  superseded: false,
  has_blob: false,
});

async function open(page: Page) {
  await installApi(page, { collections: COLLECTIONS });
  const docs = [
    doc("col-runbooks", "runbooks/drain.md", "Drain a lane"),
    doc("col-runbooks", "runbooks/fill.md", "Fill a lane"),
    doc("bench", "notes/team.md", "Team plan"),
    doc("col-legal", "legal/msa.md", "Services agreement"),
  ];
  const moves: Record<string, unknown>[] = [];
  await page.route("**/v1/knowledge.list", async (route) => {
    const body = route.request().postDataJSON();
    const places = [body.namespace, ...(body.namespaces ?? [])];
    await route.fulfill({ json: { rows: docs.filter((r) => places.includes(r.namespace)), next_cursor: null } });
  });
  await page.route("**/v1/knowledge.move", async (route) => {
    const body = route.request().postDataJSON();
    moves.push(body);
    const results = body.source_uris.map((source_uri: string) => {
      const source = docs.find((d) => d.source_uri === source_uri && d.namespace === body.namespace);
      if (source) source.namespace = body.to_namespace;
      return { source_uri, moved: !!source, reason: source ? "" : "not_found" };
    });
    await route.fulfill({ json: { found: true, results } });
  });
  await page.goto("/library?place=all");
  await expect(page.getByText("Services agreement", { exact: true })).toBeVisible();
  return moves;
}

const row = (page: Page, title: string) => page.locator("main [data-doc-row]").filter({ hasText: title });

for (const width of [1440, 375]) {
  test.describe(`${width} px`, () => {
    test.use({ viewport: { width, height: 900 } });

    test("a row adds to a writable collection after explaining relocation and sharing", async ({ page }, info) => {
      const moves = await open(page);
      await row(page, "Drain a lane").getByRole("button", { name: "Actions for Drain a lane" }).click();
      await page.screenshot({ path: info.outputPath("row-menu.png") });
      await page.getByRole("menuitem", { name: "Add to collection…" }).click();
      const dialog = page.getByRole("dialog", { name: "Add 1 document to a collection" });
      await expect(
        dialog.getByText("Adding moves 1 document out of Runbooks. Each document lives in one place."),
      ).toBeVisible();
      await dialog.getByRole("button", { name: /To/ }).click();
      const list = page.getByRole("dialog").last();
      await expect(list.locator(".collection-depth")).toHaveText(["My files", "Reference"]);
      await list.getByText("Reference", { exact: true }).click();
      await expect(dialog.getByText("They take Reference's sharing: whoever reads it reads them.")).toBeVisible();
      await page.screenshot({ path: info.outputPath("row-confirmation.png") });
      await dialog.getByRole("button", { name: "Cancel" }).click();
      expect(moves).toEqual([]);
      await expect(row(page, "Drain a lane").getByRole("link", { name: "Runbooks" })).toBeVisible();
      await row(page, "Drain a lane").getByRole("button", { name: "Actions for Drain a lane" }).click();
      await page.getByRole("menuitem", { name: "Add to collection…" }).click();
      await dialog.getByRole("button", { name: /To/ }).click();
      await page.getByRole("dialog").last().getByText("Reference", { exact: true }).click();
      await dialog.getByRole("button", { name: "Move", exact: true }).click();
      await expect(dialog).toHaveCount(0);
      expect(moves).toEqual([
        expect.objectContaining({
          namespace: "col-runbooks",
          to_namespace: "policies",
          source_uris: ["runbooks/drain.md"],
        }),
      ]);
      await expect(row(page, "Drain a lane").getByRole("link", { name: "Reference" })).toBeVisible();
      await page.screenshot({ path: info.outputPath("row-moved.png") });
    });

    test("bulk Add to collection requires one source place and moves every selected document", async ({
      page,
    }, info) => {
      const moves = await open(page);
      await row(page, "Services agreement").getByRole("button", { name: "Actions for Services agreement" }).click();
      await expect(page.getByRole("menuitem", { name: "Add to collection…" })).toHaveAttribute("aria-disabled", "true");
      await page.keyboard.press("Escape");
      await page.getByRole("checkbox", { name: "Select Drain a lane", exact: true }).click();
      await page.getByRole("checkbox", { name: "Select Team plan", exact: true }).click();
      const bar = page.getByLabel("Search and filters");
      const action = () =>
        width < 640
          ? page.getByRole("menuitem", { name: "Add to collection…" })
          : bar.getByRole("button", { name: "Add to collection…" });
      if (width < 640) await bar.getByRole("button", { name: "Actions", exact: true }).click();
      await expect(action()).toHaveAttribute("aria-disabled", "true");
      await expect(action()).toHaveAccessibleDescription(
        "Add to collection… takes documents from one place at a time.",
      );
      if (width < 640) await page.keyboard.press("Escape");
      await page.getByRole("checkbox", { name: "Select Team plan", exact: true }).click();
      await page.getByRole("checkbox", { name: "Select Fill a lane", exact: true }).click();
      if (width < 640) await bar.getByRole("button", { name: "Actions", exact: true }).click();
      await action().click();
      const dialog = page.getByRole("dialog", { name: "Add 2 documents to a collection" });
      await expect(
        dialog.getByText("Adding moves 2 documents out of Runbooks. Each document lives in one place."),
      ).toBeVisible();
      await dialog.getByRole("button", { name: /To/ }).click();
      await page.getByRole("dialog").last().getByText("Reference", { exact: true }).click();
      await page.screenshot({ path: info.outputPath("bulk-confirmation.png") });
      await dialog.getByRole("button", { name: "Move", exact: true }).click();
      await expect(dialog).toHaveCount(0);
      expect(moves).toEqual([
        expect.objectContaining({
          namespace: "col-runbooks",
          to_namespace: "policies",
          source_uris: ["runbooks/drain.md", "runbooks/fill.md"],
        }),
      ]);
      await expect(row(page, "Drain a lane").getByRole("link", { name: "Reference" })).toBeVisible();
      await expect(row(page, "Fill a lane").getByRole("link", { name: "Reference" })).toBeVisible();
    });
  });
}

test("bulk actions fit the toolbar beside the sidebar and Ask AI", async ({ page }, info) => {
  for (const width of [768, 800, 900, 1280, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await open(page);
    await page.getByRole("checkbox", { name: "Select Drain a lane", exact: true }).click();
    await page.getByRole("checkbox", { name: "Select Fill a lane", exact: true }).click();
    const bar = page.getByLabel("Search and filters");
    const checkFit = async () => {
      await expect.poll(() => bar.evaluate((el) => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(1);
      const bounds = await bar.boundingBox();
      expect(bounds?.height).toBe(48);
      const inline = bar.getByRole("button", { name: "Add to collection…" });
      if (bounds!.width < 640) {
        await expect(inline).toBeHidden();
        await bar.getByRole("button", { name: "Actions", exact: true }).click();
        await expect(page.getByRole("menuitem", { name: "Add to collection…" })).toBeVisible();
        await page.keyboard.press("Escape");
      } else {
        await expect(inline).toBeVisible();
        await expect(bar.getByRole("button", { name: "Actions", exact: true })).toBeHidden();
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    };
    await checkFit();
    if (width === 800) await page.screenshot({ path: info.outputPath("sidebar-bulk-menu.png") });
    if (width >= 1280) {
      await page.getByRole("button", { name: "Ask AI", exact: true }).click();
      await expect(page.getByRole("textbox", { name: "Question", exact: true })).toBeVisible();
      if (width === 1280) await expect.poll(async () => (await bar.boundingBox())!.width).toBeLessThan(640);
      await checkFit();
      await page.screenshot({ path: info.outputPath(`ask-ai-${width}-bulk.png`) });
      await page.getByRole("button", { name: "Ask AI", exact: true }).click();
    }
  }
});
