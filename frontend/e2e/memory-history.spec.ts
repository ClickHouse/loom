import { expect, test } from "@playwright/test";
import { installApi } from "./fixtures";

// A history version's change summary is read in full: a long tag beside other changes
// wraps instead of being cut, on the current version and on an older one.

const LONG_TAG = `owner:${"platform-reliability-ingest-lane-".repeat(5)}x`;

test("a history change summary wraps to show every change", async ({ page }) => {
  await installApi(page, { memoryRows: 1 });
  const base = {
    memory_id: "00000000-0000-0000-0000-000000000000",
    value: "Alert fired on the ingest lane",
    content_sha256: "s1",
    created_at: "2026-09-19T00:00:00Z",
    tombstoned_at: null,
    redacted_at: null,
    source_trace: "",
    status: "active",
    conflict_with: [],
  };
  const rows = [
    { ...base, version: 3, tags: ["alerts", LONG_TAG], pinned: true, confidence: 0.9 },
    { ...base, version: 2, tags: ["alerts"], pinned: false, confidence: 0.5, verified_at: "2026-09-19T00:00:00Z" },
    { ...base, version: 1, tags: [], pinned: false, confidence: 0.5 },
  ];
  await page.route("**/v1/memory.history", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ rows }) }),
  );

  await page.goto("/me/memories", { waitUntil: "domcontentloaded" });
  await page.locator("[data-memory-key]").first().click();
  const drawer = page.getByRole("dialog");
  await drawer.getByRole("button", { name: /History/ }).click();

  const summaries = drawer.getByTestId("history-change");
  await expect(summaries).toHaveCount(2);
  await expect(summaries.nth(0)).toHaveText(`tags +${LONG_TAG} · verification revoked · pinned · confidence 0.5 → 0.9`);
  await expect(summaries.nth(1)).toHaveText("tags +alerts · verified");
  for (const i of [0, 1]) {
    const fits = await summaries.nth(i).evaluate((el) => ({
      h: el.scrollHeight <= el.clientHeight + 1,
      w: el.scrollWidth <= el.clientWidth + 1,
    }));
    expect(fits).toEqual({ h: true, w: true });
  }
  // Every change is on screen, not cut: the summary spans more than one line.
  const box = await summaries.nth(0).boundingBox();
  const line = await summaries.nth(1).boundingBox();
  expect(box && line && box.height > line.height * 1.5).toBe(true);
});
