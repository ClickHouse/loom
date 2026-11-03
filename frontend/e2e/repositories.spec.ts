import { expect, test, type Locator, type Page } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import {
  boxOf,
  CODE_REPO,
  codeReposFixture,
  installApi,
  NAMESPACE,
  trackerSourcesFixture,
  TRACKER_PRIVATE_NS,
  TRACKER_PUBLIC_NS,
  MIN_TARGET_PX,
} from "./fixtures";

// A repository is one list row with two facets: `code.repos` and `tracker.source.list`
// join by repository into one row; line 1 is the name and the worst branch's health,
// line 2 a muted summary, and the one inline action a split button (Open / Join /
// Request access, its menu naming the branches). Opened, the row shows a table of its
// branches (a menu per branch), the tracker line with the scope chips, and a folded
// Settings for maintainers. Five verbs on both facets; the page lives at /repositories;
// at 400 px nothing truncates and every control is 44 px under a coarse pointer.

const SHOTS = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "../e2e-screenshots");

async function shot(page: Page, name: string): Promise<void> {
  fs.mkdirSync(SHOTS, { recursive: true });
  const file = path.join(SHOTS, `${name}.png`);
  await page.screenshot({ path: file, fullPage: true });
  await test.info().attach(name, { path: file, contentType: "image/png" });
}

function collectErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  return errors;
}

/** The `namespace.join` body the dashboard sends for a viewer's join. */
const joined = (namespace: string) => ({ op: "join", org: "dev", namespace, role: "viewer", consumer: "loom-ui" });

const card = (page: Page) => page.getByRole("region", { name: CODE_REPO, exact: true });

/** Opens a folded card by its heading and waits for the facets. */
async function unfold(region: Locator, repo: string): Promise<void> {
  const toggle = region.getByRole("button", { name: repo, exact: true });
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
}

async function noOverflow(el: Locator): Promise<void> {
  const box = await el.evaluate((n) => ({ sw: n.scrollWidth, cw: n.clientWidth }));
  expect(box.sw, `scrollWidth ${box.sw} > clientWidth ${box.cw}`).toBeLessThanOrEqual(box.cw);
}

