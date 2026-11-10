import { expect, test, type Page } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { installApi, type CodeRepoFixture } from "./fixtures";

const SHOTS = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "../e2e-screenshots");
async function shot(page: Page, name: string): Promise<void> {
  fs.mkdirSync(SHOTS, { recursive: true });
  const file = path.join(SHOTS, `${name}.png`);
  await page.screenshot({ path: file, fullPage: true });
  await test.info().attach(name, { path: file, contentType: "image/png" });
}

const TOKENS = [
  {
    name: "laptop",
    sa_subject: "sa:laptop-1",
    namespaces: ["personal-e2e", "*"],
    roles: ["writer"],
    ttl_days: 90,
    created_by: "e2e",
    created_at: new Date(Date.now() - 6 * 86400e3).toISOString(),
    expires_at: new Date(Date.now() + 84 * 86400e3).toISOString(),
    revoked_at: null,
    revoked_by: "",
    last_used_at: new Date(Date.now() - 3600e3).toISOString(),
  },
  {
    name: "cron-cleanup",
    sa_subject: "sa:cron",
    namespaces: ["personal-e2e"],
    roles: ["viewer"],
    ttl_days: 30,
    created_by: "e2e",
    created_at: new Date(Date.now() - 20 * 86400e3).toISOString(),
    expires_at: new Date(Date.now() + 10 * 86400e3).toISOString(),
    revoked_at: null,
    revoked_by: "",
    last_used_at: null,
  },
];

// An account whose list_mine holds only its auto-claimed personal namespace, with an
// empty memory list, lands on Setup with the four-step checklist above the token form.
// The API is answered locally (fixtures.ts), so the empty state is seeded by routing, not
// by a server.

const PERSONAL = "personal-e2e";

