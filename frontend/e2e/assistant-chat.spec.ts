import { expect, test, type Page, type Request } from "@playwright/test";
import { violations } from "./axe";
import { installApi, NAMESPACE } from "./fixtures";

// The Ask AI panel's chat, opened from its launcher, against a fake chat route: the answer
// arrives as the server-sent chunks `POST /v1/assistant/chat/completions` streams (OpenAI's
// chunk shape, ending at `data: [DONE]`), a refusal before the stream as Loom's error
// envelope. The answer carries every Markdown form the chat draws, a table wider than a
// phone, HTML and an image that must stay text, and citations, each drawn as its number;
// the checked ones follow in its Sources, led by the same numbers, from the stream's last
// `loom.citations` event.

const SCREENSHOT = process.env.ASSISTANT_CHAT_SCREENSHOT;

const ANSWER = [
  "**Billing** runs weekly: invoices go out on *Fridays* [memory:billing/invoices@v3].",
  "",
  "- reminders go to the account owner two days before",
  "- a failed charge retries the next morning [doc:https://example.com/specs/billing.pdf § Retries]",
  "",
  "| Account | Owner | Cadence | Last invoice |",
  "| --- | --- | --- | --- |",
  "| enterprise-accounts-north-america | ada@example.com | weekly | invoice-2026-000123 |",
  "| self-serve | billing-team | monthly | invoice-2026-000456 |",
  "",
  "```sql",
  "SELECT account, count() FROM invoices GROUP BY account ORDER BY count() DESC",
  "```",
  "",
  'The job lives in `billing/schedule.py` [code:server/billing/schedule.py:42]. <img src="https://evil.example/x.png">',
].join("\n");

const CITATIONS = [
  { kind: "memory", text: "[memory:billing/invoices@v3]", verified: true, reason: "", ref: "billing/invoices" },
  {
    kind: "doc",
    text: "[doc:https://example.com/specs/billing.pdf § Retries]",
    verified: true,
    reason: "",
    ref: "https://example.com/specs/billing.pdf",
  },
  {
    kind: "sql",
    text: "```sql\nSELECT account, count() FROM invoices GROUP BY account ORDER BY count() DESC\n```",
    verified: true,
    reason: "",
    ref: "",
  },
  {
    kind: "code",
    text: "[code:server/billing/schedule.py:42]",
    verified: false,
    reason: "code citations are not checked",
    ref: "server/billing/schedule.py",
  },
];

/** The answer as the route streams it: a few chunks, usage, its checked citations, then the end. */
function streamed(text: string): string {
  const pieces = text.match(/[\s\S]{1,40}/g) ?? [];
  const events = pieces.map((p) => ({ id: "chatcmpl-1", choices: [{ index: 0, delta: { content: p } }] }));
  events.push({ id: "chatcmpl-1", choices: [], usage: { prompt_tokens: 900, completion_tokens: 120 } } as never);
  events.push({ object: "loom.citations", id: "chatcmpl-1", choices: [], citations: CITATIONS } as never);
  return `${events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("")}data: [DONE]\n\n`;
}

/** A page with the panel open on an empty chat: readiness reports the assistant ready, and
 *  the Ask AI button opens it. */
async function openChat(page: Page) {
  await installApi(page);
  await page.route("**/v1/health/ready", (r) =>
    r.fulfill({ status: 200, json: { ready: true, checks: { assistant: { ok: true, required: false } } } }),
  );
  await page.goto("/memories");
  await expect(page.locator("main h1").first()).toBeVisible();
  await expect(launcher(page)).toBeVisible();
  await launcher(page).click();
  await expect(page.getByRole("textbox", { name: "Question" })).toBeFocused();
}

const launcher = (page: Page) => page.locator('[data-focus-key="assistant-launcher"]');
const panel = (page: Page) => page.locator("[data-assistant-panel]");

async function ask(page: Page, question: string) {
  await page.getByRole("textbox", { name: "Question" }).fill(question);
  await page.getByRole("button", { name: "Ask", exact: true }).click();
}

const conversation = (page: Page) => page.getByRole("list", { name: "Conversation" });

// Each question carries the page it is asked from, as a leading line; the transcript shows a
// chip for it instead.
const ON_MEMORIES = "Context: Memories page\n\n";