test.describe("desktop 1360", () => {
  test.use({ viewport: { width: 1360, height: 900 } });

  test("one row for ClickHouse/ClickHouse with both facets; five verbs; joins call namespace.join once each; diagnostics only for a maintainer", async ({
    page,
  }) => {
    const calls = await installApi(page, { codeRepos: codeReposFixture(), trackerSources: trackerSourcesFixture() });
    const errors = collectErrors(page);
    await page.goto("/repositories");
    await expect(page.getByRole("heading", { name: "Repositories" })).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`/repositories\\?namespace=${NAMESPACE}$`));
    // No "tracker sources" heading; the plane is named by its contents.
    await expect(page.getByRole("heading", { name: "tracker sources" })).toHaveCount(0);
    await expect(page.getByRole("region", { name: /^tracker source/ })).toHaveCount(0);
    // The toolbar: the filter leads, the count at its end.
    const bar = page.getByRole("search", { name: "Search and filters" });
    await expect(bar.getByRole("textbox", { name: "Filter repositories" })).toBeVisible();
    await expect(bar.getByText("3 repositories · 3 branches")).toBeVisible();

    const c = card(page);
    await expect(c).toHaveCount(1);
    await expect(c.getByRole("heading", { level: 2, name: CODE_REPO })).toBeVisible();
    // Folded: health, the summary and the one inline action; no branch table yet.
    await expect(c.getByText("Up to date", { exact: true })).toBeVisible();
    await expect(c.getByText("open", { exact: true })).toHaveCount(0);
    await expect(c.getByText("3 branches · 31,539 files · indexed 2h ago · not joined")).toBeVisible();
    await expect(c.getByRole("button", { name: "Join", exact: true })).toBeVisible();
    await expect(c.getByRole("table")).toHaveCount(0);
    await shot(page, "14-repositories-desktop-folded");
    await unfold(c, CODE_REPO);
    const table = c.getByRole("table", { name: `${CODE_REPO} branches` });
    await expect(table.getByRole("row")).toHaveCount(4);
    await expect(table.getByRole("row", { name: /master/ })).toContainText("31,539");
    await expect(table.getByRole("row", { name: /master/ })).toContainText("412,000");
    const rec = c.getByRole("group", { name: "Tracker" });
    await expect(rec.getByText("22,579 issues · 65,328 PRs")).toBeVisible();
    await expect(rec.getByText(/refreshed/)).toBeVisible();
    await expect(rec.getByRole("button", { name: "Public: not joined, join" })).toBeVisible();
    // The fixture's private namespace declares ClickHouse/example-private only, so its
    // chip sits on that record-only row, not here.
    await expect(rec.getByRole("button", { name: /Private/ })).toHaveCount(0);
    // Diagnostics are not on a reader's row; no slug or branch in any button label.
    await expect(c.getByText("qa.example:9440")).toHaveCount(0);
    await expect(c.getByText(/contract/)).toHaveCount(0);
    await expect(c.getByText("Settings")).toHaveCount(0);
    await expect(c.getByRole("button", { name: /tracker-clickhouse/ })).toHaveCount(0);
    await expect(c.getByRole("button", { name: /· master|· 25\.8/ })).toHaveCount(0);
    // The private namespace declares ClickHouse/example-private, and clickstack names
    // ClickHouse/clickstack: each gets a record-only row headed by that repository.
    const priv = page.getByRole("region", { name: "ClickHouse/example-private", exact: true });
    await expect(priv).toBeVisible();
    await expect(priv.getByText("Restricted", { exact: true })).toBeVisible();
    await expect(priv.getByRole("button", { name: "Request access", exact: true })).toBeVisible();
    await unfold(priv, "ClickHouse/example-private");
    await expect(priv.getByRole("table")).toHaveCount(0);
    const privRec = priv.getByRole("group", { name: "Tracker" });
    await expect(
      privRec.getByRole("button", { name: "Private (restricted): not joined, request access" }),
    ).toBeVisible();
    const cs = page.getByRole("region", { name: "ClickHouse/clickstack", exact: true });
    await expect(cs.getByText("tracker: no credential · not joined")).toBeVisible();
    await unfold(cs, "ClickHouse/clickstack");
    await expect(cs.getByText("No credential", { exact: true })).toBeVisible();
    await expect(cs.getByText("Public · registering…")).toBeVisible();
    await expect(cs.getByRole("button", { name: /^Join/ })).toHaveCount(0);
    expect(calls.trackerSources).toBe(1);
    expect(calls.join).toEqual([]);
    await shot(page, "15-repositories-desktop-before-join");

    // The row's Join joins the open trunk directly — one namespace.join.
    await c.getByRole("button", { name: "Join", exact: true }).click();
    await expect(table.getByRole("row", { name: /master/ })).toContainText("Reading");
    expect(calls.join).toEqual([joined("code-clickhouse")]);
    await expect(c.getByRole("button", { name: "Open", exact: true })).toBeVisible();
    await expect(c.getByText("3 branches · 31,539 files · indexed 2h ago · you read 1 of 3")).toBeVisible();

    // The tracker line: the Public chip confirms, then one namespace.join with the tracker namespace.
    await rec.getByRole("button", { name: "Public: not joined, join" }).click();
    let dialog = page.getByRole("dialog");
    await expect(dialog.getByText("Join the tracker?")).toBeVisible();
    await dialog.getByRole("button", { name: "Join", exact: true }).click();
    await expect(rec.getByRole("button", { name: "Public: reading" })).toBeVisible();
    expect(calls.join[1]).toEqual(joined(TRACKER_PUBLIC_NS));
    await expect(page.locator("[data-toast-viewport]")).toContainText(
      `Joined ${TRACKER_PUBLIC_NS} (${CODE_REPO}) · read-only`,
    );
    await expect(rec.getByRole("button", { name: "Leave · tracker" })).toBeVisible();

    // Request access on the restricted record facet (the record-only row).
    await privRec.getByRole("button", { name: "Private (restricted): not joined, request access" }).click();
    dialog = page.getByRole("dialog");
    await expect(dialog.getByText("Request access to the tracker?")).toBeVisible();
    await expect(dialog.getByText(/Private tracker\./)).toBeVisible();
    await dialog.getByRole("button", { name: "Request access" }).click();
    await expect(privRec.getByRole("button", { name: "Private (restricted): requested" })).toBeVisible();
    await expect(priv.getByText("Requested", { exact: true })).toBeVisible();
    expect(calls.join[2]).toEqual(joined(TRACKER_PRIVATE_NS));
    await expect(page.locator("[data-toast-viewport]")).toContainText("Access requested");

    // Request access on the restricted branch from its row's menu: the request dialog with its role choice.
    await table.getByRole("button", { name: `Actions for ${CODE_REPO} @ 25.7` }).click();
    await page.getByRole("menuitem", { name: "Request access" }).click();
    dialog = page.getByRole("dialog");
    await expect(dialog.getByRole("radio", { name: /Reader/ })).toBeChecked();
    await expect(dialog.getByRole("radio", { name: /Maintainer/ })).toBeVisible();
    await dialog.getByRole("button", { name: "Request access" }).click();
    await expect(table.getByRole("row", { name: /25\.7/ })).toContainText("Requested");
    expect(calls.join[3]).toEqual(joined("code-clickhouse-25-7"));
    // A request waiting on a maintainer is information, not a warning.
    await expect(page.getByText("Waiting for a maintainer.")).toBeVisible();
    // The repository's verbs sit in the split button's menu.
    await c.getByRole("button", { name: `More actions for ${CODE_REPO}` }).click();
    await expect(page.getByRole("menuitem", { name: "Choose branches…" })).toBeVisible();
    await expect(page.getByRole("menuitem", { name: "Open master" })).toBeVisible();
    await expect(page.getByRole("menuitem", { name: "Request maintainer access" })).toHaveCount(0); // a request is pending
    await page.keyboard.press("Escape");
    await shot(page, "16-repositories-desktop-joined-requested");

    // Open the record: the picker on the tracker namespace, the Graph view in Patterns mode.
    await rec.getByRole("button", { name: "Public: reading" }).click();
    await expect(page).toHaveURL(new RegExp(`/graph\\?namespace=${TRACKER_PUBLIC_NS}&mode=patterns$`));
    await expect(page.getByRole("button", { name: `Namespace: ${TRACKER_PUBLIC_NS}` })).toBeVisible();
    await expect(page.getByRole("button", { name: "Patterns" })).toHaveAttribute("aria-pressed", "true");
    expect(errors).toEqual([]);
  });

  test("a failed read says so with Retry, and Retry reads again", async ({ page }) => {
    await installApi(page, { codeRepos: codeReposFixture() });
    let fail = true;
    await page.route("**/v1/code.repos", (r) =>
      fail
        ? r.fulfill({
            status: 503,
            contentType: "application/json",
            body: JSON.stringify({ detail: { code: "INTERNAL", message: "ClickHouse timed out" } }),
          })
        : r.fallback(),
    );
    await page.goto("/repositories");
    await expect(page.getByRole("heading", { name: "Couldn't load repositories" })).toBeVisible();
    await expect(page.getByText("No repositories yet")).toHaveCount(0);
    fail = false;
    await page.getByRole("button", { name: "Retry" }).click();
    await expect(card(page)).toBeVisible();
  });

  test("nothing indexed: the onboarding steps and one Add repository", async ({ page }) => {
    await installApi(page, { codeRepos: [] });
    await page.goto("/repositories");
    await expect(page.getByRole("heading", { name: "Index your first repository" })).toBeVisible();
    await expect(page.getByText("Add a Git remote and a branch")).toBeVisible();
    await expect(page.getByText("Loom indexes it in the background")).toBeVisible();
    await expect(page.getByText("Agents search it with code.search")).toBeVisible();
    await expect(page.getByRole("button", { name: "Add repository" })).toHaveCount(1);
  });

  test("the sidebar link says Repositories and points to /repositories", async ({ page }) => {
    await installApi(page, { codeRepos: codeReposFixture() });
    await page.goto("/repositories?repo=");
    await expect(page.getByRole("heading", { name: "Repositories" })).toBeVisible();
    await expect(page).toHaveURL(/\/repositories/);
    const link = page.getByRole("link", { name: "Repositories" }).first();
    await expect(link).toHaveAttribute("href", "/repositories");
  });

  test("a server without tracker.source.list: the rows render, the tracker line says so, no error banner", async ({
    page,
  }) => {
    const calls = await installApi(page, { codeRepos: codeReposFixture() });
    await page.goto("/repositories");
    await expect(card(page)).toBeVisible();
    await expect(page.getByRole("alert")).toHaveCount(0);
    await unfold(card(page), CODE_REPO);
    await expect(card(page).getByRole("table", { name: `${CODE_REPO} branches` })).toBeVisible();
    await expect(card(page).getByRole("group", { name: "Tracker" })).toContainText(
      "Trackers are not available on this deployment yet.",
    );
    await expect(page.getByText("Trackers are not available on this deployment yet.")).toHaveCount(1);
    await page.waitForTimeout(300);
    expect(calls.trackerSources).toBe(1);
  });

  test("a viewer token issues no request its role cannot make", async ({ page }) => {
    const calls = await installApi(page, {
      codeRepos: codeReposFixture(),
      trackerSources: trackerSourcesFixture(),
      viewer: true,
    });
    const requested: string[] = [];
    page.on("request", (r) => {
      const u = new URL(r.url());
      if (u.pathname.startsWith("/v1/")) requested.push(u.pathname);
    });
    await page.goto("/repositories");
    await expect(card(page)).toBeVisible();
    await page.waitForTimeout(600);
    const unexpected = requested.filter((p) =>
      /^\/v1\/(qa\.source\.(add|check|rotate|remove)|admin\/|namespace\.(join|leave|cancel_request))/.test(p),
    );
    expect(unexpected).toEqual([]);
    expect(calls.join).toEqual([]);
    expect(calls.trackerSources).toBe(1);
    await expect(page.getByRole("button", { name: /rotate|retire|remove|check now/i })).toHaveCount(0);
    await expect(page.locator("#main").getByText("Settings", { exact: true })).toHaveCount(0);
  });
});

