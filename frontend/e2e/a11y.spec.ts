import { expect, test, type Page } from "@playwright/test";
import * as fs from "node:fs";
import { createRequire } from "node:module";
import { installApi, ROUTES } from "./fixtures";

// axe-core on the rendered dashboard: every route at desktop and phone width, and the shell's
// dialog and menu open. WCAG 2.0–2.2 A and AA rules; any violation fails, with its rule,
// impact and the first nodes it hit. Contrast is only measurable here, not in jsdom.
const AXE = fs.readFileSync(createRequire(import.meta.url).resolve("axe-core/axe.min.js"), "utf8");
const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

interface Violation {
  id: string;
  impact: string | null;
  nodes: string[];
}

async function violations(page: Page): Promise<Violation[]> {
  await page.addScriptTag({ content: AXE });
  return page.evaluate(async (tags) => {
    // The injected axe global; the specs carry no axe types.
    const axe = (
      window as unknown as {
        axe: {
          run: (
            ctx: Document,
            opts: object,
          ) => Promise<{
            violations: Array<{ id: string; impact: string | null; nodes: Array<{ target: string[] }> }>;
          }>;
        };
      }
    ).axe;
    const wcag = await axe.run(document, { runOnly: { type: "tag", values: tags } });
    // A popover or sheet is a dialog, and headings follow one outline; axe files both under
    // best practice, not WCAG.
    const names = await axe.run(document, { runOnly: { type: "rule", values: ["aria-dialog-name", "heading-order"] } });
    return [...wcag.violations, ...names.violations].map((v) => ({
      id: v.id,
      impact: v.impact,
      nodes: v.nodes.slice(0, 5).map((n) => n.target.join(" ")),
    }));
  }, TAGS);
}

async function open(page: Page, path: string) {
  await installApi(page, { memoryRows: 8, knowledgeRows: 4, runRows: 6 });
  await page.goto(path);
  await expect(page.locator("main h1").first()).toBeVisible();
  await page.waitForLoadState("networkidle"); // late rows, counts and badges
  await page.waitForTimeout(300); // their entry animation
}

for (const width of [1400, 375]) {
  test.describe(`${width} px`, () => {
    test.use({ viewport: { width, height: 900 } });
    for (const path of ROUTES) {
      test(`${path} has no WCAG A/AA violations`, async ({ page }) => {
        await open(page, path);
        expect(await violations(page)).toEqual([]);
      });
    }
  });
}

test("the Help dialog has no WCAG A/AA violations", async ({ page }) => {
  await open(page, "/memories");
  await page.keyboard.press("?");
  await expect(page.getByRole("dialog")).toBeVisible();
  expect(await violations(page)).toEqual([]);
});

test("the New menu has no WCAG A/AA violations", async ({ page }) => {
  await open(page, "/memories");
  await page.getByRole("button", { name: "New" }).click();
  await expect(page.getByRole("menu")).toBeVisible();
  expect(await violations(page)).toEqual([]);
});

