import { expect, test, type Locator, type Page } from "@playwright/test";
import { boxOf, installApi, isRecord, MIN_TARGET_PX } from "./fixtures";
import { violations } from "./axe";

const DESTINATION = "col-research";
const SUBJECT = "writer@example.com";
const LINKS = [
  "https://drive.google.com/file/d/1ExampleSelectedPdf/view",
  "https://docs.google.com/document/d/1ExampleSelectedDoc/edit",
];

// These fixtures exercise browser wiring and ordinary-user permissions. They do not
// qualify provider consent, ingestion, extraction or provider-permission mirroring.
async function installConnectedSource(page: Page) {
  await installApi(page, {
    viewer: true,
    savedNamespace: "bench",
    claims: ["bench", DESTINATION],
    mine: [
      {
        namespace: "bench",
        role: "writer",
        status: "active",
        visibility: "private",
        kind: "memory",
        description: "",
        joined_at: "2026-01-01T00:00:00Z",
        is_new: false,
      },
    ],
    collections: [
      { namespace: DESTINATION, parent: "", title: "Research", personal: false, role: "writer", description: "" },
    ],
    knowledgeSources: [],
  });
  const state = {
    connected: true,
    added: false,
    complete: false,
    refusedCheck: false,
    consentCalls: 0,
    accountReads: 0,
    sourceCalls: [] as { path: string; body: Record<string, unknown> }[],
  };
  await page.route("**/v1/whoami", (route) =>
    route.fulfill({
      json: {
        authenticated: true,
        subject: SUBJECT,
        org: "dev",
        namespaces: ["bench", DESTINATION],
        roles: ["WRITER"],
        is_service_account: false,
      },
    }),
  );
  await page.route("**/v1/connection.list", (route) => {
    state.accountReads++;
    const body: unknown = route.request().postDataJSON();
    expect(body).toMatchObject({ org: "dev" });
    expect(body).not.toHaveProperty("subject");
    expect(body).not.toHaveProperty("namespace");
    return route.fulfill({
      json: {
        connections: ["gdrive", "notion", "linear"].map((kind) => ({
          kind,
          connected: kind === "gdrive" && state.connected,
          connect_available: true,
          account_email: kind === "gdrive" && state.connected ? SUBJECT : null,
          scopes: [],
          connect_url: `/settings/connected-accounts?kind=${kind}`,
        })),
      },
    });
  });
  await page.route("**/v1/connect/**", (route) => {
    state.consentCalls++;
    return route.fulfill({ status: 400, json: { detail: "Consent must not start during source registration." } });
  });
  await page.route("**/v1/knowledge.source.*", (route) => {
    const path = new URL(route.request().url()).pathname;
    const raw: unknown = route.request().postDataJSON();
    if (!isRecord(raw)) throw new Error("Expected a source request body");
    state.sourceCalls.push({ path, body: raw });
    if (path.endsWith(".list")) {
      return route.fulfill({
        json: { sources: state.added ? [sourceRow(state.complete)] : [], enabled: true, accepts_sources: true },
      });
    }
    if (path.endsWith(".parse_links")) {
      expect(raw.links).toEqual(LINKS);
      return route.fulfill({
        json: {
          links: LINKS.map((link, index) => ({
            link,
            kind: "gdrive",
            item: { type: "file", id: index === 0 ? "selected-file" : "selected-doc" },
            unreadable: "",
          })),
        },
      });
    }
    if (path.endsWith(".add")) {
      if (raw.dry_run && !state.refusedCheck) {
        state.refusedCheck = true;
        return route.fulfill({
          status: 403,
          json: { detail: { message: "Selected content could not be read. Check its sharing settings." } },
        });
      }
      if (!raw.dry_run) state.added = true;
      return route.fulfill({
        json: {
          name: "selected-reading",
          kind: "gdrive",
          namespace: DESTINATION,
          config: {},
          dry_run: raw.dry_run,
          probe: { documents: 0, more: false, sample: [], checked: 2, sampled: false },
        },
      });
    }
    return route.fulfill({ status: 400, json: { detail: "Unexpected source operation in this flow." } });
  });
  return state;
}

function sourceRow(complete: boolean) {
  return {
    name: "selected-reading",
    kind: "gdrive",
    credential_mode: "connected",
    owner_subject: SUBJECT,
    owned_by_caller: true,
    config: {},
    items: [],
    created_by: SUBJECT,
    created_at: "2026-01-01T00:00:00Z",
    sync: {
      requested_at: "",
      last_synced_at: complete ? "2026-01-01T00:01:00Z" : "",
      status: complete ? "ok" : "",
      documents_seen: complete ? 2 : 0,
      changed: complete ? 2 : 0,
      removed: 0,
      unconverted: 0,
      unconverted_reasons: {},
      skipped: 0,
      error: "",
    },
  };
}

