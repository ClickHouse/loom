import { expect, test, type Page } from "@playwright/test";
import { violations } from "./axe";
import { boxOf, CODE_NS, installApi, NAMESPACE } from "./fixtures";

// The header namespace picker with memory namespaces, code indexes and tracker records:
// the code and tracker groups fold, the open picker is axe-clean at desktop and phone
// width, and Up / Down rove over the options and the group buttons.

const row = (namespace: string, kind: string, role: string, description = "") => ({
  namespace,
  role,
  status: "active",
  visibility: "private",
  kind,
  description,
  joined_at: "2026-01-01T00:00:00Z",
  is_new: false,
});

const MINE = [
  row(NAMESPACE, "memory", "admin"),
  row("team-notes", "memory", "writer"),
  row("ops-runbooks", "memory", "viewer"),
  row(CODE_NS, "code", "jwt", "ClickHouse/ClickHouse @ master"),
  row("code-loom", "code", "jwt", "acme/loom @ main"),
  row("code-docs", "code", "jwt", "acme/docs @ main"),
  row("tracker-clickhouse", "qa", "viewer", "ClickHouse issues and PRs"),
];

async function openPicker(page: Page) {
  await installApi(page, { mine: MINE, claims: MINE.map((r) => r.namespace) });
  await page.goto("/memories");
  await expect(page.locator("main h1").first()).toBeVisible();
  await page.waitForLoadState("networkidle");
  await page.getByRole("button", { name: /^Namespace: / }).click();
  const dialog = page.getByRole("dialog", { name: "Namespaces" });
  await expect(dialog).toBeVisible();
  return dialog;
}

for (const width of [1400, 375]) {
  test(`the open picker with code and tracker groups is axe-clean at ${width} px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const dialog = await openPicker(page);
    const code = dialog.getByRole("button", { name: /^Code indexes/ });
    await expect(code).toHaveAttribute("aria-expanded", "false");
    await code.click();
    await expect(dialog.getByRole("button", { name: /ClickHouse\/ClickHouse @ master/ })).toBeVisible();
    expect(await violations(page)).toEqual([]);
  });
}

test("Up and Down rove from the search field through the options and the group buttons", async ({ page }) => {
  const dialog = await openPicker(page);
  const search = dialog.getByRole("textbox", { name: "Search namespaces" });
  await expect(search).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(dialog.getByRole("button", { name: new RegExp(`\\b${NAMESPACE}\\b`) })).toBeFocused();
  await page.keyboard.press("End");
  await expect(dialog.getByRole("button", { name: /^Tracker records/ })).toBeFocused();
  await page.keyboard.press("Enter");
  await page.keyboard.press("ArrowDown");
  await expect(dialog.getByRole("button", { name: /ClickHouse issues and PRs/ })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(page.getByRole("button", { name: /^Namespace: / })).toBeFocused();
});

test("an open name family is axe-clean, each member led by its description", async ({ page }) => {
  const family = ["A", "B", "C"].map((w) =>
    row(`group-${w.toLowerCase()}-shared`, "memory", "writer", `Workspace ${w}`),
  );
  await installApi(page, { mine: [...MINE, ...family], claims: MINE.map((r) => r.namespace) });
  await page.goto("/memories");
  await expect(page.locator("main h1").first()).toBeVisible();
  await page.getByRole("button", { name: /^Namespace: / }).click();
  const dialog = page.getByRole("dialog", { name: "Namespaces" });
  const shared = dialog.getByRole("button", { name: /^group-…/ });
  await expect(shared).toHaveAttribute("aria-expanded", "false");
  await shared.click();
  await expect(dialog.getByRole("button", { name: /Workspace B/ })).toBeVisible();
  expect(await violations(page)).toEqual([]);
});

test("a search opens the group it matches", async ({ page }) => {
  const dialog = await openPicker(page);
  await dialog.getByRole("textbox", { name: "Search namespaces" }).fill("loom");
  await expect(dialog.getByRole("button", { name: /^Code indexes/ })).toHaveAttribute("aria-expanded", "true");
  await expect(dialog.getByRole("button", { name: /acme\/loom @ main/ })).toBeVisible();
  await expect(dialog.getByRole("button", { name: /^Tracker records/ })).toHaveCount(0);
});

// The list fades out over 24 px at an edge it can still scroll past; a row reached with
// the arrow keys stops clear of the fade, never under it.
test("a row reached with Down is scrolled clear of the list's fade", async ({ page }) => {
  // No shared name prefix: three that share one fold into a group of their own.
  const many = Array.from({ length: 30 }, (_, i) => row(`team${String(i).padStart(2, "0")}`, "memory", "writer"));
  await installApi(page, { mine: [...MINE, ...many], claims: [...MINE, ...many].map((r) => r.namespace) });
  await page.goto("/memories");
  await expect(page.locator("main h1").first()).toBeVisible();
  await page.waitForLoadState("networkidle");
  await page.getByRole("button", { name: /^Namespace: / }).click();
  const dialog = page.getByRole("dialog", { name: "Namespaces" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("textbox", { name: "Search namespaces" })).toBeFocused();
  await page.keyboard.press("ArrowDown");
  for (let i = 0; i < 14; i++) await page.keyboard.press("ArrowDown");
  const gap = await page.evaluate(() => {
    const row = document.activeElement as HTMLElement;
    const list = row.closest(".scroll-fade") as HTMLElement;
    const [r, l] = [row.getBoundingClientRect(), list.getBoundingClientRect()];
    return { scrolled: list.scrollTop > 0, below: l.bottom - r.bottom, above: r.top - l.top };
  });
  expect(gap.scrolled).toBe(true);
  expect(gap.below).toBeGreaterThanOrEqual(23);
  expect(gap.above).toBeGreaterThanOrEqual(23);
});

// Click UI's popover panel is a column flex aligned to the start: the list and the footer
// are stretched across it, so a row's hover and current fills and the separator span the
// popover, not the width of the longest name.
for (const width of [1400, 375]) {
  test(`the list rows and the footer separator span the popover at ${width} px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const dialog = await openPicker(page);
    const panel = await dialog.boundingBox();
    const current = await dialog.locator(".ns-row[data-current]").boundingBox();
    const footer = await dialog.locator(".border-t").boundingBox();
    if (!panel || !current || !footer) throw new Error("picker not drawn");
    // The same inset either side: the panel's padding, less the 8 px the list bleeds into it.
    const left = current.x - panel.x;
    const right = panel.x + panel.width - (current.x + current.width);
    expect(Math.abs(left - right)).toBeLessThanOrEqual(1);
    expect(left).toBeLessThan(24);
    expect(footer.width).toBeCloseTo(current.width, 0);
  });
}

