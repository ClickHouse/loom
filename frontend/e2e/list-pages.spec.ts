import { expect, test, type Locator, type Page } from "@playwright/test";
import * as fs from "node:fs";
import { violations } from "./axe";
import { codeReposFixture, installApi, LOADED_ROW, NAMESPACE, trackerSourcesFixture } from "./fixtures";

// The list pages against the list rules in `dashboard-design.md`, in the browser.

const now = new Date().toISOString();
const THREADS = Array.from({ length: 6 }, (_, i) => ({
  thread_id: `t-${i}`,
  agent: ["claude-code", "cursor"][i % 2],
  msg_count: 3 + i,
  first_seen: now,
  last_seen: now,
  last_role: "assistant",
  last_content_excerpt: "Summarised the change and pushed the branch.",
  title: i === 0 ? "" : `Refactor the ingest lane ${i}`,
}));

async function open(page: Page, path: string) {
  await installApi(page, {
    memoryRows: 12,
    knowledgeRows: 6,
    profileRows: 3,
    codeRepos: codeReposFixture(),
    trackerSources: trackerSourcesFixture(),
  });
  await page.route("**/v1/conversation.threads", (route) => route.fulfill({ json: { threads: THREADS } }));
  await page.goto(path);
  await expect(page.locator(LOADED_ROW).first()).toBeVisible();
}

// A phone is measured with a touch pointer, where every control is 44 px.
const BUDGET = [
  { width: 1400, height: 900, top: 300, touch: false },
  { width: 375, height: 812, top: 360, touch: true },
];

for (const { width, height, top, touch } of BUDGET) {
  test.describe(`${width} px`, () => {
    test.use({ viewport: { width, height }, hasTouch: touch, isMobile: touch });

    // The first row starts at or above y = 300 on a desktop, 360 on a phone.
    for (const path of ["/memories", "/library", "/conversations", "/profile", "/spaces", "/repositories"]) {
      test(`${path}: the first row starts at or above y = ${top}`, async ({ page }) => {
        await open(page, path);
        const box = await page.locator(LOADED_ROW).first().boundingBox();
        expect(box?.y ?? Infinity).toBeLessThanOrEqual(top);
      });
    }

    // Each list's search placeholder fits its field, at desktop and phone width.
    for (const path of ["/memories", "/library", "/conversations", "/profile", "/spaces", "/repositories"]) {
      test(`${path}: the search's placeholder fits its field`, async ({ page }) => {
        await open(page, path);
        const field = page.locator("main search input").first();
        const fit = await field.evaluate((el) => {
          const input = el as HTMLInputElement;
          const css = getComputedStyle(input);
          const ctx = document.createElement("canvas").getContext("2d")!;
          ctx.font = `${css.fontWeight} ${css.fontSize} ${css.fontFamily}`;
          const room = input.clientWidth - parseFloat(css.paddingLeft) - parseFloat(css.paddingRight);
          return { text: ctx.measureText(input.placeholder).width, room, placeholder: input.placeholder };
        });
        expect(fit.text, `"${fit.placeholder}"`).toBeLessThanOrEqual(fit.room);
      });
    }

    test("a Shift-click selects the range, and Escape clears the selection", async ({ page }) => {
      await open(page, "/memories");
      const boxes = page.locator("main .list-row").getByRole("checkbox");
      await boxes.nth(0).click();
      await boxes.nth(2).click({ modifiers: ["Shift"] });
      const bar = page.getByRole("search", { name: "Search and filters" });
      await expect(bar).toContainText("3 selected");
      await expect(boxes.nth(1)).toBeChecked();
      await page.keyboard.press("Escape");
      await expect(bar).not.toContainText("selected");
      await expect(boxes.nth(1)).not.toBeChecked();
    });

    test("a library row's More menu is there without a hover, and the row keeps its title", async ({ page }) => {
      await open(page, "/library");
      const row = page.locator("main [data-doc-row]").first();
      const more = row.getByRole("button", { name: /^Actions for / });
      await expect(more).toBeVisible();
      expect((await row.getByRole("button", { name: /^Preview / }).boundingBox())?.width ?? 0).toBeGreaterThan(200);
      await more.click();
      await expect(page.getByRole("menuitem", { name: "Copy URI" })).toBeVisible();
      expect(await violations(page)).toEqual([]);
    });
  });
}