test("an empty personal namespace lands on the getting-started checklist", async ({ page }) => {
  await installApi(page);
  // Later routes win: the personal-only list_mine and the empty memory.list
  // describe a brand-new account. `total: 0` is the fixtures' default for
  // every unlisted op, memory.list included; it is made explicit here.
  await page.route("**/v1/namespace.list_mine", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        rows: [
          {
            namespace: PERSONAL,
            role: "admin",
            status: "active",
            visibility: "private",
            kind: "memory",
            description: "",
            joined_at: "2026-09-16T00:00:00Z",
            is_new: true,
          },
        ],
      }),
    }),
  );
  await page.route("**/v1/memory.list", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ entries: [], truncated: false, total: 0, today: 0 }),
    }),
  );
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e)));

  await page.goto("/settings");
  const region = page.getByRole("region", { name: "Getting started" });
  await expect(region).toBeVisible();
  await expect(region.getByRole("listitem")).toHaveCount(4);
  await expect(region.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "0");
  await expect(region.getByRole("button", { name: "Mint a token" })).toBeVisible();
  // The checklist sits above the tabs; Tokens is open with the form (no token yet), Memory is one tab away.
  await expect(page.getByRole("tab", { name: "Tokens" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tab", { name: "Memory" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "System" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /Create token/ })).toBeVisible();

  // The search step opens the header palette with its hint typed in.
  await region.getByRole("button", { name: "Search your memories" }).click();
  await expect(page.getByRole("combobox", { name: "Search Loom" }).or(page.getByLabel("Search Loom"))).toHaveValue(
    "deploy window",
  );
  await expect(region.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "1");
  expect(errors).toEqual([]);
});

// Two tabs, Tokens and Memory; the token form folds once a token exists; the Memory tab reads nothing until it opens; an old System link lands on Tokens.
test.describe("settings tabs", () => {
  test("a returning account: Tokens lands on the list with the form folded; Memory is the other tab; the URL carries the tab", async ({
    page,
  }) => {
    await installApi(page);
    await page.route("**/v1/me/service_accounts", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ rows: TOKENS }) }),
    );
    let blacklistReads = 0;
    await page.route("**/v1/blacklist.list", (route) => {
      blacklistReads += 1;
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ rows: [] }) });
    });
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.goto("/settings");
    await expect(page.getByRole("tab", { name: "Tokens" })).toHaveAttribute("aria-selected", "true");
    // The list first; the form folded behind New token.
    await expect(page.getByText("laptop", { exact: true })).toBeVisible();
    await expect(page.getByText("cron-cleanup")).toBeVisible();
    await expect(page.getByRole("button", { name: "New token" })).toBeVisible();
    await expect(page.getByRole("button", { name: /Create token/ })).toHaveCount(0);
    // Nothing of the other tabs was read.
    await page.waitForTimeout(300);
    expect(blacklistReads).toBe(0);
    await shot(page, "19-settings-tokens-folded");
    await page.getByRole("button", { name: "New token" }).click();
    await expect(page.getByRole("button", { name: /Create token/ })).toBeVisible();
    await page.getByRole("radio", { name: "Only these namespaces" }).click();
    await expect(page.getByRole("checkbox", { name: "Read bench" })).toBeVisible();
    await shot(page, "20-settings-tokens-form-open");
    await page.getByRole("button", { name: "Cancel" }).click();
    await expect(page.getByRole("button", { name: /Create token/ })).toHaveCount(0);

    await page.getByRole("tab", { name: "Memory" }).click();
    await expect(page).toHaveURL(/tab=memory/);
    await expect(page.getByRole("heading", { name: "Never save" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Import a document" })).toBeVisible();
    await expect.poll(() => blacklistReads).toBe(1);
    // A suggested pattern fills the form, never the clipboard.
    await page.getByRole("button", { name: "API keys" }).click();
    await expect(page.getByLabel("Substring to block")).toHaveValue("sk-[A-Za-z0-9]{20,}");
    await shot(page, "21-settings-memory");
    await page.getByRole("button", { name: "Import a document" }).click();
    await expect(page.getByRole("dialog", { name: "Import a document" })).toBeVisible();
    await page.keyboard.press("Escape");

    // A deep link opens the tab it names.
    await page.goto("/settings?tab=memory");
    await expect(page.getByRole("tab", { name: "Memory" })).toHaveAttribute("aria-selected", "true");
    expect(errors).toEqual([]);
  });

  test("an old System link opens Tokens", async ({ page }) => {
    await installApi(page);
    await page.goto("/settings");
    await expect(page.getByRole("tab", { name: "Tokens" })).toBeVisible();
    await expect(page.getByRole("tab", { name: "System" })).toHaveCount(0);
    await page.goto("/settings?tab=system");
    await expect(page.getByRole("tab", { name: "Tokens" })).toHaveAttribute("aria-selected", "true");
  });

  test.describe("phone 400, coarse pointer", () => {
    test.use({ viewport: { width: 400, height: 860 }, hasTouch: true, isMobile: true });
    test("the tab strip fits, the form folds, nothing scrolls sideways", async ({ page }) => {
      await installApi(page);
      await page.route("**/v1/me/service_accounts", (route) =>
        route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ rows: TOKENS }) }),
      );
      await page.goto("/settings");
      await expect(page.getByRole("button", { name: "New token" })).toBeVisible();
      const doc = await page.evaluate(() => ({
        sw: document.documentElement.scrollWidth,
        cw: document.documentElement.clientWidth,
      }));
      expect(doc.sw).toBeLessThanOrEqual(doc.cw);
      for (const name of ["Tokens", "Memory"]) {
        const box = await page.getByRole("tab", { name }).boundingBox();
        expect(box, `${name} tab box`).not.toBeNull();
        // Layout lands on sub-pixel values (43.99998 for a 44 px minimum).
        expect(Math.round(box?.height ?? 0), `${name} tab height`).toBeGreaterThanOrEqual(44);
      }
      await shot(page, "23-settings-phone-tokens");
      await page.getByRole("tab", { name: "Memory" }).click();
      await expect(page.getByRole("heading", { name: "Never save" })).toBeVisible();
      await expect(page.getByRole("button", { name: "Add", exact: true })).toBeVisible();
      const doc2 = await page.evaluate(() => ({
        sw: document.documentElement.scrollWidth,
        cw: document.documentElement.clientWidth,
      }));
      expect(doc2.sw).toBeLessThanOrEqual(doc2.cw);
      await shot(page, "24-settings-phone-memory");
    });
  });
});

