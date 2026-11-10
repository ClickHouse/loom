import { expect, test, type Page, type Request } from "@playwright/test";
import { violations } from "./axe";
import { installApi, NAMESPACE } from "./fixtures";

// The assistant panel's chat, opened from the header's launcher, against a fake chat route: the answer arrives as the
// server-sent chunks `POST /v1/assistant/chat/completions` streams (OpenAI's chunk shape,
// ending at `data: [DONE]`), a refusal before the stream as Loom's error envelope. The
// answer carries every Markdown form the chat draws, a table wider than a phone, HTML and
// an image that must stay text, and citations, which stay plain text in the answer; the
// checked ones follow in its Sources, from the stream's last `loom.citations` event.

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
 *  the launcher (or `Ctrl+/`) opens it. */
async function openChat(page: Page, by: "launcher" | "shortcut" = "launcher") {
  await installApi(page);
  await page.route("**/v1/health/ready", (r) =>
    r.fulfill({ status: 200, json: { ready: true, checks: { assistant: { ok: true, required: false } } } }),
  );
  await page.goto("/memories");
  await expect(page.locator("main h1").first()).toBeVisible();
  await expect(launcher(page)).toBeVisible();
  if (by === "launcher") await launcher(page).click();
  // The shortcut's listener is wired once the shell has drawn: a press that lands before
  // it is pressed again, only while the panel is still closed.
  else
    await expect(async () => {
      if ((await launcher(page).getAttribute("aria-expanded")) !== "true") await page.keyboard.press("Control+/");
      await expect(launcher(page)).toHaveAttribute("aria-expanded", "true", { timeout: 5_000 });
    }).toPass({ timeout: 30_000 });
  await expect(page.getByRole("textbox", { name: "Question" })).toBeFocused();
}

const launcher = (page: Page) => page.locator('[data-focus-key="assistant-launcher"]');
const panel = (page: Page) => page.locator("[data-assistant-panel]");

async function ask(page: Page, question: string) {
  await page.getByRole("textbox", { name: "Question" }).fill(question);
  await page.getByRole("button", { name: "Ask", exact: true }).click();
}

const conversation = (page: Page) => page.getByRole("list", { name: "Conversation" });

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
        messages: [{ role: "user", content: "How does billing run?" }],
      });

      await expect(chat.locator("strong", { hasText: "Billing" })).toBeVisible();
      await expect(chat.locator("pre")).toContainText("SELECT account");
      await expect(chat).toContainText("[memory:billing/invoices@v3]");
      await expect(chat).toContainText("[code:server/billing/schedule.py:42]");
      await expect(chat).toContainText('<img src="https://evil.example/x.png">');
      expect(await chat.locator("a, img").count()).toBe(0);
      const sources = chat.getByRole("list", { name: "Sources" });
      await expect(sources.getByRole("listitem")).toHaveCount(4);
      await expect(sources.getByRole("button", { name: "memory:billing/invoices@v3" })).toBeVisible();
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
  await openChat(page, "shortcut");
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
      { role: "user", content: "Who owns billing?" },
      { role: "assistant", content: "Answer 1." },
      { role: "user", content: "Since when?" },
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
  await expect(page.getByRole("alert").filter({ hasText: "The assistant could not finish this answer" })).toBeVisible();
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
  // A refusal that names no reason reads as not set up; a platform admin is sent to Health.
  const why = panel(page).getByTestId("assistant-unavailable");
  await expect(why).toHaveAttribute("data-reason", "not_configured");
  await expect(why).toContainText("Set up the assistant: see Health");
  await expect(page.getByRole("button", { name: "Ask", exact: true })).toHaveAttribute("aria-disabled", "true");
  await expect(launcher(page)).toHaveAccessibleName("Assistant, unavailable");
  // The rest of the page still works.
  await expect(page.locator("main h1").first()).toBeVisible();
  expect(await violations(page)).toEqual([]);
});

for (const width of [360, 400]) {
  test.describe(`phone ${width}, coarse pointer`, () => {
    test.use({ viewport: { width, height: 800 }, hasTouch: true, isMobile: true });

    test("the header's buttons fit one row with the launcher, a 44 px target", async ({ page }) => {
      await installApi(page);
      await page.goto("/memories");
      await expect(page.locator("main h1").first()).toBeVisible();
      await expect(launcher(page)).toBeVisible();
      const fit = await page.evaluate(() => {
        // The header's right cluster: the row of buttons the launcher sits in.
        const cluster = document
          .querySelector('[data-focus-key="assistant-launcher"]')
          ?.closest("header > div") as HTMLElement;
        const shown = Array.from(cluster.children).filter((c) => (c as HTMLElement).offsetWidth > 0);
        return {
          overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
          rows: new Set(shown.map((c) => Math.round(c.getBoundingClientRect().top))).size,
        };
      });
      expect(fit).toEqual({ overflow: 0, rows: 1 });
      const box = await launcher(page).boundingBox();
      expect(Math.round(box?.width ?? 0)).toBeGreaterThanOrEqual(44);
      expect(Math.round(box?.height ?? 0)).toBeGreaterThanOrEqual(44);
    });
  });
}