// A row's placeholder size (content-visibility) is the list's drawn row height, so the
// scrollbar does not jump as rows come into view; and the first read's skeleton rows are
// the drawn rows' height and spacing, so the rows land where the skeleton was. On a desktop
// and on a phone, where a row's meta takes its own line and the pointer is a finger.
const LISTS = [
  { path: "/memories", op: "memory.list" },
  { path: "/library", op: "knowledge.list" },
  { path: "/conversations", op: "conversation.threads" },
];
for (const { width, touch } of [
  { width: 1400, touch: false },
  { width: 375, touch: true },
]) {
  test.describe(`rows at ${width} px`, () => {
    test.use({ viewport: { width, height: 900 }, hasTouch: touch, isMobile: touch });
    test.beforeEach(async ({ page }) => {
      expect(await page.evaluate(() => matchMedia("(pointer: coarse)").matches)).toBe(touch);
    });
    for (const { path, op } of LISTS) {
      test(`${path}: a row's placeholder size is its drawn height`, async ({ page }) => {
        await open(page, path);
        const row = page.locator(LOADED_ROW).first();
        await row.locator("[data-row-open]").first().focus();
        const { drawn, intrinsic } = await row.evaluate((el) => {
          const drawn = el.getBoundingClientRect().height;
          // A fresh skipped row uses the estimate, without a remembered content size.
          const skipped = el.cloneNode(true) as HTMLElement;
          skipped.style.contentVisibility = "hidden";
          el.after(skipped);
          const intrinsic = skipped.getBoundingClientRect().height;
          skipped.remove();
          return { drawn, intrinsic };
        });
        expect(Math.abs(drawn - intrinsic), `drawn ${drawn}, placeholder ${intrinsic}`).toBeLessThanOrEqual(8);
      });

      test(`${path}: the rows land the height and spacing of the skeleton`, async ({ page }) => {
        let answer = () => {};
        const held = new Promise<void>((resolve) => {
          answer = resolve;
        });
        const boxes = (selector: string) =>
          page.locator(selector).evaluateAll((els) =>
            els.slice(0, 2).map((el) => {
              const r = el.getBoundingClientRect();
              return { top: r.top, height: r.height };
            }),
          );
        await installApi(page, { memoryRows: 12, knowledgeRows: 6 });
        await page.route("**/v1/conversation.threads", (route) => route.fulfill({ json: { threads: THREADS } }));
        // The list's read waits, so its skeleton can be measured.
        await page.route(`**/v1/${op}`, async (route) => {
          await held;
          await route.fallback();
        });
        await page.goto(path);
        await expect(page.locator("main .list-row-skeleton").nth(1)).toBeAttached();
        const [s1, s2] = await boxes("main .list-row-skeleton");
        answer();
        await expect(page.locator(LOADED_ROW).nth(1)).toBeVisible();
        const [r1, r2] = await boxes(LOADED_ROW);
        expect(Math.abs(r1.height - s1.height), `row ${r1.height}, skeleton ${s1.height}`).toBeLessThanOrEqual(8);
        const pitch = { rows: r2.top - r1.top, skeleton: s2.top - s1.top };
        expect(Math.abs(pitch.rows - pitch.skeleton), JSON.stringify(pitch)).toBeLessThanOrEqual(2);
      });
    }
  });
}