test.describe("phone 400, coarse pointer", () => {
  test.use({ viewport: { width: 400, height: 860 }, hasTouch: true, isMobile: true });

  test("nothing truncates: the row and its title fit, the split button takes the width under the summary, every control is 44 px", async ({
    page,
  }) => {
    await installApi(page, { codeRepos: codeReposFixture(), trackerSources: trackerSourcesFixture() });
    const errors = collectErrors(page);
    await page.goto("/repositories");
    const c = card(page);
    await expect(c).toBeVisible();
    const title = c.getByRole("heading", { level: 2, name: CODE_REPO });
    await expect(title).toHaveText(CODE_REPO);
    await noOverflow(title);
    await noOverflow(c);
    // The page itself does not scroll sideways.
    const doc = await page.evaluate(() => ({
      sw: document.documentElement.scrollWidth,
      cw: document.documentElement.clientWidth,
    }));
    expect(doc.sw).toBeLessThanOrEqual(doc.cw);
    // The action sits under the summary line and takes the row's width.
    const summary = c.getByText("3 branches · 31,539 files · indexed 2h ago · not joined");
    const sb = await boxOf(summary);
    const join = c.getByRole("button", { name: "Join", exact: true });
    const jb = await boxOf(join);
    expect(jb.y).toBeGreaterThan(sb.y + sb.height - 1);
    const more = await boxOf(c.getByRole("button", { name: `More actions for ${CODE_REPO}` }));
    const row = await boxOf(c.locator(".list-row"));
    expect(more.x + more.width - jb.x).toBeGreaterThan(row.width - 40);
    // Folded, the row is short, and the disclosure itself is a 44 px target.
    expect(row.height).toBeLessThan(200);
    const toggle = c.getByRole("button", { name: CODE_REPO, exact: true });
    expect((await boxOf(toggle)).height).toBeGreaterThanOrEqual(MIN_TARGET_PX);
    await shot(page, "17-repositories-phone-folded");
    await unfold(c, CODE_REPO);
    await noOverflow(c);
    // The branch table stacks: each branch is a card of Branch · Health · Last indexed.
    const table = c.getByRole("table", { name: `${CODE_REPO} branches` });
    await expect(table.getByRole("row", { name: /master/ }).getByText("Up to date")).toBeVisible();
    await expect(table.getByRole("row", { name: /master/ }).getByText("31,539")).toBeHidden();
    // Every control on the row is a 44 px target under a coarse pointer.
    const buttons = c.getByRole("button");
    const n = await buttons.count();
    expect(n).toBeGreaterThanOrEqual(6);
    for (let i = 0; i < n; i++) {
      const b = buttons.nth(i);
      if (!(await b.isVisible())) continue;
      const box = await boxOf(b);
      const name = (await b.getAttribute("aria-label")) ?? (await b.innerText());
      expect(box.height, `${name} height`).toBeGreaterThanOrEqual(MIN_TARGET_PX);
      expect(box.width, `${name} width`).toBeGreaterThanOrEqual(MIN_TARGET_PX);
    }
    await expect(c.getByText("Up to date", { exact: true }).first()).toBeVisible();
    await shot(page, "17-repositories-phone");
    expect(errors).toEqual([]);
  });
});