// A long name truncates in the header, whole in a tooltip on hover and focus. The picker and the
// search share the row from the `md` breakpoint up: each keeps its floor (the search's is
// wider from `lg`, where the account name joins the right cluster) and the page never
// scrolls sideways.
for (const [width, searchFloor, kind] of [
  [1400, 160, "memory"],
  [1024, 160, "memory"],
  [768, 72, "memory"],
  // A code index's icon and badge take more of the trigger; its name still shows.
  [768, 72, "code"],
] as const) {
  test(`a long ${kind} namespace truncates and leaves the header search its width at ${width} px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const long = `team-${"0123abcd-".repeat(8)}end`;
    await installApi(page, { mine: [row(long, kind, "admin")], claims: [long], savedNamespace: long });
    await page.goto("/memories");
    await expect(page.locator("main h1").first()).toBeVisible();
    const trigger = page.getByRole("button", { name: new RegExp(`^Namespace: ${long}`) });
    const name = trigger.getByText(long);
    await expect(name).toBeVisible();
    expect(await name.evaluate((el) => el.scrollWidth > el.clientWidth && el.clientWidth > 0)).toBe(true);
    expect((await boxOf(trigger)).width).toBeGreaterThanOrEqual(96);
    expect((await boxOf(page.locator("#header-search"))).width).toBeGreaterThanOrEqual(searchFloor);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    // Keyboard focus and hover each show the whole name.
    await trigger.focus();
    await expect(page.getByRole("tooltip")).toHaveText(long);
    await page.keyboard.press("Escape");
    await expect(page.getByRole("tooltip")).toHaveCount(0);
    await trigger.hover();
    await expect(page.getByRole("tooltip")).toHaveText(long);
    // The label already says the name: the tip is not read again as a description.
    await expect(trigger).not.toHaveAttribute("aria-describedby");
    // The open panel shuts the tip, and the button says the panel is open.
    await trigger.click();
    await expect(trigger).toHaveAttribute("data-state", "open");
    await expect(page.getByRole("tooltip")).toHaveCount(0);
  });
}

// From xl the assistant launcher shows its word: with the longest namespace the header still
// leaves the search its floor, so a later header item that crowds it fails here.
test("at 1280 px the launcher shows its word and the header search keeps its floor", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  const long = `team-${"0123abcd-".repeat(8)}end`;
  await installApi(page, { mine: [row(long, "memory", "admin")], claims: [long], savedNamespace: long });
  await page.goto("/memories");
  await expect(page.locator("main h1").first()).toBeVisible();
  const launcher = page.locator('[data-focus-key="assistant-launcher"]');
  await expect(launcher).toHaveAccessibleName("Assistant");
  await expect(launcher.getByText("Assistant", { exact: true })).toBeVisible();
  expect((await boxOf(page.locator("#header-search"))).width).toBeGreaterThanOrEqual(160);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("a namespace that fits has no tooltip on focus", async ({ page }) => {
  await installApi(page);
  await page.goto("/memories");
  await expect(page.locator("main h1").first()).toBeVisible();
  // Focus opens a tip at once; hover waits out the provider's delay, so focus is the check.
  await page.getByRole("button", { name: `Namespace: ${NAMESPACE}` }).focus();
  await expect(page.getByRole("tooltip")).toHaveCount(0);
});