// Only the current row's controls are tab stops: every other row's tag chips and menu
// triggers are out of the tab order too.
for (const path of ["/memories", "/library", "/conversations", "/profile", "/spaces", "/repositories"]) {
  test(`${path}: no row but the current one has a tab stop`, async ({ page }) => {
    await open(page, path);
    // The roving tab stop is set once a row is current.
    await page.waitForFunction(() =>
      [...document.querySelectorAll("main .list-row [data-row-open]")].some((el) => (el as HTMLElement).tabIndex === 0),
    );
    const outside = await page.evaluate(() => {
      const rows = [...document.querySelectorAll("main .list-row")];
      const current = rows.find((r) => r.querySelector<HTMLElement>("[data-row-open]")?.tabIndex === 0);
      const focusable = 'a[href], button, input, select, textarea, [tabindex], [role="button"], [role="checkbox"]';
      return rows
        .filter((r) => r !== current)
        .flatMap((r) => [...r.querySelectorAll<HTMLElement>(focusable)])
        .filter((el) => el.tabIndex >= 0 && !(el as HTMLButtonElement).disabled)
        .map((el) => el.getAttribute("aria-label") ?? el.textContent);
    });
    expect(outside).toEqual([]);
  });
}

test("a row list is one tab stop, the arrows move through it, and the ring is drawn on the row", async ({ page }) => {
  await open(page, "/library");
  const opens = page.locator("main [data-row-open]");
  await opens.first().focus();
  await page.keyboard.press("ArrowDown");
  await expect(opens.nth(1)).toBeFocused();
  expect(await opens.evaluateAll((els) => els.filter((e) => (e as HTMLElement).tabIndex === 0).length)).toBe(1);
  // Focus from the keyboard: the row draws the ring, around its meta and its menu too.
  await page.keyboard.press("ArrowDown");
  const ring = await page
    .locator("main .list-row")
    .nth(2)
    .evaluate((el) => getComputedStyle(el).outlineStyle);
  expect(ring).toBe("solid");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("dialog")).toBeVisible();
});

// The list asks for as many characters as two lines of the value hold at 1400 px, so a long
// value fills both lines rather than ending one line in (at 160 characters it ends one line
// in), and ends in "…" where the row holds more than the card shows.
test("a long memory value fills the card's two lines at 1400 px and ends in a cut mark", async ({ page }) => {
  await installApi(page, { memoryRows: 12 });
  await page.goto("/memories");
  const value = page.locator('main .list-row [id$="-value"]').first();
  await expect(value).toBeVisible();
  const { height, line, text } = await value.evaluate((el) => ({
    height: el.getBoundingClientRect().height,
    line: Number.parseFloat(getComputedStyle(el).lineHeight),
    text: el.textContent ?? "",
  }));
  expect(Math.round(height)).toBe(Math.round(2 * line));
  expect(text.endsWith("…"), "a cut value ends in a cut mark").toBe(true);
});

