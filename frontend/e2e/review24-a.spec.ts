import { expect, test, type Page } from "@playwright/test";
import * as fs from "node:fs";
import { createRequire } from "node:module";
import { installApi } from "./fixtures";

// Layout, focus and announcement checks on Tasks, the sidebar, Profile, Library, Runs and
// Memories: each one a case a review found broken.

const AXE = fs.readFileSync(createRequire(import.meta.url).resolve("axe-core/axe.min.js"), "utf8");
const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

async function violations(page: Page): Promise<Array<{ id: string; nodes: string[] }>> {
  await page.addScriptTag({ content: AXE });
  return page.evaluate(async (tags) => {
    const axe = (
      window as unknown as {
        axe: {
          run: (
            ctx: Document,
            opts: object,
          ) => Promise<{ violations: Array<{ id: string; nodes: Array<{ target: string[] }> }> }>;
        };
      }
    ).axe;
    const out = await axe.run(document, { runOnly: { type: "tag", values: tags } });
    return out.violations.map((v) => ({ id: v.id, nodes: v.nodes.slice(0, 5).map((n) => n.target.join(" ")) }));
  }, TAGS);
}

async function open(page: Page, path: string) {
  await installApi(page, { memoryRows: 8, knowledgeRows: 4, runRows: 6, profileRows: 3 });
  await page.goto(path);
  await expect(page.locator("main h1").first()).toBeVisible();
  await page.waitForLoadState("networkidle");
}

/** The name of the focused element, or "body". */
const focused = (page: Page) =>
  page.evaluate(() => {
    const el = document.activeElement as HTMLElement | null;
    if (!el || el === document.body) return "body";
    return el.getAttribute("aria-label") || el.textContent?.trim() || el.tagName;
  });

for (const width of [1280, 1400, 1440]) {
  test(`at ${width} px a task row beside the open panel keeps its title and no two parts overlap`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await installApi(page);
    await page.goto("/tasks");
    await page.getByRole("row", { name: /^Task 1/ }).click();
    await expect(page.getByRole("complementary", { name: "Task detail" })).toBeVisible();
    const problems = await page.evaluate(() => {
      const out: string[] = [];
      const box = (e: Element) => e.getBoundingClientRect();
      const hits = (a: DOMRect, b: DOMRect) =>
        a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1;
      for (const row of document.querySelectorAll(".tq-row")) {
        const title = row.querySelector(".title");
        if (!title || box(title).width < 40)
          out.push(`${row.getAttribute("aria-label")}: title ${box(title!).width}px`);
        const parts = [...row.querySelectorAll(".t > *, .tq-sub, .tq-meta > :not(.tq-acts), .tq-acts > *")].filter(
          (e) => box(e).width > 0,
        );
        for (let i = 0; i < parts.length; i++)
          for (let j = i + 1; j < parts.length; j++)
            if (!parts[i].contains(parts[j]) && hits(box(parts[i]), box(parts[j])))
              out.push(`${row.getAttribute("aria-label")}: ${parts[i].className} over ${parts[j].className}`);
      }
      // A group's name stays on one line.
      for (const name of document.querySelectorAll("[id^='tq-group-']"))
        if (box(name).height > 24) out.push(`group ${name.textContent} wraps`);
      return out;
    });
    expect(problems).toEqual([]);
  });
}

test("the sidebar toggle keeps the focus when Enter collapses and expands the rail", async ({ page }) => {
  await open(page, "/memories");
  const toggle = page.getByRole("button", { name: "Collapse sidebar" });
  await toggle.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("button", { name: "Expand sidebar" })).toBeFocused();
  await expect(page.getByRole("button", { name: "Expand sidebar" })).toHaveAttribute("aria-expanded", "false");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("button", { name: "Collapse sidebar" })).toBeFocused();
  await expect(page.getByRole("button", { name: "Collapse sidebar" })).toHaveAttribute("aria-expanded", "true");
});