async function checkLayout(page: Page, dialog: Locator, width: number) {
  expect(await violations(page)).toEqual([]);
  expect(await dialog.evaluate((el) => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  if (width === 375) {
    for (const button of await dialog.getByRole("button").all()) {
      if (!(await button.isVisible())) continue;
      const box = await boxOf(button);
      expect(box.width).toBeGreaterThanOrEqual(MIN_TARGET_PX);
      expect(box.height).toBeGreaterThanOrEqual(MIN_TARGET_PX);
    }
  }
}

async function checkConnectionTab(
  page: Page,
  dialog: Locator,
  state: { connected: boolean; consentCalls: number },
  screenshot: string,
) {
  const origin = new URL(page.url()).origin;
  const blocked = { external: 0, mcp: 0 };
  // Page routes remain authoritative for the source tab. The popup gets explicit
  // own-account fixtures; every other API request is refused before reaching a server.
  await page.context().route("**/*", (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== origin) {
      blocked.external++;
      return route.abort("blockedbyclient");
    }
    if (url.pathname === "/mcp" || url.pathname.startsWith("/mcp/")) {
      blocked.mcp++;
      return route.abort("blockedbyclient");
    }
    if (!url.pathname.startsWith("/v1/") && url.pathname !== "/v1") return route.continue();
    if (url.pathname === "/v1/health") return route.fulfill({ json: {} });
    if (url.pathname === "/v1/whoami") {
      return route.fulfill({
        json: {
          authenticated: true,
          subject: SUBJECT,
          org: "dev",
          namespaces: ["bench", DESTINATION],
          roles: ["WRITER"],
          is_service_account: false,
        },
      });
    }
    if (url.pathname === "/v1/namespace.list_mine") {
      return route.fulfill({
        json: { rows: [{ namespace: "bench", role: "writer", status: "active", kind: "memory" }] },
      });
    }
    if (url.pathname === "/v1/connection.list") {
      const body: unknown = route.request().postDataJSON();
      expect(body).toMatchObject({ org: "dev" });
      expect(body).not.toHaveProperty("subject");
      expect(body).not.toHaveProperty("namespace");
      return route.fulfill({
        json: {
          connections: ["gdrive", "notion", "linear"].map((kind) => ({
            kind,
            connected: kind === "gdrive" && state.connected,
            connect_available: true,
            account_email: kind === "gdrive" && state.connected ? SUBJECT : null,
            scopes: [],
            connect_url: `/settings/connected-accounts?kind=${kind}`,
          })),
        },
      });
    }
    if (url.pathname.startsWith("/v1/connect/")) state.consentCalls++;
    return route.fulfill({ status: 503, json: { detail: "This request has no browser contract fixture." } });
  });
  const [popup] = await Promise.all([
    page.context().waitForEvent("page"),
    dialog.getByRole("link", { name: /^Connect Google Drive/ }).click(),
  ]);
  try {
    await expect(popup.getByRole("heading", { name: "Connected accounts", exact: true })).toBeVisible();
    await expect(popup.getByText("After connecting, return to the tab where you are adding a source.")).toBeVisible();
    const url = new URL(popup.url());
    expect(url.origin).toBe(origin);
    expect(url.pathname).toBe("/settings/connected-accounts");
    expect([...url.searchParams.keys()].sort()).toEqual(["from", "kind", "namespace"]);
    expect(url.searchParams.get("kind")).toBe("gdrive");
    expect(url.searchParams.get("from")).toBe("source");
    expect(url.searchParams.get("namespace")).toBe("bench");
    for (const provider of ["Google Drive", "Notion", "Linear"])
      await expect(popup.getByRole("button", { name: `Connect ${provider}`, exact: true })).toBeVisible();
    await expect(popup.getByRole("heading", { name: "Google Drive", exact: true })).toBeFocused();
    await expect(popup.locator('input[type="password"]')).toHaveCount(0);
    expect(state.consentCalls).toBe(0);
    expect(blocked).toEqual({ external: 0, mcp: 0 });
    await popup.screenshot({ path: screenshot, fullPage: true });
  } finally {
    await popup.close();
  }
  await page.bringToFront();
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel(/^Name /)).toHaveValue("selected-reading");
  await expect(dialog.getByLabel("Content links (one per line)")).toHaveValue(LINKS.join("\n"));
}

