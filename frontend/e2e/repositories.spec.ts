import { expect, test, type Locator, type Page } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import {
  boxOf,
  CODE_REPO,
  codeReposFixture,
  installApi,
  trackerSourcesFixture,
  TRACKER_PRIVATE_NS,
  TRACKER_PUBLIC_NS,
} from "./fixtures";

// A repository is one card with two facets: `code.repos` and `tracker.source.list` join
// by repository into one card; the Code facet keeps the branch chips; the Issues & PRs
// facet is the tracker namespace(s) whose `repos` name the repository, chips named by
// scope; five verbs on both facets; the page lives at /repositories;
// at 400 px nothing truncates and every control is 44 px under a coarse pointer. A card
// is folded by default — one row with the standing, a one-line summary and the primary
// verb — and its heading is the disclosure that opens the facets.

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

  test("one card for ClickHouse/ClickHouse with both facets; five verbs; joins call namespace.join once each; diagnostics only for a maintainer", async ({
    page,
  }) => {
    const calls = await installApi(page, { codeRepos: codeReposFixture(), trackerSources: trackerSourcesFixture() });
    const errors = collectErrors(page);
    await page.goto("/repositories");
    await expect(page.getByRole("heading", { name: "Repositories" })).toBeVisible();
    await expect(page).toHaveURL(/\/repositories$/);
    // No "tracker sources" heading; the plane is named by its contents.
    await expect(page.getByRole("heading", { name: "tracker sources" })).toHaveCount(0);
    await expect(page.getByRole("region", { name: /^tracker source/ })).toHaveCount(0);

    const c = card(page);
    await expect(c).toHaveCount(1);
    await expect(c.getByRole("heading", { level: 2, name: CODE_REPO })).toBeVisible();
    await expect(c.getByText("not joined", { exact: true })).toBeVisible();
    // Folded: the row carries the summary and the primary verb, no facet rows yet.
    await expect(c.getByText("3 branches · 31,539 files · 22,579 issues · 65,328 PRs")).toBeVisible();
    await expect(c.getByRole("button", { name: "Join · master" })).toBeVisible();
    await expect(c.getByRole("group", { name: "Code" })).toHaveCount(0);
    await expect(c.getByRole("group", { name: "Tracker" })).toHaveCount(0);
    await shot(page, "14-repositories-desktop-folded");
    await unfold(c, CODE_REPO);
    await expect(c.getByText("3 branches · 31,539 files · 22,579 issues · 65,328 PRs")).toHaveCount(0);
    const code = c.getByRole("group", { name: "Code" });
    await expect(code.getByText("3 branches", { exact: true })).toBeVisible();
    await expect(code.getByText(/31,539 files · 412,000 symbols at master/)).toBeVisible();
    await expect(code.getByText(/indexed 2h ago/)).toBeVisible();
    await expect(code.getByRole("button", { name: "master: not joined, join" })).toBeVisible();
    await expect(code.getByRole("button", { name: "25.8: not joined, join" })).toBeVisible();
    await expect(code.getByRole("button", { name: "25.7: not joined, request access" })).toBeVisible();
    const rec = c.getByRole("group", { name: "Tracker" });
    await expect(rec.getByText("22,579 issues · 65,328 PRs")).toBeVisible();
    await expect(rec.getByText(/refreshed/)).toBeVisible();
    await expect(rec.getByRole("button", { name: "Public: not joined, join" })).toBeVisible();
    // The fixture's private namespace declares ClickHouse/example-private only, so its
    // chip sits on that record-only card, not here.
    await expect(rec.getByRole("button", { name: /Private/ })).toHaveCount(0);
    // Diagnostics are not on a reader's card; no slug in any button.
    await expect(c.getByText("qa.example:9440")).toHaveCount(0);
    await expect(c.getByText(/contract/)).toHaveCount(0);
    await expect(c.getByText("Details")).toHaveCount(0);
    await expect(c.getByRole("button", { name: /tracker-clickhouse/ })).toHaveCount(0);
    await expect(c.getByRole("button", { name: /Ask to maintain/ })).toHaveCount(0);
    // The private namespace declares ClickHouse/example-private, and clickstack
    // names ClickHouse/clickstack: each gets a record-only card headed by that repository.
    const priv = page.getByRole("region", { name: "ClickHouse/example-private", exact: true });
    await expect(priv).toBeVisible();
    await expect(priv.getByText("restricted", { exact: true })).toBeVisible();
    await unfold(priv, "ClickHouse/example-private");
    await expect(priv.getByRole("group", { name: "Code" })).toHaveCount(0);
    const privRec = priv.getByRole("group", { name: "Tracker" });
    await expect(
      privRec.getByRole("button", { name: "Private (restricted): not joined, request access" }),
    ).toBeVisible();
    await expect(priv.getByRole("button", { name: "Request access · tracker" })).toBeVisible();
    const cs = page.getByRole("region", { name: "ClickHouse/clickstack", exact: true });
    await expect(cs.getByText("tracker: no credential")).toBeVisible();
    await unfold(cs, "ClickHouse/clickstack");
    await expect(cs.getByText("no credential", { exact: true })).toBeVisible();
    await expect(cs.getByText("Public · registering…")).toBeVisible();
    await expect(cs.getByRole("button", { name: /^Join/ })).toHaveCount(0);
    expect(calls.trackerSources).toBe(1);
    expect(calls.join).toEqual([]);
    await shot(page, "15-repositories-desktop-before-join");

    // Header: Join the open trunk directly — one namespace.join.
    await c.getByRole("button", { name: "Join · master" }).click();
    await expect(code.getByRole("button", { name: "master: reading" })).toBeVisible();
    expect(calls.join).toEqual([{ op: "join", org: "dev", namespace: "code-clickhouse", role: "viewer" }]);
    await expect(c.getByRole("button", { name: "Open · master" })).toBeVisible();
    await expect(c.getByRole("button", { name: "Leave · master" })).toBeVisible();
    await expect(c.getByText("you read 1 of 3 branches · tracker: not joined")).toBeVisible();

    // Issues & PRs facet: the Public chip confirms, then one namespace.join with the tracker namespace.
    await rec.getByRole("button", { name: "Public: not joined, join" }).click();
    let dialog = page.getByRole("dialog");
    await expect(dialog.getByText("Join the tracker?")).toBeVisible();
    await dialog.getByRole("button", { name: "Join", exact: true }).click();
    await expect(rec.getByRole("button", { name: "Public: reading" })).toBeVisible();
    expect(calls.join[1]).toEqual({ op: "join", org: "dev", namespace: TRACKER_PUBLIC_NS, role: "viewer" });
    await expect(page.locator("[data-toast-viewport]")).toContainText(
      `Joined ${TRACKER_PUBLIC_NS} (${CODE_REPO}) · read-only`,
    );
    await expect(c.getByText("you read 1 of 3 branches · tracker: reading")).toBeVisible();
    // Two things read: the header's quiet Leave gives way to one per facet.
    await expect(c.getByRole("button", { name: "Leave · master" })).toBeVisible();
    await expect(rec.getByRole("button", { name: "Leave · tracker" })).toBeVisible();

    // Request access on the restricted record facet (the record-only card).
    await privRec.getByRole("button", { name: "Private (restricted): not joined, request access" }).click();
    dialog = page.getByRole("dialog");
    await expect(dialog.getByText("Request access to the tracker?")).toBeVisible();
    await expect(dialog.getByText(/Private tracker\./)).toBeVisible();
    await dialog.getByRole("button", { name: "Request access" }).click();
    await expect(privRec.getByRole("button", { name: "Private (restricted): requested" })).toBeVisible();
    await expect(priv.getByRole("button", { name: "Withdraw request · tracker" })).toBeVisible();
    expect(calls.join[2]).toEqual({ op: "join", org: "dev", namespace: TRACKER_PRIVATE_NS, role: "viewer" });
    await expect(page.locator("[data-toast-viewport]")).toContainText("Access requested");

    // Request access on the restricted branch chip: the request dialog with its role choice.
    await code.getByRole("button", { name: "25.7: not joined, request access" }).click();
    dialog = page.getByRole("dialog");
    await expect(dialog.getByRole("radio", { name: /Reader/ })).toBeChecked();
    await expect(dialog.getByRole("radio", { name: /Maintainer/ })).toBeVisible();
    await dialog.getByRole("button", { name: "Request access" }).click();
    await expect(code.getByRole("button", { name: "25.7: requested" })).toBeVisible();
    expect(calls.join[3]).toEqual({ op: "join", org: "dev", namespace: "code-clickhouse-25-7", role: "viewer" });
    await expect(c.getByText("you read 1 of 3 branches · tracker: reading")).toBeVisible();
    // The branch verbs on a partly joined card.
    await expect(code.getByRole("button", { name: "Choose branches…" })).toBeVisible();
    await expect(code.getByRole("button", { name: "Request maintainer access" })).toHaveCount(0); // a request is pending
    await shot(page, "16-repositories-desktop-joined-requested");

    // Open the record: the picker on the tracker namespace, the Graph view in Patterns mode.
    await rec.getByRole("button", { name: "Public: reading" }).click();
    await expect(page).toHaveURL(/\/graph\?mode=patterns$/);
    await expect(page.getByRole("button", { name: `Namespace: ${TRACKER_PUBLIC_NS}` })).toBeVisible();
    await expect(page.getByRole("button", { name: "Patterns" })).toHaveAttribute("aria-pressed", "true");
    expect(errors).toEqual([]);
  });

  test("the sidebar link says Repositories and points to /repositories", async ({ page }) => {
    await installApi(page, { codeRepos: codeReposFixture() });
    await page.goto("/repositories?repo=");
    await expect(page.getByRole("heading", { name: "Repositories" })).toBeVisible();
    await expect(page).toHaveURL(/\/repositories/);
    const link = page.getByRole("link", { name: "Repositories" }).first();
    await expect(link).toHaveAttribute("href", "/repositories");
  });

  test("a server without tracker.source.list: the cards render, one stated line, no error banner", async ({ page }) => {
    const calls = await installApi(page, { codeRepos: codeReposFixture() });
    await page.goto("/repositories");
    await expect(card(page)).toBeVisible();
    await expect(page.getByText("Trackers are not available on this deployment yet.")).toBeVisible();
    await expect(page.getByRole("alert")).toHaveCount(0);
    await unfold(card(page), CODE_REPO);
    await expect(card(page).getByRole("group", { name: "Code" })).toBeVisible();
    await expect(card(page).getByRole("group", { name: "Tracker" })).toHaveCount(0);
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
    await expect(page.getByText("Details")).toHaveCount(0);
  });
});