// The shell's overlays, open: axe sees what a user sees once a dialog, popover or menu is up.
test.describe("open overlays", () => {
  test("the header search popover", async ({ page }) => {
    await open(page, "/memories");
    await page.getByRole("combobox", { name: "Search Loom" }).fill("alerts");
    await expect(page.getByRole("listbox")).toBeVisible();
    expect(await violations(page)).toEqual([]);
  });

  test("the header search panel stays open on Tab focus and leaves no focus guard behind", async ({ page }) => {
    await open(page, "/memories");
    const search = page.getByRole("combobox", { name: "Search Loom" });
    const guards = page.locator("[data-radix-focus-guard]");
    await page.getByRole("button", { name: /^Namespace: / }).focus();
    await page.keyboard.press("Tab");
    await expect(search).toBeFocused();
    // The empty box's recent memories are options of the listbox; ↓ highlights the first.
    const listbox = page.getByRole("listbox", { name: "Search results" });
    await expect(listbox).toBeVisible();
    await page.waitForTimeout(300);
    await expect(listbox).toBeVisible();
    expect(await violations(page)).toEqual([]);
    await page.keyboard.press("ArrowDown");
    await expect(search).toHaveAttribute(
      "aria-activedescendant",
      (await listbox.getByRole("option").first().getAttribute("id")) ?? "",
    );
    // Focus stays in the field, so the active option draws the focus ring itself.
    const ring = await listbox
      .getByRole("option")
      .first()
      .evaluate((el) => {
        const probe = document.createElement("span");
        probe.style.color = "var(--color-focus)";
        document.body.append(probe);
        const focus = getComputedStyle(probe).color;
        probe.remove();
        return { shadow: getComputedStyle(el).boxShadow, focus };
      });
    expect(ring.shadow).toContain(ring.focus);

    await page.keyboard.press("Tab");
    await expect(listbox).toBeHidden();
    await expect(guards).toHaveCount(0);

    await search.click();
    await expect(listbox).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(listbox).toBeHidden();
    await expect(guards).toHaveCount(0);
    await expect(search).toBeFocused();
  });

  test("the namespace picker", async ({ page }) => {
    await open(page, "/memories");
    await page.getByRole("button", { name: /^Namespace: / }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
    expect(await violations(page)).toEqual([]);
  });

  test("the memory inspector", async ({ page }) => {
    await open(page, "/memories");
    await page.locator("[data-memory-key] button").first().click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.waitForLoadState("networkidle");
    expect(await violations(page)).toEqual([]);
  });

  test("j moves focus over the memory cards, one ring on the card, and the inspector returns it", async ({ page }) => {
    await open(page, "/memories");
    const opens = page.locator("[data-memory-open]");
    await page.keyboard.press("j");
    await page.keyboard.press("j");
    await expect(opens.nth(1)).toBeFocused();
    // The card draws the ring; its stretched button draws none of its own.
    const rings = await opens.nth(1).evaluate((el) => ({
      button: getComputedStyle(el).outlineStyle,
      card: getComputedStyle(el.closest("[data-card]") as Element).outlineStyle,
    }));
    expect(rings).toEqual({ button: "none", card: "solid" });
    await page.keyboard.press("Enter");
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toBeHidden();
    await expect(opens.nth(1)).toBeFocused();
    // Space selects the focused card instead of opening it.
    await page.keyboard.press(" ");
    await expect(page.locator("[data-card]").nth(1).getByRole("checkbox")).toBeChecked();
    await expect(page.getByRole("dialog")).toBeHidden();
  });

  test("the Add memory dialog, and focus back on New after Escape", async ({ page }) => {
    await open(page, "/memories");
    const trigger = page.getByRole("button", { name: "New" });
    await trigger.click();
    await page.getByRole("menuitem", { name: /Memory/ }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
    expect(await violations(page)).toEqual([]);
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toBeHidden();
    await expect(trigger).toBeFocused();
  });

  test("the two header bells", async ({ page }) => {
    await open(page, "/memories");
    for (const name of [/^Pending approvals/, /^Tasks waiting for a person/]) {
      await page.getByRole("button", { name }).click();
      await expect(page.getByRole("dialog")).toBeVisible();
      expect(await violations(page)).toEqual([]);
      await page.keyboard.press("Escape");
      await expect(page.getByRole("dialog")).toBeHidden();
    }
  });

  test("the profile's read-by popover", async ({ page }) => {
    await installApi(page);
    await page.route("**/v1/profile.activity", (route) =>
      route.fulfill({
        json: {
          entries: [
            {
              agent: "claude-code",
              last_activated_at: new Date(Date.now() - 3_600_000).toISOString(),
              event_count: 4,
              operations: ["profile.get"],
            },
          ],
        },
      }),
    );
    await page.goto("/profile");
    await expect(page.locator("main h1").first()).toBeVisible();
    await page.getByRole("button", { name: /^(Read|Checked) by/ }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
    expect(await violations(page)).toEqual([]);
  });

  test("the token dialog returns focus to Generate token", async ({ page }) => {
    await open(page, "/settings");
    // A slow mint: Click UI disables the loading button, which drops its focus first.
    await page.route("**/v1/me/setup/mcp_config", async (route) => {
      await new Promise((r) => setTimeout(r, 300));
      await route.fulfill({
        json: {
          client: "claude-code",
          config_path: { darwin: "~/.claude.json", linux: "~/.claude.json", win32: "%USERPROFILE%\\.claude.json" },
          config_key: "mcpServers",
          config: {},
          token: "eyJhbGciOiJFUzI1NiJ9.e30.sig",
          subject: "dev",
          org: "dev",
          namespace: "dev",
          namespace_scope: "single",
          read_scope: "inherited_grants",
          restricted_namespaces: [],
          mcp_url: "http://127.0.0.1/mcp",
          persisted: true,
          sa_subject: "sa:claude-code-test",
        },
      });
    });
    const generate = page.getByRole("button", { name: "Generate token" });
    await generate.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("dialog", { name: "Token generated" })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toBeHidden();
    await expect(generate).toBeFocused();
  });

  test("a button that loads in place keeps focus through its loading state", async ({ page }) => {
    await open(page, "/memories");
    // Click UI disables a loading button, which drops its focus; it takes it back after.
    await page.route("**/v1/namespace.audit", async (route) => {
      await new Promise((r) => setTimeout(r, 300));
      await route.fulfill({ status: 503, json: { detail: "audit unavailable", code: "UNAVAILABLE" } });
    });
    const run = page.getByRole("button", { name: "Run audit" });
    await run.focus();
    await page.keyboard.press("Enter");
    await expect(run).toBeEnabled();
    await expect(run).toBeFocused();
  });

  test("the thread dialog's last Load more leaves Tab moving inside the dialog", async ({ page }) => {
    const now = new Date().toISOString();
    const turn = (i: number) => ({ msg_id: `m${i}`, agent: "cursor", role: "user", content: `turn ${i}`, ts: now });
    await installApi(page);
    await page.route("**/v1/conversation.threads", (route) =>
      route.fulfill({
        json: {
          threads: [
            {
              thread_id: "t-long",
              agent: "cursor",
              msg_count: 3,
              first_seen: now,
              last_seen: now,
              last_role: "user",
              last_content_excerpt: "x",
              title: "Long thread",
            },
          ],
        },
      }),
    );
    await page.route("**/v1/conversation.get", (route) =>
      route.fulfill({
        json: {
          found: true,
          thread_id: "t-long",
          title: "Long thread",
          agent: "cursor",
          msg_count: 3,
          created_at: null,
          updated_at: null,
          messages: [turn(0), turn(1)],
          next_cursor: "c-1",
        },
      }),
    );
    await page.route("**/v1/conversation.list", (route) =>
      route.fulfill({ json: { rows: [turn(2)], next_cursor: null } }),
    );
    await page.goto("/conversations");
    await page.getByText("Long thread").first().click();
    const dialog = page.getByRole("dialog");
    await dialog.getByRole("button", { name: /Load more/ }).focus();
    await page.keyboard.press("Enter");
    const transcript = dialog.getByRole("region", { name: "Transcript" });
    await expect(transcript).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(transcript).not.toBeFocused();
    expect(await dialog.evaluate((d) => d.contains(document.activeElement))).toBe(true);
  });

  test("a loading button inside a dialog keeps focus inside the dialog", async ({ page }) => {
    const now = new Date().toISOString();
    const turn = (i: number) => ({ msg_id: `m${i}`, agent: "cursor", role: "user", content: `turn ${i}`, ts: now });
    await installApi(page);
    await page.route("**/v1/conversation.threads", (route) =>
      route.fulfill({
        json: {
          threads: [
            {
              thread_id: "t-long",
              agent: "cursor",
              msg_count: 3,
              first_seen: now,
              last_seen: now,
              last_role: "user",
              last_content_excerpt: "x",
              title: "Long thread",
            },
          ],
        },
      }),
    );
    await page.route("**/v1/conversation.get", (route) =>
      route.fulfill({
        json: {
          found: true,
          thread_id: "t-long",
          title: "Long thread",
          agent: "cursor",
          msg_count: 3,
          created_at: null,
          updated_at: null,
          messages: [turn(0), turn(1)],
          next_cursor: "c-1",
        },
      }),
    );
    await page.route("**/v1/conversation.list", async (route) => {
      await new Promise((r) => setTimeout(r, 3000));
      await route.fulfill({ json: { rows: [turn(2)], next_cursor: null } });
    });
    await page.goto("/conversations");
    await page.getByText("Long thread").first().click();
    const dialog = page.getByRole("dialog");
    await dialog.getByRole("button", { name: /Load more/ }).focus();
    await page.keyboard.press("Enter");
    // Click UI disables a loading button, which drops its focus; Tab must not leave the dialog.
    for (let i = 0; i < 5; i++) {
      await page.keyboard.press("Tab");
      expect(await dialog.evaluate((d) => d.contains(document.activeElement))).toBe(true);
    }
  });

  test("a long dialog scrolls the control Tab wraps to into view", async ({ page }) => {
    const now = new Date().toISOString();
    const turn = (i: number) => ({ msg_id: `m${i}`, agent: "cursor", role: "user", content: `turn ${i}`, ts: now });
    await installApi(page);
    await page.route("**/v1/conversation.threads", (route) =>
      route.fulfill({
        json: {
          threads: [
            {
              thread_id: "t-long",
              agent: "cursor",
              msg_count: 200,
              first_seen: now,
              last_seen: now,
              last_role: "user",
              last_content_excerpt: "x",
              title: "Long thread",
            },
          ],
        },
      }),
    );
    await page.route("**/v1/conversation.get", (route) =>
      route.fulfill({
        json: {
          found: true,
          thread_id: "t-long",
          title: "Long thread",
          agent: "cursor",
          msg_count: 200,
          created_at: null,
          updated_at: null,
          messages: Array.from({ length: 200 }, (_, i) => turn(i)),
        },
      }),
    );
    await page.goto("/conversations");
    await page.getByText("Long thread").first().click();
    const dialog = page.getByRole("dialog");
    // The transcript is the dialog's last stop; Tab wraps from it to Close at the top.
    await dialog.getByRole("region", { name: "Transcript" }).focus();
    await page.keyboard.press("Tab");
    const close = dialog.getByRole("button", { name: "Close" });
    await expect(close).toBeFocused();
    await expect(close).toBeInViewport();
  });

  test("Conversations with recent threads", async ({ page }) => {
    const now = new Date().toISOString();
    await installApi(page);
    await page.route("**/v1/conversation.threads", (route) =>
      route.fulfill({
        json: {
          threads: [
            {
              thread_id: "t-1",
              agent: "cursor",
              msg_count: 2,
              first_seen: now,
              last_seen: now,
              last_role: "user",
              last_content_excerpt: "x",
              title: "Deploy plan",
            },
          ],
        },
      }),
    );
    await page.goto("/conversations");
    await expect(page.getByText("Deploy plan").first()).toBeVisible();
    expect(await violations(page)).toEqual([]);
  });

  test("an open Select list", async ({ page }) => {
    await open(page, "/settings");
    await page.getByRole("button", { name: /Home namespace/ }).click();
    await expect(page.locator("[cui-select-item]").first()).toBeVisible();
    expect(await violations(page)).toEqual([]);
  });

  test("the task sheet at 1000 px", async ({ page }) => {
    await page.setViewportSize({ width: 1000, height: 900 });
    await open(page, "/tasks");
    await page.getByRole("row", { name: "Task 2: a regression the public bot found" }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
    expect(await violations(page)).toEqual([]);
  });

  test.describe("375 px", () => {
    test.use({ viewport: { width: 375, height: 812 } });
    test("the More dialog opens on the dialog itself, not on Close", async ({ page }) => {
      await open(page, "/memories");
      await page.getByRole("button", { name: "More" }).click();
      const dialog = page.getByRole("dialog");
      await expect(dialog).toBeVisible();
      await expect(page.getByRole("button", { name: "Close" })).not.toBeFocused();
      expect(await violations(page)).toEqual([]);
      await page.keyboard.press("Escape");
      await expect(dialog).toBeHidden();
    });
  });
});

// The header search loads late; a keyboard user who tabs through its placeholder before it
// arrives keeps the focus where they put it (WCAG 3.2.1).
for (const gap of [200, 600]) {
  test(`the late header search leaves focus where Tab put it (${gap} ms)`, async ({ page }) => {
    await installApi(page, { memoryRows: 8 });
    await page.route(/HeaderSearch-.*\.js$/, async (route) => {
      await new Promise((r) => setTimeout(r, 1500));
      await route.continue();
    });
    await page.goto("/memories");
    await expect(page.locator("main h1").first()).toBeVisible();
    await page.getByRole("button", { name: /^Namespace:/ }).focus();
    await page.keyboard.press("Tab");
    await page.waitForTimeout(gap);
    await page.keyboard.press("Tab");
    const there = await page.evaluate(() => document.activeElement?.outerHTML.slice(0, 80));
    await page.waitForTimeout(2500);
    expect(await page.evaluate(() => document.activeElement?.outerHTML.slice(0, 80))).toBe(there);
    await expect(page.getByRole("listbox", { name: "Search results" })).toHaveCount(0);
  });
}

// A fast Tab walk through the header search leaves its panel closed and no Radix focus
// guards (invisible, ringless tab stops) behind.
test("a fast Tab walk through header search leaves nothing open behind it", async ({ page }) => {
  await installApi(page);
  for (let walk = 0; walk < 6; walk++) {
    await page.goto("/admin/usage");
    await expect(page.locator("main h1").first()).toBeVisible();
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(400);
    await page.locator("body").click({ position: { x: 1, y: 1 } });
    await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
    for (let i = 0; i < 30; i++) await page.keyboard.press("Tab");
    expect(await page.evaluate(() => document.querySelectorAll("[data-radix-focus-guard]").length)).toBe(0);
    await expect(page.getByRole("dialog", { name: "Search results" })).toHaveCount(0);
  }
});

// Click UI's focus ring shows at once; Tailwind's transition-colors would fade it in.
test("the focus ring is the focus colour from the first frame", async ({ page }) => {
  await open(page, "/memories");
  const trigger = page.getByRole("button", { name: /^Namespace: / });
  await page.keyboard.press("Tab");
  await trigger.focus();
  const ring = await trigger.evaluate((el) => {
    const probe = document.createElement("span");
    probe.style.color = "var(--color-focus)";
    document.body.append(probe);
    const focus = getComputedStyle(probe).color;
    probe.remove();
    return { outline: getComputedStyle(el).outlineColor, focus };
  });
  expect(ring.outline).toBe(ring.focus);
});

// WCAG 2.1.4: the one-key shortcuts can be turned off, and ? then types nothing.
test("the single-key shortcuts turn off from the help dialog", async ({ page }) => {
  await open(page, "/memories");
  await page.getByRole("button", { name: "Keyboard shortcuts" }).click();
  const help = page.getByRole("dialog", { name: "Keyboard shortcuts" });
  await help.getByRole("switch", { name: "Single-key shortcuts" }).click();
  await page.keyboard.press("Escape");
  await expect(help).toBeHidden();
  await page.locator("main h1").first().click();
  await page.keyboard.press("?");
  await page.waitForTimeout(300);
  await expect(help).toBeHidden();
});

test("reduced motion stops Click UI's dialog animation", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await open(page, "/memories");
  await page.keyboard.press("?");
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  expect(await dialog.evaluate((el) => getComputedStyle(el).animationName)).toBe("none");
});