for (const width of [1400, 375]) {
  test.describe(`${width} px connected source`, () => {
    test.use({ viewport: { width, height: 900 }, hasTouch: width === 375 });

    test("an ordinary writer checks selected content, preserves a refused draft and adds to the chosen collection", async ({
      page,
    }, testInfo) => {
      const state = await installConnectedSource(page);
      await page.goto(`/library?collection=${DESTINATION}`);
      const sources = page.getByRole("button", { name: "Sources", exact: true });
      await sources.focus();
      await page.keyboard.press("Enter");
      await expect(page.getByRole("heading", { name: "Sources", exact: true })).toBeFocused();
      const opener = page.getByRole("button", { name: "Add a source", exact: true });
      await opener.focus();
      await page.keyboard.press("Enter");
      const dialog = page.getByRole("dialog", { name: "Add a document source · Research", exact: true });
      const name = dialog.getByLabel(/^Name /);
      const links = dialog.getByLabel("Content links (one per line)");
      const check = dialog.getByRole("button", { name: "Check", exact: true });
      const add = dialog.getByRole("button", { name: "Add source", exact: true });
      await expect(name).toBeFocused();
      await expect(dialog.getByRole("radio", { name: "My connected account", exact: true })).toBeChecked();
      await expect(dialog.getByText(`Using ${SUBJECT}.`, { exact: true })).toBeVisible();
      await expect(dialog.getByRole("radio", { name: "Shared credential", exact: true })).toHaveCount(0);
      await expect(dialog.locator('input[type="password"]')).toHaveCount(0);
      await name.fill("selected-reading");
      await links.fill(LINKS.join("\n"));
      await expect(add).toBeDisabled();
      await check.click();
      await expect(dialog.getByRole("alert")).toContainText("Selected content could not be read.");
      await expect(name).toHaveValue("selected-reading");
      await expect(links).toHaveValue(LINKS.join("\n"));
      await expect(add).toBeDisabled();
      await check.click();
      await expect(dialog.getByText("Access checked for 2 items. This check does not import documents.")).toBeVisible();
      await expect(add).toBeEnabled();

      state.connected = false;
      await dialog.getByRole("button", { name: "Refresh account", exact: true }).click();
      await expect(dialog.getByText("Connect your Google Drive account to continue.", { exact: true })).toBeVisible();
      await expect(add).toBeDisabled();
      await expect(check).toBeDisabled();
      await expect(links).toHaveValue(LINKS.join("\n"));
      await checkConnectionTab(page, dialog, state, testInfo.outputPath(`connected-source-${width}-accounts.png`));
      state.connected = true;
      await dialog.getByRole("button", { name: "Refresh account", exact: true }).click();
      await expect(dialog.getByText(`Using ${SUBJECT}.`, { exact: true })).toBeVisible();
      await expect(add).toBeDisabled();
      await check.click();
      await expect(add).toBeEnabled();
      await checkLayout(page, dialog, width);
      await page.screenshot({ path: testInfo.outputPath(`connected-source-${width}-checked.png`), fullPage: true });
      await add.click();
      await expect(dialog).toHaveCount(0);
      await expect(opener).toBeFocused();
      const rows = page.getByRole("list", { name: "Document sources" });
      await expect(rows).toContainText("Never synced");
      state.complete = true;
      await page.getByRole("button", { name: "Refresh sources", exact: true }).click();
      await expect(rows).toContainText("Synced");
      await expect(rows).toContainText("2 documents listed · 2 written");
      await rows.getByRole("button", { name: "Actions for selected-reading", exact: true }).click();
      await expect(page.getByRole("menuitem", { name: "Manage connection", exact: true })).toBeVisible();
      await expect(page.getByRole("menuitem", { name: "Rotate credential", exact: true })).toHaveCount(0);
      await expect(page.getByRole("menuitem", { name: "Push notifications", exact: true })).toHaveCount(0);
      await page.keyboard.press("Escape");
      expect(await violations(page)).toEqual([]);
      await page.screenshot({ path: testInfo.outputPath(`connected-source-${width}-synced.png`), fullPage: true });

      const adds = state.sourceCalls.filter(({ path }) => path.endsWith(".add"));
      expect(adds.map(({ body }) => body.dry_run)).toEqual([true, true, true, false]);
      for (const { path, body } of state.sourceCalls) {
        expect(body).toMatchObject({ org: "dev", namespace: DESTINATION });
        if (!path.endsWith(".add")) continue;
        expect(body).toMatchObject({
          name: "selected-reading",
          kind: "gdrive",
          credential_mode: "connected",
          links: LINKS,
          include: ["docs", "slides", "pdf", "pptx"],
        });
        for (const field of ["credential", "config", "subject", "owner_subject", "account_id"])
          expect(body).not.toHaveProperty(field);
      }
      expect(state.accountReads).toBeGreaterThanOrEqual(3);
      expect(state.consentCalls).toBe(0);
    });
  });
}