test.describe("phone 400, coarse pointer", () => {
  test.use({ viewport: { width: 400, height: 860 }, hasTouch: true, isMobile: true });

  test("nothing truncates: the card and its title fit, the actions stack under the header, every chip and verb is 44 px", async ({
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
    // The action column sits under the header: the primary button starts below the title.
    const tb = await boxOf(title);
    const pb = await boxOf(c.getByRole("button", { name: "Join · master" }));
    expect(pb.y).toBeGreaterThan(tb.y + tb.height - 1);
    // Folded, the row is short: three stacked lines and the verb, and the disclosure
    // itself is a 44 px target.
    const folded = await boxOf(c);
    expect(folded.height).toBeLessThan(260);
    const toggle = c.getByRole("button", { name: CODE_REPO, exact: true });
    expect((await boxOf(toggle)).height).toBeGreaterThanOrEqual(44);
    await shot(page, "17-repositories-phone-folded");
    await unfold(c, CODE_REPO);
    await noOverflow(c);
    // Every control on the card is a 44 px target under a coarse pointer.
    const buttons = c.getByRole("button");
    const n = await buttons.count();
    expect(n).toBeGreaterThanOrEqual(6);
    for (let i = 0; i < n; i++) {
      const b = buttons.nth(i);
      const box = await boxOf(b);
      const name = (await b.getAttribute("aria-label")) ?? (await b.innerText());
      expect(box.height, `${name} height`).toBeGreaterThanOrEqual(44);
      expect(box.width, `${name} width`).toBeGreaterThanOrEqual(44);
    }
    // The open/restricted pill and the standing phrase are whole.
    await expect(c.getByText("open", { exact: true })).toBeVisible();
    await noOverflow(c.getByText("open", { exact: true }));
    await expect(c.getByText("not joined", { exact: true })).toBeVisible();
    await shot(page, "17-repositories-phone");
    expect(errors).toEqual([]);
  });
});