for (const width of [1400, 375]) {
  test.describe(`${width} px`, () => {
    test.use({ viewport: { width, height: 900 } });

    test("a streamed answer renders as text with Markdown, fits the page and has no WCAG A/AA violations", async ({
      page,
    }) => {
      const sent: Request[] = [];
      await openChat(page);
      await page.route("**/v1/assistant/chat/completions", (r) => {
        sent.push(r.request());
        return r.fulfill({ status: 200, contentType: "text/event-stream", body: streamed(ANSWER) });
      });
      await ask(page, "How does billing run?");
      const chat = conversation(page);
      await expect(chat.getByRole("table")).toContainText("enterprise-accounts-north-america");
      await expect(page.getByRole("button", { name: "Stop" })).toHaveCount(0);

      const request = sent[0];
      expect(request.headers()["x-loom-namespace"]).toBe(NAMESPACE);
      expect(request.headers().authorization).toBe("Bearer e2e-token");
      expect(request.postDataJSON()).toEqual({
        stream: true,
        messages: [{ role: "user", content: `${ON_MEMORIES}How does billing run?` }],
      });

      await expect(chat.locator("strong", { hasText: "Billing" })).toBeVisible();
      await expect(chat.locator("pre")).toContainText("SELECT account");
      // Each citation is its number, as the search's card draws it, linking to its row.
      await expect(chat).not.toContainText("[memory:billing/invoices@v3]");
      await expect(chat).toContainText("on Fridays 1.");
      await expect(chat).toContainText("schedule.py 3.");
      await expect(chat).toContainText('<img src="https://evil.example/x.png">');
      expect(await chat.locator("img").count()).toBe(0);
      expect(await chat.locator("a:not([href^='#'])").count()).toBe(0);
      const sources = chat.getByRole("list", { name: "Sources" });
      await expect(sources.getByRole("listitem")).toHaveCount(4);
      await expect(sources.getByRole("listitem").first()).toHaveText("1. billing/invoices");
      const one = chat.getByRole("link", { name: "Source 1" });
      expect(await one.getAttribute("href")).toBe(`#${await sources.getByRole("listitem").first().getAttribute("id")}`);
      await expect(sources.getByRole("button", { name: "billing/invoices" })).toBeVisible();
      await expect(sources).toContainText("SQL query: SELECT account, count() FROM invoices");
      await expect(sources).toContainText("code citations are not checked");

      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow).toBeLessThanOrEqual(0);
      expect(await violations(page)).toEqual([]);
      if (SCREENSHOT && width === 1400) await panel(page).screenshot({ path: SCREENSHOT });
    });
  });
}

test("each turn sends the whole conversation", async ({ page }) => {
  const bodies: unknown[] = [];
  await openChat(page);
  await page.route("**/v1/assistant/chat/completions", (r) => {
    bodies.push(r.request().postDataJSON());
    return r.fulfill({ status: 200, contentType: "text/event-stream", body: streamed(`Answer ${bodies.length}.`) });
  });
  await ask(page, "Who owns billing?");
  await expect(conversation(page)).toContainText("Answer 1.");
  await page.getByRole("textbox", { name: "Question" }).fill("Since when?");
  await page.getByRole("textbox", { name: "Question" }).press("Enter");
  await expect(conversation(page)).toContainText("Answer 2.");
  expect(bodies[1]).toEqual({
    stream: true,
    messages: [
      { role: "user", content: `${ON_MEMORIES}Who owns billing?` },
      { role: "assistant", content: "Answer 1." },
      { role: "user", content: `${ON_MEMORIES}Since when?` },
    ],
  });
});

test("a refusal before the stream and an error event inside it are shown in the chat", async ({ page }) => {
  await openChat(page);
  let n = 0;
  await page.route("**/v1/assistant/chat/completions", (r) => {
    n += 1;
    if (n === 1)
      return r.fulfill({
        status: 503,
        json: {
          detail: { code: "OVERLOADED", message: "the assistant is already answering 4 questions", retry_after_s: 5 },
        },
      });
    // The route's one error inside a stream ends it: no [DONE] follows.
    const error = { code: "ASSISTANT_UPSTREAM_FAILED", message: "LibreChat ended the answer with an error" };
    return r.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: `data: ${JSON.stringify({ id: "c", choices: [{ delta: { content: "Partly" } }] })}\n\ndata: ${JSON.stringify({ error })}\n\n`,
    });
  });
  await ask(page, "Who owns billing?");
  await expect(page.getByRole("alert").filter({ hasText: "Loom is busy right now" })).toContainText(
    "Try again in 5 s.",
  );
  // The refused question waits in the field.
  await expect(page.getByRole("textbox", { name: "Question" })).toHaveValue("Who owns billing?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  // One sentence; the server did not mark it retryable, so the alert offers no Retry.
  const failed = page.getByRole("alert").filter({ hasText: "The model stopped before the answer finished." });
  await expect(failed).toContainText("Your question is kept.");
  await expect(failed.getByRole("button", { name: "Retry" })).toHaveCount(0);
  await expect(conversation(page)).toContainText("Partly");
});