test("the collapsed rail reads the review-queue and agents counts in the link names", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("loom_sidebar_collapsed", "1"));
  await installApi(page);
  await page.route("**/v1/conflicts.list", (r) => r.fulfill({ json: { rows: [], total: 3 } }));
  await page.route("**/v1/agents.connected", (r) => r.fulfill({ json: { new_count: 2 } }));
  await page.goto("/memories");
  await expect(page.getByRole("link", { name: "Review queue, 3" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Agents, 2" })).toBeVisible();
});

test("a profile editor takes the focus, Escape and Cancel close it, and the opener gets the focus back", async ({
  page,
}) => {
  await open(page, "/profile");
  const edit = page.getByRole("button", { name: /^Edit / }).first();
  const name = (await edit.getAttribute("aria-label")) ?? "";

  await edit.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("textbox", { name: "Section text" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("textbox", { name: "Section text" })).toHaveCount(0);
  await expect(page.getByRole("button", { name })).toBeFocused();

  await page.keyboard.press("Enter");
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("button", { name })).toBeFocused();

  await page.keyboard.press("Enter");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("button", { name })).toBeFocused();

  const add = page.getByRole("button", { name: "New section" });
  await add.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("textbox", { name: "Section label" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("textbox", { name: "Section label" })).toHaveCount(0);
  expect(await focused(page)).toBe("New section");
});

test.describe("375 px", () => {
  test.use({ viewport: { width: 375, height: 812 } });

  test("a library card keeps its metadata and tags inside the card", async ({ page }) => {
    await open(page, "/library");
    const cards = page.locator("main [data-doc-row]");
    await expect(cards.first()).toBeVisible();
    const outside = await page.evaluate(() =>
      [...document.querySelectorAll("main [data-doc-row]")].flatMap((card) => {
        const right = card.getBoundingClientRect().right;
        return [...card.querySelectorAll("button, span")]
          .filter((e) => e.getBoundingClientRect().right > right + 0.5)
          .map((e) => e.textContent);
      }),
    );
    expect(outside).toEqual([]);
  });

  test("the current phone tab has the rail's selected fill", async ({ page }) => {
    await open(page, "/memories");
    const fills = await page.evaluate(() => {
      const tab = document.querySelector<HTMLElement>("nav[aria-label='Sections'] a[aria-current='page']");
      const probe = document.createElement("div");
      probe.style.background = "var(--click-sidebar-main-navigation-item-color-background-active)";
      document.body.append(probe);
      const want = getComputedStyle(probe).backgroundColor;
      probe.remove();
      return { tab: tab ? getComputedStyle(tab).backgroundColor : "", want };
    });
    expect(fills.tab).toBe(fills.want);
  });
});

test("library row actions keep their space before a hover, and a touch screen shows them", async ({ browser }) => {
  for (const touch of [false, true]) {
    const context = await browser.newContext({
      hasTouch: touch,
      isMobile: touch,
      viewport: { width: 1400, height: 900 },
    });
    const page = await context.newPage();
    await open(page, "/library");
    const row = page.locator("main [data-doc-row]").first();
    const preview = row.getByRole("button", { name: /^Preview / });
    const before = (await preview.boundingBox())?.width;
    if (touch) {
      await expect(row.getByRole("button", { name: "Delete document" })).toBeVisible();
    } else {
      await row.hover();
      await expect(row.getByRole("button", { name: "Delete document" })).toBeVisible();
      expect((await preview.boundingBox())?.width).toBe(before);
    }
    await context.close();
  }
});

test("the library's filter count is announced from a status region mounted before the search", async ({ page }) => {
  await open(page, "/library");
  const status = page.locator("main [role='status']");
  await expect(status).toHaveText("showing 4");
  await page.getByRole("textbox", { name: "Search library" }).fill("tag:lane-0");
  await expect(status).toHaveText("1 of 4 shown");
});

test("an errored field shows the focus ring", async ({ page }) => {
  await open(page, "/memories");
  const outline = await page.evaluate(() => {
    const input = document.createElement("input");
    input.setAttribute("aria-invalid", "true");
    document.querySelector("main")?.append(input);
    input.focus();
    const cs = getComputedStyle(input);
    return `${cs.outlineStyle} ${cs.outlineWidth}`;
  });
  expect(outline).toBe("solid 2px");
});

test("grouped runs label every column", async ({ page }) => {
  await open(page, "/runs");
  await page.getByRole("button", { name: "Group by session" }).click();
  const tables = page.locator("main table");
  await expect(tables.nth(1)).toBeVisible();
  for (const table of await tables.all()) await expect(table.getByRole("columnheader").first()).toBeAttached();
  expect(await violations(page)).toEqual([]);
});

test("a run's replay opens and has no WCAG A/AA violations", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await open(page, "/runs");
  await page.locator("main table tbody button").first().click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText("recalled keys")).toBeVisible();
  expect(errors).toEqual([]);
  expect(await violations(page)).toEqual([]);
});

test("the memory search summary is announced from a status region mounted before the search", async ({ page }) => {
  await open(page, "/memories");
  const regions = await page.locator("main [role='status']").elementHandles();
  await page.getByRole("textbox", { name: "Search memories" }).fill("lag");
  await expect
    .poll(async () => {
      const texts = await Promise.all(regions.map((r) => r.evaluate((e) => e.textContent ?? "")));
      return texts.some((t) => /result|match|Searching/i.test(t));
    })
    .toBe(true);
});

test("Escape on the memory list is left alone while nothing is selected", async ({ page }) => {
  await open(page, "/memories");
  const prevented = await page.evaluate(() => {
    const e = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    document.body.dispatchEvent(e);
    return e.defaultPrevented;
  });
  expect(prevented).toBe(false);
});

test("headings use Click UI's title type, the xl step at 27 px", async ({ page }) => {
  await open(page, "/memories");
  const type = await page.evaluate(() => {
    const out: Record<string, string> = {};
    for (const tag of ["h1", "h2", "h3"]) {
      const h = document.createElement(tag);
      h.textContent = "x";
      document.querySelector("main")?.append(h);
      const cs = getComputedStyle(h);
      out[tag] = `${cs.fontWeight} ${cs.fontSize}/${cs.lineHeight}`;
    }
    out.xl = getComputedStyle(document.documentElement).getPropertyValue("--text-xl--line-height").trim();
    return out;
  });
  expect(type).toEqual({ h1: "700 20px/30px", h2: "700 18px/27px", h3: "600 16px/24px", xl: "27px" });
});
