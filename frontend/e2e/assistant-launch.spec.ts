import { expect, test, type Page } from "@playwright/test";
import { violations } from "./axe";
import { CODE_TRACKER_ONLY_CLAIMS, CODE_TRACKER_ONLY_MINE, installApi } from "./fixtures";

// The header's Ask AI button (right after the search field at every width) and Ctrl+J, the
// modes the width gives the panel (beside the page from 1280 px, over it below), the window
// it expands to from 1024 px (over the page, with no address or history change), the header
// search's answer card against a fake chat route, the phone sheet from the top bar, and an
// /assistant address, which lands on the landing page and opens the assistant over it.

const launcher = (page: Page) => page.locator('[data-focus-key="assistant-launcher"]');
const panel = (page: Page) => page.locator("[data-assistant-panel]");
const rect = (page: Page, selector: string) =>
  page
    .locator(selector)
    .first()
    .evaluate((el) => {
      const r = el.getBoundingClientRect();
      return { left: r.left, top: r.top, width: r.width, right: r.right, bottom: r.bottom };
    });

async function onMemories(page: Page, width: number, height = 900) {
  await page.setViewportSize({ width, height });
  await installApi(page);
  await page.goto("/memories");
  // The page's own heading: the boot screen's visually hidden "Loom" heading is no arrival.
  await expect(page.locator("main h1").first()).toHaveText("Memories");
  await expect(launcher(page)).toBeVisible();
}

/** The chat route's answer as it streams: two chunks, one checked citation, the end. */
function streamed(text: string): string {
  const events: unknown[] = text
    .split(/(?<=\. )/)
    .map((p) => ({ id: "chatcmpl-1", choices: [{ index: 0, delta: { content: p } }] }));
  events.push({ object: "loom.text_done", id: "chatcmpl-1", choices: [] });
  events.push({
    object: "loom.citations",
    id: "chatcmpl-1",
    choices: [],
    support: "supported",
    citations: [
      { kind: "memory", text: "[memory:billing/invoices@v3]", verified: true, reason: "", ref: "billing/invoices" },
    ],
  });
  return `${events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("")}data: [DONE]\n\n`;
}

const ANSWER = "Invoices go out **weekly**, on Fridays [memory:billing/invoices@v3]. Reminders go two days before.";

/** Readiness says the assistant is ready, and the chat route answers ANSWER, after `delayMs`;
 *  the bodies sent. */
async function fakeAssistant(page: Page, delayMs = 0): Promise<string[]> {
  const sent: string[] = [];
  await page.route("**/v1/health/ready", (r) =>
    r.fulfill({ status: 200, json: { ready: true, checks: { assistant: { ok: true, required: false } } } }),
  );
  await page.route("**/v1/assistant/chat/completions", async (r) => {
    sent.push(r.request().postData() ?? "");
    if (delayMs) await new Promise((done) => setTimeout(done, delayMs));
    return r.fulfill({ status: 200, headers: { "Content-Type": "text/event-stream" }, body: streamed(ANSWER) });
  });
  return sent;
}

for (const width of [768, 1024, 1280, 1920]) {
  test(`at ${width} px Ask AI is the header's button right after the search, its ring whole`, async ({ page }) => {
    await onMemories(page, width);
    const header = await rect(page, "header");
    const button = await rect(page, '[data-focus-key="assistant-launcher"]');
    const search = await rect(page, "#header-search");
    expect(button.top).toBeGreaterThanOrEqual(header.top);
    expect(button.bottom).toBeLessThanOrEqual(header.bottom);
    expect(button.left).toBeGreaterThan(search.right);
    expect(button.left - search.right).toBeLessThanOrEqual(16);
    // Its word from 1280 px; narrower, the icon, named by its label.
    const word = launcher(page).getByText("Ask AI", { exact: true });
    if (width >= 1280) await expect(word).toBeVisible();
    else await expect(word).toBeHidden();
    await expect(launcher(page)).toHaveAccessibleName("Ask AI");
    // Nothing in the header pushes the page sideways.
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    // Next to the search in the tab order.
    await page.getByRole("combobox", { name: "Search Loom" }).focus();
    await page.keyboard.press("Escape");
    await page.keyboard.press("Tab");
    await expect(launcher(page)).toBeFocused();
    const ring = await launcher(page).evaluate((el) => {
      const r = el.getBoundingClientRect();
      const s = getComputedStyle(el);
      return { width: s.outlineWidth, inView: r.right + 4 <= window.innerWidth && r.top - 4 >= 0 };
    });
    expect(ring).toEqual({ width: "2px", inView: true });
    expect(await violations(page)).toEqual([]);
  });
}