// One mint for a CI token over two team namespaces and a restricted repository: the
// repository is picked by its name and branch, beside the ticked namespaces, never typed.
test("a restricted repository is picked by repository and branch in the token form", async ({ page }) => {
  const member = (namespace: string, role: string, kind: string) => ({
    namespace,
    role,
    status: "active",
    visibility: "private",
    kind,
    description: "",
    joined_at: "2026-09-16T00:00:00Z",
    is_new: false,
  });
  const branch = (namespace: string, name: string) =>
    ({
      org: "dev",
      namespace,
      repo: "acme/private",
      branch: name,
      git_head: "a1b2c3d",
      clone_head: "a1b2c3d",
      fast_tier_lag: 0,
      scip_lag: 0,
      scip_py_lag: 0,
      indexed_at: "2026-09-16T00:00:00Z",
      last_refresh_at: "2026-09-16T00:00:00Z",
      counts: {},
      join_policy: "approval",
      my_status: "member",
      my_role: "viewer",
      source: "config",
      searchable: true,
      classification: "restricted",
    }) as CodeRepoFixture;
  await installApi(page, {
    mine: [
      member("team-private", "admin", "memory"),
      member("team-public", "viewer", "memory"),
      member("code-acme-private", "viewer", "code"),
      member("code-acme-private-26-8", "viewer", "code"),
    ],
    codeRepos: [branch("code-acme-private", "master"), branch("code-acme-private-26-8", "26.8")],
    savedNamespace: "team-private",
  });
  const minted: Record<string, unknown>[] = [];
  await page.route("**/v1/me/service_accounts.create", async (route) => {
    minted.push(route.request().postDataJSON());
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        token: "eyJhbGciOiJFUzI1NiJ9.e30.sig",
        name: "ci",
        sa_subject: "sa:ci-1",
        org: "dev",
        namespaces: ["team-private", "team-public", "code-acme-private"],
        ns_roles: { "team-private": "admin", "team-public": "viewer", "code-acme-private": "viewer" },
        reads_everything: false,
        saves_to: "team-private",
        created_at: "2026-09-16T00:00:00",
        expires_at: null,
        restricted_namespaces: ["code-acme-private"],
        mcp_url: "http://127.0.0.1/mcp",
      }),
    });
  });
  await page.goto("/settings");
  await page.getByRole("radio", { name: "Only these namespaces" }).click();
  const repo = page.getByRole("checkbox", { name: "Read every branch of acme/private" });
  await expect(repo).toBeVisible();
  await expect(page.getByRole("checkbox", { name: "Read acme/private branch 26.8" })).toBeVisible();
  await expect(page.getByRole("checkbox", { name: "Read code-acme-private" })).toHaveCount(0);
  // Nothing is ticked at first: the namespace the page is on is picked like any other.
  await expect(page.getByRole("checkbox", { name: "Read team-private" })).not.toBeChecked();
  await page.getByRole("checkbox", { name: "Read team-private" }).click();
  await page.getByRole("checkbox", { name: "Read team-public" }).click();
  await page.getByRole("checkbox", { name: "Read acme/private branch master" }).click();
  await shot(page, "21-settings-token-with-a-restricted-repository");
  await page.getByRole("button", { name: /Create token/ }).click();
  await expect(page.getByRole("dialog", { name: "Token created" })).toBeVisible();
  expect(minted).toHaveLength(1);
  expect(minted[0].namespaces).toEqual([
    { namespace: "team-private", role: "admin" },
    { namespace: "team-public", role: "viewer" },
    { namespace: "code-acme-private", role: "viewer" },
  ]);
});
