import { expect, test, type Page } from "@playwright/test";
import { violations } from "./axe";
import { codeReposFixture, installApi } from "./fixtures";

// The pages whose rows are columns of comparable fields are Click UI Tables (Runs, Users,
// Agents, Activity, Usage, the review queue, Health's workers); the long ones page with
// Click UI's Pagination bar or load more at the bottom. At desktop and phone width each
// has rows, stays clean under axe, and never scrolls the page sideways (under 769 px Click
// UI lays a table out as a list of cards).

const now = Date.now();
const ago = (min: number) => new Date(now - min * 60_000).toISOString();

const USERS = Array.from({ length: 60 }, (_, i) => ({
  subject: `user-${String(i + 1).padStart(2, "0")}@example.com`,
  contact_email: "",
  grants: [{ namespace: "team-alerts", role: i % 4 ? "viewer" : "writer", is_owner: i === 0, granted_at: null }],
  is_platform_admin: i === 0,
  last_active: ago(i * 90),
  has_password_auth: i % 2 === 0,
}));

const AUDIT = Array.from({ length: 30 }, (_, i) => ({
  ts: ago(i * 7),
  event_type: i % 3 ? "memory.delete" : "namespace.join",
  actor: i % 2 ? "alice@example.com" : "oncall-agent",
  subject: JSON.stringify({ memory_key: `alerts/rule-${i}`, namespace: "team-alerts", role: "viewer" }),
  request_id: `req-${i}`,
}));

const conflict = (i: number) => ({
  memory_id: `mem-${i}`,
  memory_key: `alerts/threshold/lane-${i}`,
  memory_type: "semantic",
  value: `page when lane ${i} p95 latency is over ${300 + i} ms`,
  version: 100 - i,
  pinned: i === 2,
  created_at: ago(i * 30),
  conflict_with: [`peer-${i}-000000000000`],
  content_sha256: "",
});

const WORKERS = {
  workers: {
    embedding_worker: {
      effective: true,
      interval_seconds: 30,
      last_run_at: ago(0.2),
      last_duration_ms: 420,
      consecutive_failures: 0,
      last_pass_status: "ok",
      total_runs: 1200,
      rows_written: 44,
      reporting_from: "deploy:workers:green",
    },
    dedup_worker: {
      effective: true,
      interval_seconds: 300,
      last_run_at: ago(3),
      last_duration_ms: 12_300,
      consecutive_failures: 3,
      last_error: "ConnectionError: refused",
    },
    conflicts_worker: { effective: false, disabled_reason: "disabled_by_setting" },
  },
  store: { table: "loom.worker_status", rows: 3, reporters: ["deploy:workers:green"] },
};

async function withRows(page: Page) {
  await installApi(page, { runRows: 200, memoryRows: 4 });
  // Registered after installApi, so these answer first.
  await page.route("**/v1/admin.users", (r) =>
    r.fulfill({
      json: {
        users: USERS,
        total: USERS.length,
        total_pending_elevations: 0,
        total_active_last_7d: 12,
        platform_admins_configured: ["user-01@example.com"],
      },
    }),
  );
  await page.route("**/v1/audit.list", (r) => r.fulfill({ json: { rows: AUDIT, next_cursor: null } }));
  await page.route("**/v1/conflicts.list", async (r) => {
    const body = r.request().postDataJSON() as { count_only?: boolean; keys_only?: boolean; before_version?: number };
    if (body.count_only) return r.fulfill({ json: { rows: [], total: 4 } });
    if (body.keys_only) return r.fulfill({ json: { rows: [], keys: [] } });
    if (body.before_version) return r.fulfill({ json: { rows: [conflict(3)], next_before_version: null } });
    return r.fulfill({ json: { rows: [0, 1, 2].map(conflict), next_before_version: 97 } });
  });
  await page.route("**/v1/conflicts.allowlist_list", (r) =>
    r.fulfill({
      json: {
        rows: [
          {
            memory_key_a: "alerts/threshold/lane-1",
            memory_key_b: "alerts/threshold/lane-1-eu",
            allowed_at: ago(60),
            allowed_by: "op@example.com",
            reason: "regional override",
          },
        ],
      },
    }),
  );
  await page.route("**/v1/admin/workers.status", (r) => r.fulfill({ json: WORKERS }));
}