test("open, the button looks pressed; Ctrl+J and the button close it", async ({ page }) => {
  await onMemories(page, 1440);
  const closed = await launcher(page).evaluate((el) => getComputedStyle(el).backgroundColor);
  await launcher(page).click();
  await expect(panel(page)).toHaveAttribute("data-open", "");
  await expect(launcher(page)).toHaveAttribute("aria-expanded", "true");
  const pressed = await launcher(page).evaluate((el) => getComputedStyle(el).backgroundColor);
  expect(pressed).not.toBe(closed);
  await page.keyboard.press("Control+j");
  await expect(panel(page)).toBeHidden();
  await expect(launcher(page)).toBeFocused();
  // The shortcut opens it from anywhere on the page, and the button closes it.
  await page.locator("main h1").first().click();
  await page.keyboard.press("Control+j");
  await expect(page.getByRole("textbox", { name: "Question" })).toBeFocused();
  await launcher(page).click();
  await expect(panel(page)).toBeHidden();
});

test("Ctrl+J is left to another dialog that holds the focus", async ({ page }) => {
  await onMemories(page, 1440);
  await page.getByRole("button", { name: "Keyboard shortcuts" }).click();
  const help = page.getByRole("dialog");
  await expect(help).toContainText("Open or close the assistant");
  await expect(help).toContainText("Ctrl");
  await page.keyboard.press("Control+j");
  await page.waitForTimeout(300);
  await expect(panel(page)).toHaveCount(0);
  await expect(help).toBeVisible();
});

test("at 1280 px with the sidebar open the panel docks beside the page, which keeps 640 px", async ({ page }) => {
  await onMemories(page, 1280);
  const main = await rect(page, "main");
  await launcher(page).click();
  await expect(panel(page)).toHaveAttribute("data-mode", "push");
  const docked = await rect(page, "[data-assistant-panel]");
  const after = await rect(page, "main");
  // The page's corner stays; the page gives up exactly the panel's width, at the window's right.
  expect({ left: after.left, top: after.top }).toEqual({ left: main.left, top: main.top });
  expect(main.width - after.width).toBeCloseTo(docked.width, 0);
  expect(after.width).toBeGreaterThanOrEqual(640);
  expect(docked.right).toBeCloseTo(1280, 0);
  await expect(launcher(page)).toHaveAttribute("aria-expanded", "true");
  // Expand and Collapse leave <main> where it is.
  await page.getByRole("button", { name: "Expand the assistant" }).click();
  expect(await rect(page, "main")).toEqual(after);
  await page.getByRole("button", { name: "Collapse the assistant" }).click();
  expect(await rect(page, "main")).toEqual(after);
  await page.getByRole("button", { name: "Close the assistant" }).click();
  expect(await rect(page, "main")).toEqual(main);
});

test("at 1279 px the panel is over the page, modal, and the header under it inert", async ({ page }) => {
  await onMemories(page, 1279);
  await launcher(page).click();
  await expect(panel(page)).toHaveAttribute("data-mode", "overlay");
  await expect(panel(page)).toHaveAttribute("aria-modal", "true");
  // Over the page: the page keeps the window's whole width.
  expect((await rect(page, "main")).right).toBeCloseTo(1279, 0);
  await expect(page.locator("header").locator("xpath=ancestor-or-self::*[@inert]")).toHaveCount(1);
});

test("Expand leaves the address and the history alone, and plays no motion", async ({ page }) => {
  await onMemories(page, 1400);
  await launcher(page).click();
  const url = page.url();
  const length = await page.evaluate(() => history.length);
  await page.getByRole("button", { name: "Expand the assistant" }).click();
  await expect(panel(page)).toHaveAttribute("data-mode", "expanded");
  await expect(panel(page)).toHaveAttribute("role", "dialog");
  expect(page.url()).toBe(url);
  expect(await page.evaluate(() => history.length)).toBe(length);
  expect(await panel(page).evaluate((el) => el.getAnimations().length)).toBe(0);
  const box = await rect(page, "[data-assistant-panel]");
  expect(box).toMatchObject({ left: 24, top: 0, right: 1400 });
  expect(await violations(page)).toEqual([]);
  // Ctrl+J closes the window, and the next open is docked.
  await page.keyboard.press("Control+j");
  await expect(panel(page)).toBeHidden();
  await launcher(page).click();
  await expect(panel(page)).toHaveAttribute("data-mode", "push");
});