// A ranked search hit's similarity and score sit in the row's meta line, inside its open
// button: on one line beside the key and "Why this result", the similarity screen-reader
// text under 640 px, read in the row's description, and a click on them opens the row.
for (const width of [375, 700, 1400]) {
  test.describe(`a ranked hit at ${width} px`, () => {
    // A phone is measured with a touch pointer, as the other 375 px checks are.
    const touch = width === 375;
    test.use({ viewport: { width, height: 800 }, hasTouch: touch, isMobile: touch });

    test("its scores sit on one line, clear of the key and the button, and open the row", async ({ page }) => {
      // A long agent name: the meta line still ends inside the row.
      await installApi(page, { memoryRows: 12, firstMemoryAgent: "ingest-lane-partition-watchdog-agent" });
      // No row holds the typed text, so both hits are ranked rows rather than keyword rows.
      await page.route("**/v1/memory.list", (route) =>
        route.request().postDataJSON()?.contains
          ? route.fulfill({ json: { entries: [], total: 0, today: 0, truncated: false } })
          : route.fallback(),
      );
      await page.route("**/v1/memory.search", (route) =>
        route.fulfill({
          json: {
            embedding_model: "e2e",
            results: [0, 1].map((i) => ({
              memory_key: `alerts/2026-09-19/000${i}`,
              memory_type: "semantic",
              content_excerpt: "",
              distance: 0.3,
              similarity: 0.72 - i * 0.1,
              rank_score: 0.81 - i * 0.1,
              confidence: 1,
            })),
          },
        }),
      );
      await page.goto("/memories");
      await page.getByRole("textbox", { name: "Search memories" }).fill("partition lag");
      const row = page.locator('main [data-memory-key="alerts/2026-09-19/0000"]');
      const scores = row.getByTestId("search-score");
      await expect(scores).toBeVisible();
      const open = row.getByRole("button", { name: "alerts/2026-09-19/0000" });
      await expect(open).toHaveAccessibleDescription(/similarity 0\.72 · score 0\.81/);

      const box = async (l: Locator) => {
        const b = await l.boundingBox();
        if (!b) throw new Error("the element has no box");
        return b;
      };
      const s = await box(scores);
      const line = await scores.evaluate((el) => Number.parseFloat(getComputedStyle(el).lineHeight));
      expect(s.height, "the scores sit on one line").toBeLessThan(1.5 * line);
      const similarityShown = await scores
        .getByText(/similarity/)
        .evaluate((el) => el.getBoundingClientRect().width > 1);
      expect(similarityShown).toBe(width >= 640);
      const overlaps = (a: { x: number; y: number; width: number; height: number }, b: typeof a) =>
        a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
      const key = await box(open.locator("span[aria-hidden='true']").filter({ hasText: "alerts/2026-09-19/0000" }));
      const why = await box(row.getByRole("button", { name: "Why this result" }));
      expect(overlaps(s, key), "the scores clear the key").toBe(false);
      expect(overlaps(s, why), "the scores clear the button").toBe(false);
      const time = await box(open.locator("time"));
      const card = await box(row);
      expect(time.x + time.width, "the time ends inside the row").toBeLessThanOrEqual(card.x + card.width);

      await scores.click();
      await expect(page.getByRole("dialog")).toBeVisible();
    });
  });
}

