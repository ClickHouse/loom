import { expect, test } from "@playwright/test";
import { violations } from "./axe";
import { installApi, NAMESPACE } from "./fixtures";

for (const status of ["failed", "completed"] as const) {
  test(`a ${status} erasure with a withheld receipt never claims nothing remains`, async ({ page }, testInfo) => {
    await installApi(page, { viewer: true });
    const now = new Date().toISOString();
    const row = {
      erasure_id: "e1",
      scope_type: "namespace",
      scope_value: NAMESPACE,
      requester: "ops",
      status,
      requested_at: now,
      completed_at: now,
      failure_reason: status === "failed" ? "ERASURE_FAILED" : "",
      steps_completed_count: 3,
      reversals_count: 0,
      residue_count: 0,
      verified_at: null,
    };
    await page.route("**/v1/erasure.list", (r) => r.fulfill({ json: { rows: [row] } }));
    await page.route("**/v1/erasure.status", (r) =>
      r.fulfill({ json: { ...row, found: true, current_step: 3, residue: {}, census: null } }),
    );
    await page.goto("/admin/activity");
    await page.getByRole("tab", { name: "Erasures", exact: true }).click();
    await page.getByRole("button", { name: /Details of the erasure of/ }).click();
    const dialog = page.getByRole("dialog", { name: "Erasure details" });
    await expect(dialog.getByText("Counts unavailable", { exact: true })).toBeVisible();
    await expect(dialog.getByText("Nothing", { exact: true })).toHaveCount(0);
    await expect(dialog.getByText("Verified", { exact: true })).toHaveCount(0);
    expect(await violations(page)).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath("receipt.png") });
    await dialog.getByRole("button", { name: "Close", exact: true }).filter({ hasText: "Close" }).click();
    await expect(dialog).toHaveCount(0);
  });
}