test("Esc and a click on the scrim strip collapse the window; into the overlay with no motion", async ({ page }) => {
  await onMemories(page, 1100);
  await launcher(page).click();
  await expect(panel(page)).toHaveAttribute("data-mode", "overlay");
  await page.waitForFunction(() => document.querySelector("[data-assistant-panel]")?.getAnimations().length === 0);
  await page.getByRole("button", { name: "Expand the assistant" }).click();
  await page.keyboard.press("Escape");
  await expect(panel(page)).toHaveAttribute("data-mode", "overlay");
  expect(await panel(page).evaluate((el) => el.getAnimations().length)).toBe(0);
  await page.getByRole("button", { name: "Expand the assistant" }).click();
  await page.mouse.click(10, 450);
  await expect(panel(page)).toHaveAttribute("data-mode", "overlay");
  await expect(panel(page)).toHaveAttribute("data-open", "");
});

test("at 1920 px a wheel over the window's side area scrolls the transcript", async ({ page }) => {
  await onMemories(page, 1920);
  await launcher(page).click();
  await page.getByRole("button", { name: "Expand the assistant" }).click();
  // A card in the transcript, which then takes the window's height.
  await page.getByRole("button", { name: "What does this namespace hold, and who appears in it?" }).click();
  await expect(page.getByTestId("assistant-card")).toBeVisible();
  const box = page.getByTestId("assistant-transcript");
  // Taller than its box: the scroll has somewhere to go.
  await box.evaluate((el) => {
    const filler = document.createElement("div");
    filler.style.height = "3000px";
    el.firstElementChild?.append(filler);
  });
  const area = await rect(page, "[data-testid='assistant-transcript']");
  await page.mouse.move(area.left + 40, area.top + 100);
  await page.mouse.wheel(0, 400);
  await expect.poll(() => box.evaluate((el) => el.scrollTop)).toBeGreaterThan(0);
});

for (const [mode, width] of [
  ["push", 1400],
  ["overlay", 1100],
] as const) {
  test(`dragging the splitter keeps the panel's edge under the pointer (${mode})`, async ({ page }) => {
    await onMemories(page, width);
    await launcher(page).click();
    await expect(panel(page)).toHaveAttribute("data-mode", mode);
    // Past the overlay's slide-in, which moves the splitter while it plays.
    await expect(panel(page)).not.toHaveAttribute("data-entering", "");
    const splitter = page.getByRole("separator", { name: "Resize the assistant panel" });
    const start = await splitter.boundingBox();
    if (!start) throw new Error("no splitter");
    const y = start.y + start.height / 2;
    await page.mouse.move(start.x + start.width / 2, y);
    await page.mouse.down();
    await page.mouse.move(start.x - 40, y, { steps: 4 });
    await page.mouse.up();
    const after = await rect(page, "[data-assistant-panel]");
    expect(after.left).toBeCloseTo(start.x - 40, -1);
  });
}