async function open(page: Page, path: string) {
  await page.goto(path);
  await expect(page.locator("main h1").first()).toBeVisible();
  await page.waitForLoadState("networkidle");
  // Settled: no table or list still marks itself loading.
  await expect(page.locator('main [aria-busy="true"]')).toHaveCount(0);
}

/** Neither the document nor the scrolling main is wider than the window. */
async function sideways(page: Page): Promise<number> {
  return page.evaluate(() => {
    const main = document.getElementById("main");
    const doc = document.documentElement.scrollWidth - window.innerWidth;
    return Math.max(doc, main ? main.scrollWidth - main.clientWidth : 0);
  });
}

const TABLES: [string, string][] = [
  ["/runs", "Runs"],
  ["/admin/users", "Users"],
  ["/admin/activity", "Audit events"],
  ["/admin/review_queue", "Conflicts"],
  ["/admin/health", "Workers"],
];

for (const width of [1400, 375]) {
  test.describe(`${width} px`, () => {
    test.use({ viewport: { width, height: 900 } });
    for (const [path, name] of TABLES) {
      test(`${path} is a table with rows, axe clean and no sideways scroll`, async ({ page }) => {
        await withRows(page);
        await open(page, path);
        const table = page.getByRole("table", { name, exact: true });
        await expect(table.locator("tbody tr").first()).toBeVisible();
        expect(await table.locator("tbody tr").count()).toBeGreaterThan(1);
        expect(await sideways(page)).toBeLessThanOrEqual(0);
        expect(await violations(page)).toEqual([]);
      });
    }
  });
}

test("runs page 50 at a time over the newest 200, and say so", async ({ page }) => {
  await withRows(page);
  await open(page, "/runs");
  const table = page.getByRole("table", { name: "Runs", exact: true });
  await expect(table.locator("tbody tr")).toHaveCount(50);
  await expect(page.getByText(/Newest 200 runs/)).toBeVisible();
  const pager = page.getByRole("navigation", { name: "Runs pages" });
  await expect(pager).toContainText("1–50 of 200");
  await pager.getByRole("button", { name: "Next page" }).click();
  await expect(pager).toContainText("51–100 of 200");
  await expect(page.locator("#runs-list")).toBeFocused();
  await expect(table.locator("tbody tr")).toHaveCount(50);
});

test("users page 50 at a time", async ({ page }) => {
  await withRows(page);
  await open(page, "/admin/users");
  const table = page.getByRole("table", { name: "Users", exact: true });
  await expect(table.locator("tbody tr")).toHaveCount(50);
  const pager = page.getByRole("navigation", { name: "Users pages" });
  await pager.getByRole("button", { name: "Next page" }).click();
  await expect(table.locator("tbody tr")).toHaveCount(10);
  await expect(pager).toContainText("51–60 of 60");
});

test("the review queue loads more and counts what it holds of the total", async ({ page }) => {
  await withRows(page);
  await open(page, "/admin/review_queue");
  const table = page.getByRole("table", { name: "Conflicts", exact: true });
  await expect(table.locator("tbody tr")).toHaveCount(3);
  await expect(page.getByText("3 of 4")).toBeVisible();
  await page.getByRole("button", { name: "Load more" }).click();
  await expect(table.locator("tbody tr")).toHaveCount(4);
  await expect(page.getByText("4 of 4")).toBeVisible();
  await expect(page.getByRole("button", { name: "Load more" })).toHaveCount(0);
});

test("repositories page above 50 and not below", async ({ page }) => {
  const one = codeReposFixture(now)[0];
  const many = Array.from({ length: 60 }, (_, i) => ({
    ...one,
    namespace: `code-repo-${i}`,
    repo: `acme/repo-${String(i).padStart(2, "0")}`,
  }));
  await installApi(page, { codeRepos: many });
  await open(page, "/repositories");
  const pager = page.getByRole("navigation", { name: "Repository pages" });
  await expect(pager).toContainText("1–50 of 60");
  await pager.getByRole("button", { name: "Next page" }).click();
  await expect(pager).toContainText("51–60 of 60");
  await expect(page.locator("#repositories-list")).toBeFocused();

  await installApi(page, { codeRepos: codeReposFixture(now) });
  await open(page, "/repositories");
  await expect(page.locator("#repositories-list")).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Repository pages" })).toHaveCount(0);
});