test("a row's +N tag chip shows the folded tags on hover, above the row's open button", async ({ page }) => {
  // Five tags on the first row: three chips, then +2.
  await installApi(page, { memoryRows: 12, firstMemoryTags: ["alerts", "storage", "lane-0", "gateway", "edge"] });
  await page.goto("/memories");
  const more = page
    .locator("main .list-row")
    .first()
    .getByRole("note")
    .filter({ hasText: "2 more tags: gateway, edge" });
  await more.hover();
  await expect(page.getByRole("tooltip")).toHaveText("gateway, edge");
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test.describe("a touch pointer at 375 px", () => {
  test.use({ viewport: { width: 375, height: 812 }, hasTouch: true, isMobile: true });

  // A touch does not open the tip, so a tap on "+N" reaches the row's open button, and the
  // drawer it opens lists every tag.
  test("a tap on a row's +N tag chip opens the row's drawer", async ({ page }) => {
    await installApi(page, { memoryRows: 12, firstMemoryTags: ["alerts", "storage", "lane-0", "gateway", "edge"] });
    await page.goto("/memories");
    const more = page
      .locator("main .list-row")
      .first()
      .getByRole("note")
      .filter({ hasText: "2 more tags: gateway, edge" });
    const box = await more.boundingBox();
    if (!box) throw new Error("the +N chip has no box");
    await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2);
    await expect(page.getByRole("dialog")).toBeVisible();
  });

  // A member's guide summary in Spaces is a 12 px line of text, but its link is a button,
  // and every button is a 44 px target under a coarse pointer: a tap near either edge opens
  // the guide, not the row's join.
  test("a Spaces row's guide summary link is a 44 px target", async ({ page }) => {
    await installApi(page, {
      browseRows: 3,
      mine: [
        { namespace: NAMESPACE, role: "writer", status: "active", visibility: "private", kind: "memory" },
        { namespace: "team-002", role: "writer", status: "active", visibility: "public", kind: "memory" },
      ],
      claims: [NAMESPACE, "team-002"],
    });
    await page.goto("/spaces");
    const link = page.locator("[data-guide-link]");
    await expect(link).toBeVisible();
    const box = await link.boundingBox();
    if (!box) throw new Error("the guide link has no box");
    expect(Math.round(box.height)).toBeGreaterThanOrEqual(44);
    for (const dy of [3, box.height - 3]) {
      await page.touchscreen.tap(box.x + 12, box.y + dy);
      await expect(page.getByRole("dialog", { name: "Guide for team-002" })).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(page.getByRole("dialog")).toHaveCount(0);
    }
  });

  // A tag chip keeps its 24 px box, so the row keeps its height, and takes a 44 px tap: a
  // tap 8 px above the box filters by the tag instead of opening the row, and one 8 px below
  // takes the filter off (the row paints only its own box, content-visibility, and the hit
  // area below the chip still lies inside it).
  test("a row's tag chip is a 44 px target above and below that does not grow the row", async ({ page }) => {
    await installApi(page, { memoryRows: 12, firstMemoryTags: ["alerts", "storage"] });
    await page.goto("/memories");
    const tag = page.locator("main .list-row").first().getByRole("button", { name: "Filter by tag alerts" });
    const box = await tag.boundingBox();
    if (!box) throw new Error("the tag chip has no box");
    expect(Math.round(box.height)).toBeLessThan(44);
    await page.touchscreen.tap(box.x + box.width / 2, box.y - 8);
    await expect(tag).toHaveAttribute("aria-pressed", "true");
    const after = await tag.boundingBox();
    if (!after) throw new Error("the tag chip has no box");
    await page.touchscreen.tap(after.x + after.width / 2, after.y + after.height + 8);
    await expect(tag).toHaveAttribute("aria-pressed", "false");
    await expect(page.getByRole("dialog")).toHaveCount(0);
  });

  type Box = { x: number; y: number; width: number; height: number };
  // Press a row's tag by a tap at the point `at` gives for the chip's box, then take the
  // filter off by the same tap.
  async function tapTwice(page: Page, tag: string, at: (box: Box) => [number, number]) {
    const chip = page
      .locator("main .list-row")
      .first()
      .getByRole("button", { name: `Filter by tag ${tag}` });
    for (const pressed of ["true", "false"]) {
      const box = await chip.boundingBox();
      if (!box) throw new Error(`the ${tag} chip has no box`);
      const [x, y] = at(box);
      await page.touchscreen.tap(x, y);
      await expect(chip, `${tag} at ${Math.round(x - box.x)}, ${Math.round(y - box.y)}`).toHaveAttribute(
        "aria-pressed",
        pressed,
      );
    }
  }

  // Tags that would wrap at 375 px stay on one line under a finger, so no tag's 44 px hit
  // area lies over another tag: a tap just inside a chip's bottom edge presses that chip.
  const WRAPPING = ["incident-response", "storage", "alerts", "x", "y"];
  test("a row's tags stay on one line, and a tap inside a chip presses that chip", async ({ page }) => {
    await installApi(page, { memoryRows: 12, firstMemoryTags: WRAPPING });
    await page.goto("/memories");
    const row = page.locator("main .list-row").first();
    const chips = row.getByRole("button", { name: /^Filter by tag / });
    await expect(chips).toHaveCount(3);
    const tops = await chips.evaluateAll((els) => els.map((el) => Math.round(el.getBoundingClientRect().top)));
    expect(new Set(tops).size, `chip tops ${tops}`).toBe(1);
    // Near each end and in the middle, so a hit area from a tag on another line shows wherever
    // it lies over the chip.
    for (const tag of WRAPPING.slice(0, 3)) {
      for (const at of [0.1, 0.5, 0.9]) await tapTwice(page, tag, (b) => [b.x + b.width * at, b.y + b.height - 2]);
    }
  });

  // A one-letter tag is 44 px wide and its hit area adds height only, so a tap at either
  // outer edge of its 44 px target presses that tag, not the neighbour whose hit area would
  // otherwise reach over the gap.
  test("a tap at a short tag's outer edge presses that tag", async ({ page }) => {
    await installApi(page, { memoryRows: 12, firstMemoryTags: ["x", "y", "z"] });
    await page.goto("/memories");
    for (const tag of ["x", "y", "z"]) {
      for (const dx of [-21, 21]) await tapTwice(page, tag, (b) => [b.x + b.width / 2 + dx, b.y + b.height / 2]);
    }
  });
});

test("an old Insights link shows the list, with the audit's card in the assistant panel", async ({ page }) => {
  await installApi(page, { memoryRows: 12 });
  await page.goto("/memories?section=insights");
  await expect(page).toHaveURL(new RegExp(`/memories\\?namespace=${NAMESPACE}$`));
  await expect(page.getByRole("tab", { name: "Insights" })).toHaveCount(0);
  await expect(page.locator("main .list-row").first()).toBeVisible();
  await expect(page.locator("[data-card='namespace.audit']")).toBeVisible();
  expect(await violations(page)).toEqual([]);
  // The header's Insights button opens the starters again.
  await expect(page.getByRole("button", { name: "Insights" })).toBeVisible();
});

test("the Profile's sections are a list with a selection toolbar", async ({ page }) => {
  await open(page, "/profile");
  const boxes = page.locator("main .list-row").getByRole("checkbox");
  await boxes.nth(0).click();
  await boxes.nth(2).click({ modifiers: ["Shift"] });
  await expect(page.getByRole("search", { name: "Search and filters" })).toContainText("3 selected");
  expect(await violations(page)).toEqual([]);
});

// The Kbd and Recall Lab bar rules use Click UI's radius tokens, not raw 4 px and 2 px (the
// computed values match, so the stylesheet itself is read). It runs here, not in vitest,
// whose CSS handling empties a `.css` import.
test("styles.css draws the Kbd and the score bar on radius tokens", () => {
  const css = fs.readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
  const rule = (selector: string) => css.slice(css.indexOf(`${selector} {`)).split("}")[0];
  for (const selector of [".kbd", ".recall-bar", ".recall-bar > span"]) {
    expect(rule(selector), selector).toMatch(/border-radius: var\(--/);
  }
});

// The guide link is only as wide as its text: a click beside it on its line still opens
// the row (a member's Request upgrade), not the guide.
test.describe("1400 px, the guide summary line", () => {
  test.use({ viewport: { width: 1400, height: 900 } });
  test("a click beside a Spaces row's guide link is the row's", async ({ page }) => {
    await installApi(page, {
      browseRows: 3,
      mine: [
        { namespace: NAMESPACE, role: "writer", status: "active", visibility: "private", kind: "memory" },
        { namespace: "team-002", role: "writer", status: "active", visibility: "public", kind: "memory" },
      ],
      claims: [NAMESPACE, "team-002"],
    });
    await page.goto("/spaces");
    const link = page.locator("[data-guide-link]");
    await expect(link).toBeVisible();
    const box = await link.boundingBox();
    const row = await page.locator(".list-row", { has: link }).boundingBox();
    if (!box || !row) throw new Error("the guide link or its row has no box");
    const beside = box.x + box.width + 24;
    expect(beside).toBeLessThan(row.x + row.width / 2);
    await page.mouse.click(beside, box.y + box.height / 2);
    await expect(page.getByRole("dialog")).toBeVisible();
    await expect(page.getByRole("dialog", { name: "Guide for team-002" })).toHaveCount(0);
  });
});