test.describe("an /assistant address", () => {
  test("lands on Memories and opens the window over it, the page keeping its width", async ({ page }) => {
    await page.setViewportSize({ width: 1400, height: 900 });
    await installApi(page);
    await page.goto("/assistant");
    await expect(panel(page)).toHaveAttribute("data-mode", "expanded");
    await expect(page).toHaveURL(/\/memories/);
    await expect(page.getByRole("textbox", { name: "Question" })).toBeFocused();
    // No spacer: the page under the window keeps its full width.
    const main = await rect(page, "main");
    expect(main.right).toBeCloseTo(1400, 0);
  });

  test("an account that reaches nothing lands on Setup, and nothing opens", async ({ page }) => {
    await installApi(page, { viewer: true, claims: [], mine: [], savedNamespace: null, collections: [] });
    await page.goto("/assistant");
    await expect(page.getByRole("heading", { name: "Settings", exact: true })).toBeVisible();
    await page.waitForTimeout(500);
    await expect(panel(page)).toHaveCount(0);
  });

  test("a namespace the account cannot read opens nothing", async ({ page }) => {
    await installApi(page);
    await page.goto("/assistant?namespace=group-b-shared");
    await expect(page.getByRole("heading", { level: 1, name: "No access to group-b-shared" })).toBeVisible();
    await page.waitForTimeout(500);
    await expect(panel(page)).toHaveCount(0);
  });

  test("on a code namespace it opens over Repositories", async ({ page }) => {
    await installApi(page, {
      viewer: true,
      claims: CODE_TRACKER_ONLY_CLAIMS,
      mine: CODE_TRACKER_ONLY_MINE,
      savedNamespace: null,
    });
    await page.goto("/assistant");
    await expect(page).toHaveURL(/\/repositories/);
    await expect(panel(page)).toHaveAttribute("data-open", "");
  });

  test("an entry an earlier build pushed opens the assistant on Back after a reload", async ({ page }) => {
    await onMemories(page, 1280);
    await page.evaluate(() => history.pushState(null, "", "/assistant"));
    await page.evaluate(() => history.pushState(null, "", "/runs"));
    await page.reload();
    await expect(page.locator("main h1").first()).toHaveText("Runs");
    await page.goBack();
    await expect(panel(page)).toHaveAttribute("data-mode", "expanded");
    // The address names the page shown.
    await expect(page).toHaveURL(/\/memories/);
    await expect(page.locator("main h1").first()).toHaveText("Memories");
  });

  test("Back onto it with the panel docked expands it, the page keeping its width", async ({ page }) => {
    await onMemories(page, 1280);
    await page.evaluate(() => history.pushState(null, "", "/assistant"));
    await page.evaluate(() => history.pushState(null, "", "/runs"));
    await page.reload();
    await expect(page.locator("main h1").first()).toHaveText("Runs");
    await launcher(page).click();
    await expect(panel(page)).toHaveAttribute("data-mode", "push");
    const main = await rect(page, "main");
    await page.goBack();
    await expect(panel(page)).toHaveAttribute("data-mode", "expanded");
    expect((await rect(page, "main")).width).toBeCloseTo(main.width, 0);
  });
});

test("on a phone the top bar's Ask AI is a 44 px icon that opens the full-height sheet; Back closes it", async ({
  browser,
}) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
  const page = await context.newPage();
  await installApi(page);
  await page.goto("/memories");
  await expect(page.locator("main h1").first()).toHaveText("Memories");
  const button = await rect(page, '[data-focus-key="assistant-launcher"]');
  const header = await rect(page, "header");
  expect(button.width).toBeGreaterThanOrEqual(44);
  expect(button.bottom).toBeLessThanOrEqual(header.bottom);
  await expect(launcher(page)).toHaveAccessibleName("Ask AI");
  await expect(launcher(page).getByText("Ask AI", { exact: true })).toBeHidden();
  await expect(page.getByRole("navigation", { name: "Sections" })).not.toContainText("Assistant");
  await launcher(page).tap();
  await expect(panel(page)).toHaveAttribute("data-mode", "sheet");
  // Past the slide-in.
  await expect(panel(page)).not.toHaveAttribute("data-entering", "");
  expect(await rect(page, "[data-assistant-panel]")).toMatchObject({ top: 0, left: 0, width: 390, bottom: 844 });
  await page.goBack();
  await expect(panel(page)).toBeHidden();
  await expect(launcher(page)).toBeFocused();
  await context.close();
});

