import { expect, test, type Page } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import {
  CODE_NS,
  CODE_TRACKER_ONLY_CLAIMS,
  CODE_TRACKER_ONLY_MINE,
  NAMESPACE,
  TRACKER_PUBLIC_NS,
  installApi,
} from "./fixtures";

// A token that names only code and/or QA namespaces lands on Repositories with the picker
// on its first reachable code or tracker namespace, and makes no memory.* request; an
// explicit path always wins — no deep link is redirected to Setup; a memory view on such
// a token renders its own empty state; the memory-only fixture lands on Memories. The
// API is answered locally (fixtures.ts). Bare `/` is visited as `/`: `vite preview`
// serves only the base path with its slash, and `pathnameView` reads both as bare.

const SHOTS = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "../e2e-screenshots");

async function shot(page: Page, name: string): Promise<void> {
  fs.mkdirSync(SHOTS, { recursive: true });
  const file = path.join(SHOTS, `${name}.png`);
  await page.screenshot({ path: file });
  await test.info().attach(name, { path: file, contentType: "image/png" });
}

function collectErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  return errors;
}

const CODE_TRACKER_ONLY = {
  viewer: true,
  claims: CODE_TRACKER_ONLY_CLAIMS,
  mine: CODE_TRACKER_ONLY_MINE,
  savedNamespace: null,
} as const;

