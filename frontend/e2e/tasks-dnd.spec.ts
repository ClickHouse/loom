import { expect, test } from "@playwright/test";
import { installApi } from "./fixtures";

// The Tasks board and list in a real browser: a card dragged from To do onto In progress
// sends task.claim for that task (jsdom has no drag, so this is the only check of the
// native drag itself — the drag must survive the list revealing its empty groups).

test("dragging a to-do card onto In progress claims it, on the board and in the list", async ({ page }) => {
  await installApi(page);
  const claims: string[] = [];
  await page.route("**/v1/task.claim", async (route) => {
    const body = route.request().postDataJSON() as { task_id: string };
    claims.push(body.task_id);
    await route.fulfill({ json: { task: { task_id: body.task_id, status: "in_progress" }, seq: 2 } });
  });
  await page.goto("/tasks");
  const title = "Task 2: a regression the public bot found";

  await page.getByRole("button", { name: "Board", exact: true }).click();
  const card = page.getByRole("region", { name: /^To do/ }).getByRole("button", { name: new RegExp(title) });
  await card.dragTo(page.getByRole("region", { name: /^In progress/ }));
  await expect.poll(() => claims).toEqual(["00000000-0000-4000-8000-000000000002"]);

  await page.getByRole("button", { name: "List", exact: true }).click();
  const row = page.getByRole("row", { name: title });
  // In progress is empty, so the list shows it only while a row is held.
  await row.hover();
  await page.mouse.down();
  await page.mouse.move(10, 10, { steps: 4 });
  const target = page.getByRole("rowgroup", { name: /^In progress/ });
  await expect(target).toHaveAttribute("data-drop", "ok");
  await target.hover();
  await page.mouse.up();
  await expect.poll(() => claims.length).toBe(2);
});