test("on a phone the card's citation number keeps its glyph's width, and each source row is 44 px", async ({
  browser,
}) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
  const page = await context.newPage();
  await installApi(page);
  await fakeAssistant(page);
  await page.goto("/memories");
  await expect(page.locator("main h1").first()).toHaveText("Memories");
  await page.getByRole("button", { name: "Search", exact: true }).tap();
  const search = page.getByRole("combobox", { name: "Search Loom" });
  await search.pressSequentially("how often are invoices sent?");
  await expect(page.getByRole("option", { name: "Ask AI: how often are invoices sent?" })).toBeVisible();
  await page.keyboard.press("Enter");
  const card = page.getByTestId("search-answer");
  await expect(card.getByTestId("assistant-support")).toBeVisible();
  const number = card.getByRole("link", { name: "Source 1" });
  const box = await number.boundingBox();
  // The glyph's width, and no tab stop: the 44 px rows below are the way to each source.
  expect(box?.width ?? 99).toBeLessThan(16);
  await expect(number).toHaveAttribute("tabindex", "-1");
  const rows = card.getByRole("list", { name: "Sources" }).getByRole("listitem");
  await expect(rows).toHaveCount(1);
  for (const row of await rows.all()) expect((await row.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(44);
  await context.close();
});

test.describe("the header search's answer card", () => {
  test("a question asked in place streams into the card, and Continue opens the panel on it", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await installApi(page);
    const sent = await fakeAssistant(page);
    await page.goto("/memories");
    await expect(page.locator("main h1").first()).toHaveText("Memories");
    const search = page.getByRole("combobox", { name: "Search Loom" });
    await search.click();
    await search.pressSequentially("how often are invoices sent?");
    const card = page.getByRole("option", { name: "Ask AI: how often are invoices sent?" });
    await expect(card).toBeVisible();
    await expect(search).toHaveAttribute("aria-activedescendant", (await card.getAttribute("id")) ?? "");
    expect(sent).toHaveLength(0);
    expect(await violations(page)).toEqual([]);
    await page.keyboard.press("Enter");
    const answer = page.getByTestId("search-answer");
    await expect(answer.getByTestId("search-answer-text")).toContainText("Invoices go out weekly, on Fridays");
    await expect(answer.getByTestId("assistant-support")).toContainText("Checked against what Loom holds");
    await expect(page.locator("#loom-assistant-status")).toHaveText("The answer is complete.");
    await expect(search).toBeFocused();
    expect(sent).toHaveLength(1);
    expect(JSON.parse(sent[0]).messages).toEqual([
      { role: "user", content: "Context: Memories page\n\nhow often are invoices sent?" },
    ]);
    expect(await violations(page)).toEqual([]);
    await page.keyboard.press("Enter");
    await expect(panel(page)).toHaveAttribute("data-open", "");
    await expect(page.getByRole("listbox", { name: "Search results" })).toHaveCount(0);
    const turns = page.getByRole("list", { name: "Conversation" }).locator(":scope > li");
    await expect(turns).toHaveCount(1);
    await expect(turns.last()).toContainText("how often are invoices sent?");
    await expect(turns.last()).toContainText("Reminders go two days before.");
    await expect(page.getByRole("textbox", { name: "Question" })).toBeFocused();
    await expect(launcher(page)).toHaveAttribute("data-launcher-state", "normal");
    expect(sent).toHaveLength(1);
  });

  test("keywords keep their results first; Tab shows the card, and closed it keeps answering", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await installApi(page);
    // The answer comes once the card is closed.
    const sent = await fakeAssistant(page, 1500);
    await page.goto("/memories");
    await expect(page.locator("main h1").first()).toHaveText("Memories");
    const search = page.getByRole("combobox", { name: "Search Loom" });
    await search.click();
    await search.pressSequentially("billing invoices");
    const options = page.getByRole("listbox", { name: "Search results" }).getByRole("option");
    await expect(options.last()).toHaveAccessibleName("Ask AI: billing invoices");
    await page.keyboard.press("Tab");
    await expect(search).toBeFocused();
    await expect(options.first()).toHaveAccessibleName("Ask AI: billing invoices");
    await page.keyboard.press("Enter");
    await expect.poll(() => sent.length).toBe(1);
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("search-answer")).toHaveCount(0);
    await expect(launcher(page)).toHaveAttribute("data-launcher-state", "working");
    // The answer ended with the card closed: the button says one is ready.
    await expect(launcher(page)).toHaveAttribute("data-launcher-state", "unread");
    expect(sent).toHaveLength(1);
  });

  test("with the chat off there is no card, and Tab leaves the field", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.addInitScript(() => {
      (window as Window & { __LOOM_ASSISTANT?: boolean }).__LOOM_ASSISTANT = false;
    });
    await installApi(page);
    await page.goto("/memories");
    await expect(page.locator("main h1").first()).toHaveText("Memories");
    const search = page.getByRole("combobox", { name: "Search Loom" });
    await search.click();
    await search.pressSequentially("how often are invoices sent?");
    await expect(page.locator("[data-header-search-panel]")).toBeVisible();
    await page.waitForTimeout(500);
    await expect(page.getByRole("option", { name: /^Ask AI/ })).toHaveCount(0);
    await page.keyboard.press("Tab");
    await expect(search).not.toBeFocused();
  });
});