test("bare / with a code+QA-only token lands on Repositories, picker on the code namespace, zero memory.* requests", async ({
  page,
}) => {
  const calls = await installApi(page, CODE_TRACKER_ONLY);
  const errors = collectErrors(page);
  await page.goto("/");
  await expect(page).toHaveURL(new RegExp(`/repositories\\?namespace=${CODE_NS}$`));
  await expect(page.getByRole("heading", { name: "Repositories", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Settings", exact: true })).toHaveCount(0);
  const picker = page.getByRole("button", { name: `Namespace: ${CODE_NS}` });
  await expect(picker).toBeVisible();
  await expect(page.getByRole("status").filter({ hasText: "No namespace selected" })).toHaveCount(0);

  // The picker groups the namespaces by kind: the current code index opens its group, and
  // the tracker records (a grants-only row) sit folded under their own heading.
  await picker.click();
  const list = page.getByLabel("Namespaces");
  await expect(list.getByRole("button", { name: /code-clickhouse/ })).toBeVisible();
  const trackers = list.getByRole("button", { name: /^Tracker records/ });
  await expect(trackers).toHaveAttribute("aria-expanded", "false");
  await trackers.click();
  await expect(list.getByRole("button", { name: /tracker-clickhouse/ })).toBeVisible();
  await shot(page, "15-landing-code-tracker-only-repositories");
  await page.keyboard.press("Escape");

  expect(calls.memory).toEqual([]);
  expect(calls.search).toEqual([]);
  expect(errors).toEqual([]);
});

test("bare / honours a saved QA pick: Repositories with the picker on it", async ({ page }) => {
  const calls = await installApi(page, { ...CODE_TRACKER_ONLY, savedNamespace: TRACKER_PUBLIC_NS });
  await page.goto("/");
  await expect(page).toHaveURL(new RegExp(`/repositories\\?namespace=${TRACKER_PUBLIC_NS}$`));
  await expect(page.getByRole("button", { name: `Namespace: ${TRACKER_PUBLIC_NS}` })).toBeVisible();
  expect(calls.memory).toEqual([]);
});

test("a deep link into Patterns mode with a code+QA-only token stays on the graph", async ({ page }) => {
  const calls = await installApi(page, { ...CODE_TRACKER_ONLY, trackerBound: true });
  const errors = collectErrors(page);
  await page.goto("/graph?mode=patterns");
  await expect(page).toHaveURL(new RegExp(`/graph\\?namespace=${CODE_NS}&mode=patterns$`));
  await expect(page.getByRole("heading", { name: "Settings", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: `Namespace: ${CODE_NS}` })).toBeVisible();
  await expect(page.getByRole("grid")).toBeVisible();
  await expect(page.getByRole("button", { name: "Patterns" })).toHaveAttribute("aria-pressed", "true");
  await expect.poll(() => calls.qa.filter((c) => c.op === "binding").length).toBeGreaterThan(0);
  expect(calls.qa.find((c) => c.op === "binding")).toMatchObject({ namespace: CODE_NS });
  await shot(page, "16-landing-code-tracker-only-patterns-deep-link");
  expect(calls.memory).toEqual([]);
  expect(errors).toEqual([]);
});

test("/repositories with a memory-only token stays on Repositories", async ({ page }) => {
  await installApi(page);
  await page.goto("/repositories");
  await expect(page).toHaveURL(new RegExp(`/repositories\\?namespace=${NAMESPACE}$`));
  await expect(page.getByRole("heading", { name: "Repositories", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: `Namespace: ${NAMESPACE}` })).toBeVisible();
});

test("/memories with a code-only token stays on Memories with its empty state and zero memory.* requests", async ({
  page,
}) => {
  const calls = await installApi(page, {
    viewer: true,
    claims: [CODE_NS],
    mine: [CODE_TRACKER_ONLY_MINE[0]],
    savedNamespace: null,
  });
  const errors = collectErrors(page);
  await page.goto("/memories");
  await expect(page).toHaveURL(/\/memories$/);
  await expect(page.getByRole("heading", { name: "Memories", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "No memory namespace to read" })).toBeVisible();
  await expect(page.getByText("No namespace selected")).toBeVisible();
  await shot(page, "17-memories-code-only-empty-state");
  expect(calls.memory).toEqual([]);
  expect(calls.search).toEqual([]);
  expect(errors).toEqual([]);
});

test("a memory view on a picked code namespace names the kind and offers Repositories, without a memory.* request", async ({
  page,
}) => {
  const calls = await installApi(page, { ...CODE_TRACKER_ONLY, savedNamespace: CODE_NS });
  await page.goto("/memories");
  await expect(page.getByRole("heading", { name: `${CODE_NS} is a repository index` })).toBeVisible();
  // The ScopeBadge names the namespace with its kind.
  await expect(page.locator("#main").getByText(CODE_NS, { exact: true }).locator("..")).toContainText("code");
  await page.getByRole("button", { name: "Open repositories" }).click();
  await expect(page).toHaveURL(new RegExp(`/repositories\\?namespace=${CODE_NS}$`));
  await expect(page.getByRole("heading", { name: "Repositories", exact: true })).toBeVisible();
  expect(calls.memory).toEqual([]);
});

test("the memory-only fixture lands on Memories on bare /", async ({ page }) => {
  await installApi(page);
  await page.goto("/");
  await expect(page).toHaveURL(/\/memories/);
  await expect(page.getByRole("heading", { name: "Memories", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: `Namespace: ${NAMESPACE}` })).toBeVisible();
  await expect(page.getByRole("status").filter({ hasText: "No namespace selected" })).toHaveCount(0);
});

test("an account that reads only collections lands on the Library", async ({ page }) => {
  await installApi(page, { viewer: true, claims: [], mine: [], savedNamespace: null });
  await page.goto("/");
  await expect(page).toHaveURL(/\/library/);
  await expect(page.getByText(/can reach no namespace yet/)).toHaveCount(0);
});

test("a token that reaches nothing lands on Setup with the join-or-grant notice and a button to Repositories", async ({
  page,
}) => {
  const calls = await installApi(page, { viewer: true, claims: [], mine: [], savedNamespace: null, collections: [] });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Settings", exact: true })).toBeVisible();
  const notice = page.getByRole("status").filter({ hasText: "No namespace selected" });
  await expect(notice).toBeVisible();
  await expect(notice).toContainText(
    "Your token can reach no namespace yet. Join a repository or tracker source from Repositories, or ask for a grant.",
  );
  await expect(notice).not.toContainText("memory namespace");
  await shot(page, "18-landing-nothing-reachable-setup");
  await notice.getByRole("button", { name: "Open repositories" }).click();
  await expect(page).toHaveURL(/\/repositories$/);
  await expect(page.getByRole("heading", { name: "Repositories", exact: true })).toBeVisible();
  expect(calls.memory).toEqual([]);
});