test("Stop aborts the request", async ({ page }) => {
  // The page's own record of the chat request's signal: a request the route holds open
  // sends no failure event when the page aborts it.
  await page.addInitScript(() => {
    const send = window.fetch.bind(window);
    window.fetch = (input, init) => {
      if (String(input).endsWith("/v1/assistant/chat/completions"))
        init?.signal?.addEventListener("abort", () => document.documentElement.setAttribute("data-chat-aborted", ""));
      return send(input, init);
    };
  });
  await openChat(page);
  // The route never answers: the answer is still on its way when Stop is pressed.
  await page.route("**/v1/assistant/chat/completions", () => undefined);
  await ask(page, "Who owns billing?");
  await expect(page.getByText("Answering…")).toBeVisible();
  await page.getByRole("button", { name: "Stop" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-chat-aborted", "");
  await expect(conversation(page)).toContainText("Stopped.");
  await expect(page.getByRole("textbox", { name: "Question" })).toBeFocused();
});

test("an unavailable assistant is a plain state, not a crash", async ({ page }) => {
  await openChat(page);
  await page.route("**/v1/assistant/chat/completions", (r) =>
    r.fulfill({
      status: 503,
      json: { detail: { code: "ASSISTANT_UNAVAILABLE", message: "the assistant is not set up on this replica yet" } },
    }),
  );
  await ask(page, "Who owns billing?");
  // A refusal that names no reason reads as not set up: one empty state, with the link to
  // the docs on setting up the assistant service, and no question field.
  const why = panel(page).getByTestId("assistant-unavailable");
  await expect(why).toHaveAttribute("data-reason", "not_configured");
  await expect(why.getByRole("heading", { level: 3 })).toHaveText("Chat isn't set up");
  await expect(why.getByRole("button", { name: "How to set up the assistant service" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Question" })).toHaveCount(0);
  await expect(panel(page).getByRole("checkbox")).toHaveCount(0);
  // Chat not set up is no broken mark on the launcher: its starters still read.
  await expect(launcher(page)).toHaveAccessibleName("Ask AI");
  await expect(launcher(page)).toHaveAttribute("data-launcher-state", "normal");
  // The rest of the page still works.
  await expect(page.locator("main h1").first()).toBeVisible();
  expect(await violations(page)).toEqual([]);
});

for (const width of [360, 400]) {
  test.describe(`phone ${width}, coarse pointer`, () => {
    test.use({ viewport: { width, height: 800 }, hasTouch: true, isMobile: true });

    test("the top bar is one row with Ask AI and no New, the bottom nav three tabs, each a 44 px target", async ({
      page,
    }) => {
      await installApi(page);
      await page.goto("/memories");
      await expect(page.locator("main h1").first()).toBeVisible();
      await expect(page.locator("header").locator('[data-focus-key="assistant-launcher"]')).toBeVisible();
      const fit = await page.evaluate(() => {
        const big = (el: Element) => {
          const r = el.getBoundingClientRect();
          return r.width >= 44 && r.height >= 44;
        };
        const header = document.querySelector("header") as HTMLElement;
        // Every control the top bar shows (the search field's row stays hidden until
        // asked).
        const controls = [...header.querySelectorAll<HTMLElement>("button")].filter((b) => b.offsetParent !== null);
        const tops = new Set(controls.map((b) => Math.round(b.getBoundingClientRect().top)));
        // The phone's bottom nav (the sidebar's nav is hidden at this width).
        const nav = [...document.querySelectorAll<HTMLElement>('nav[aria-label="Sections"]')].find(
          (n) => n.offsetParent !== null,
        ) as HTMLElement;
        return {
          overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
          rows: tops.size,
          ask: controls.some((b) => b.dataset.focusKey === "assistant-launcher"),
          newShown: controls.some((b) => b.getAttribute("aria-label") === "New"),
          small: controls.filter((b) => !big(b)).map((b) => b.getAttribute("aria-label")),
          items: Array.from(nav.children).map((c) => ({ text: c.textContent?.trim(), big: big(c) })),
        };
      });
      expect(fit).toEqual({
        overflow: 0,
        rows: 1,
        ask: true,
        newShown: false,
        small: [],
        items: ["Memories", "Library", "More"].map((text) => ({ text, big: true })),
      });
    });

    test("the More sheet offers New, which opens Add memory", async ({ page }) => {
      await installApi(page);
      await page.goto("/memories");
      await expect(page.locator("main h1").first()).toBeVisible();
      await page.getByRole("button", { name: "More" }).tap();
      const sheet = page.getByRole("dialog", { name: "More" });
      const memory = sheet.getByRole("button", { name: "New memory" });
      await expect(memory).toBeVisible();
      // Past the sheet's entrance, which scales it.
      await expect.poll(async () => (await memory.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(44);
      await memory.tap();
      await expect(sheet).toBeHidden();
      await expect(page.getByRole("dialog")).toBeVisible();
    });
  });
}

test("at 390 px the top bar's Ask AI opens the sheet, and Back returns the focus to it", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 800 });
  await installApi(page);
  await page.goto("/memories");
  await expect(page.locator("main h1").first()).toBeVisible();
  await launcher(page).click();
  await expect(panel(page)).toHaveAttribute("data-mode", "sheet");
  await expect(page.getByRole("textbox", { name: "Question" })).toBeFocused();
  await page.goBack();
  await expect(panel(page)).toBeHidden();
  await expect(launcher(page)).toBeFocused();
  await expect(launcher(page).locator("xpath=ancestor::header")).toHaveCount(1);
});

// "Ask about this" in the memory drawer: the drawer closes, the panel opens in the mode the
// width gives (beside the page at 1440 px, over it at 1024 px, the phone sheet at 390 px)
// with its field focused and the memory in its chip, and the question goes with the context
// line.
const MODE_AT: Record<number, string> = { 1440: "push", 1024: "overlay", 390: "sheet" };
for (const width of [1440, 1024, 390]) {
  test(`at ${width} px, Ask about this in the drawer asks with the memory in the chip`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await installApi(page, { memoryRows: 3 });
    await page.route("**/v1/health/ready", (r) =>
      r.fulfill({ status: 200, json: { ready: true, checks: { assistant: { ok: true, required: false } } } }),
    );
    const sent: unknown[] = [];
    await page.route("**/v1/assistant/chat/completions", (r) => {
      sent.push(r.request().postDataJSON());
      return r.fulfill({ status: 200, contentType: "text/event-stream", body: streamed("Ada wrote it.") });
    });
    const key = "alerts/2026-09-19/0001";
    await page.goto(`/memories?memory=${encodeURIComponent(key)}`, { waitUntil: "domcontentloaded" });
    const drawer = page.getByRole("dialog", { name: key });
    const ask = drawer.locator("footer").getByRole("button", { name: "Ask AI about this memory" });
    await expect(ask).toHaveText("Ask about this");
    await ask.click();
    await expect(drawer).toBeHidden();
    const field = page.getByRole("textbox", { name: "Question" });
    await expect(field).toBeFocused();
    await expect(panel(page)).toHaveAttribute("data-mode", MODE_AT[width]);
    // Modal over the page and on the phone, beside it otherwise.
    if (width === 1440) await expect(panel(page)).not.toHaveAttribute("aria-modal");
    else await expect(panel(page)).toHaveAttribute("aria-modal", "true");
    await expect(panel(page).getByTestId("assistant-page-chip")).toHaveText(`Includes: Memories · key ${key}`);
    expect(await violations(page)).toEqual([]);
    await field.fill("Who wrote it?");
    await field.press("Enter");
    await expect(conversation(page)).toContainText("Ada wrote it.");
    const messages = (sent[0] as { messages: { role: string; content: string }[] }).messages;
    expect(messages).toHaveLength(1);
    // The memory's writer agent follows its key when the drawer knows it.
    expect(messages[0].content).toMatch(
      new RegExp(`^Context: Memories page, memory "${key}"( \\(agent "[^"]+"\\))?\\n\\nWho wrote it\\?$`),
    );
    await expect(conversation(page)).toContainText(`Includes: Memories · key ${key}`);
    await expect(conversation(page)).not.toContainText("Context:");
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(0);
  });
}

// "Ask about this" in the Library's document preview on a phone: the preview closes, the
// sheet opens on its own history entry with the field focused and the document in its chip,
// and Back closes the sheet onto the Library.
test("at 390 px, Ask about this in the document preview opens the sheet, which Back closes", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await installApi(page, { knowledgeRows: 4 });
  await page.route("**/v1/health/ready", (r) =>
    r.fulfill({ status: 200, json: { ready: true, checks: { assistant: { ok: true, required: false } } } }),
  );
  const uri = "docs/runbooks/handbook-000.md";
  await page.goto(`/library?doc=${encodeURIComponent(uri)}`, { waitUntil: "domcontentloaded" });
  const preview = page.getByRole("dialog", { name: "Runbook 0: lane 0 recovery" });
  const ask = preview.getByRole("button", { name: "Ask AI about this document" });
  await expect(ask).toHaveText("Ask about this");
  await ask.click();
  await expect(preview).toBeHidden();
  const field = page.getByRole("textbox", { name: "Question" });
  await expect(field).toBeFocused();
  await expect(panel(page)).toHaveAttribute("data-mode", "sheet");
  await expect(panel(page).getByTestId("assistant-page-chip")).toHaveText(
    "Includes: Library · document Runbook 0: lane 0 recovery",
  );
  expect(await page.evaluate(() => (history.state as { loomSheet?: boolean } | null)?.loomSheet)).toBe(true);
  expect(await violations(page)).toEqual([]);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  await page.goBack();
  await expect(panel(page)).toBeHidden();
  await expect(page.locator("main h1").first()).toHaveText("Library");
});
