import { expect, test, type Page } from "@playwright/test";
import { installApi, NAMESPACE, ROUTES, SMALL_SEED, TASK_INBOX } from "./fixtures";

// Every keyboard focus ring is drawn whole. On each page, at desktop width and on a touch
// phone, Tab walks up to 40 stops and the arrow keys rove into each row list or the namespace
// picker's options met on the way. At each stop the ring (the focused element's outline, or
// the one an ancestor draws for it, as a list row does) must fit inside every box that clips
// it, within 0.5 px: an ancestor in its containing-block chain whose overflow is not visible.
// After Tab the browser scrolls, and leaves a partly visible element where it is, so
// a scroll box counts its whole scrollable area: the ring is clipped when no scrolling shows
// it. After an arrow key Loom scrolls the item itself, so the box's padding box counts.

const TAB_STOPS = 40;
const ARROW_STOPS = 24;

/** Each clipping box the focused element's ring overflows, described; none when it has no ring. */
async function clippedRings(page: Page, scrolled: "browser" | "loom"): Promise<string[]> {
  return page.evaluate((scrolled) => {
    const el = document.activeElement;
    if (!(el instanceof HTMLElement) || el === document.body || !el.matches(":focus-visible")) return [];
    const name = (e: Element) => {
      const label = e.getAttribute("aria-label") ?? (e.textContent ?? "").trim().slice(0, 32);
      const cls = e.classList.length ? `.${[...e.classList].slice(0, 3).join(".")}` : "";
      const role = e.getAttribute("role");
      return `${e.tagName.toLowerCase()}${role ? `[role=${role}]` : ""}${cls} "${label}"`;
    };
    // The next box whose overflow can clip `e`: its parent, or for a positioned box the
    // ancestor that positions it.
    const clipParent = (e: Element): Element | null => {
      const pos = getComputedStyle(e).position;
      let p = e.parentElement;
      const plain = (x: Element) => getComputedStyle(x).transform === "none" && getComputedStyle(x).filter === "none";
      if (pos === "fixed") while (p && plain(p)) p = p.parentElement;
      else if (pos === "absolute")
        while (p && getComputedStyle(p).position === "static" && plain(p)) p = p.parentElement;
      return p;
    };
    // The ring's element: the focused one, or the nearest ancestor drawing it (`:has(:focus-visible)`).
    let ringEl: Element | null = el;
    for (; ringEl && ringEl !== document.body; ringEl = ringEl.parentElement) {
      const s = getComputedStyle(ringEl);
      const drawn = s.outlineStyle !== "none" && parseFloat(s.outlineWidth) > 0;
      if (drawn && (ringEl === el || ringEl.matches(":has(:focus-visible)"))) break;
    }
    if (!ringEl || ringEl === document.body) return [];
    const s = getComputedStyle(ringEl);
    // The outline's outer edge sits outline-offset + outline-width from the border box: out
    // for an outset ring, in for one inset by more than its width.
    const reach = parseFloat(s.outlineWidth) + parseFloat(s.outlineOffset);
    const r = ringEl.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return [];
    const ring = { left: r.left - reach, right: r.right + reach, top: r.top - reach, bottom: r.bottom + reach };
    const out: string[] = [];
    type Box = { start: number; size: number; scroll: number; extent: number };
    for (
      let box = clipParent(ringEl);
      box && box !== document.body && box !== document.documentElement;
      box = clipParent(box)
    ) {
      const bs = getComputedStyle(box);
      const b = box.getBoundingClientRect();
      const left = b.left + box.clientLeft;
      const top = b.top + box.clientTop;
      const over: string[] = [];
      // One axis (`own`: the element's size on it); returns how far scrolling the box would
      // move the ring into its view, for the boxes further out.
      const axis = (overflow: string, lo: number, hi: number, own: number, at: Box, sides: [string, string]) => {
        // An element larger than the box (a tab panel taller than the page) is seen a part at a time.
        if (overflow === "visible" || own > at.size) return 0;
        const scrolls = scrolled === "browser" && (overflow === "auto" || overflow === "scroll");
        // A browser-scrolled box: anywhere in its scrollable area. Loom-scrolled: in view.
        const from = scrolls ? at.start - at.scroll : at.start;
        const to = scrolls ? from + at.extent : at.start + at.size;
        if (lo < from - 0.5) over.push(`${sides[0]} ${(from - lo).toFixed(1)}px`);
        if (hi > to + 0.5) over.push(`${sides[1]} ${(hi - to).toFixed(1)}px`);
        if (!scrolls) return 0;
        return lo < at.start ? at.start - lo : hi > at.start + at.size ? at.start + at.size - hi : 0;
      };
      const x = { start: left, size: box.clientWidth, scroll: box.scrollLeft, extent: box.scrollWidth };
      const y = { start: top, size: box.clientHeight, scroll: box.scrollTop, extent: box.scrollHeight };
      const dx = axis(bs.overflowX, ring.left, ring.right, r.width, x, ["left", "right"]);
      const dy = axis(bs.overflowY, ring.top, ring.bottom, r.height, y, ["top", "bottom"]);
      // The boxes further out see the ring where scrolling this one brings it.
      ring.left += dx;
      ring.right += dx;
      ring.top += dy;
      ring.bottom += dy;
      if (over.length) out.push(`${name(ringEl)} clipped by ${name(box)}: ${over.join(", ")}`);
    }
    return out;
  }, scrolled);
}

/** True when focus is on an item of an arrow-key list (a row, a tree item, a picker option)
 *  not roved yet on this page. */
async function newList(page: Page): Promise<boolean> {
  return page.evaluate(() => {
    const item = document.activeElement?.closest(
      '[role="row"], [role="treeitem"], [role="option"], [data-nav], [data-row-open]',
    );
    const list =
      item?.closest('[role="tree"], [role="grid"], [role="listbox"], table, [role="list"], ul') ?? item?.parentElement;
    if (!list) return false;
    const w = window as unknown as { __roved?: WeakSet<Element> };
    w.__roved ??= new WeakSet();
    if (w.__roved.has(list)) return false;
    w.__roved.add(list);
    return true;
  });
}

/** Tab through the page, roving into each arrow-key list met, and gather every clipped ring. */
async function walk(page: Page, where: string): Promise<string[]> {
  const found = new Set<string>();
  const check = async (scrolled: "browser" | "loom") => {
    for (const v of await clippedRings(page, scrolled)) found.add(`${where}: ${v}`);
  };
  for (let i = 0; i < TAB_STOPS; i++) {
    await page.keyboard.press("Tab");
    await check("browser");
    if (!(await newList(page))) continue;
    for (let j = 0; j < ARROW_STOPS; j++) {
      await page.keyboard.press("ArrowDown");
      await check("loom");
    }
  }
  return [...found];
}

// The picker's groups: memory namespaces, and more code indexes than an open group shows,
// so its "Show all" button is there too.
const ns = (namespace: string, kind: string) => ({
  namespace,
  role: kind === "code" ? "jwt" : "writer",
  status: "active",
  visibility: "private",
  kind,
  description: kind === "code" ? `acme/${namespace} @ main` : "",
  joined_at: "2026-01-01T00:00:00Z",
  is_new: false,
});
const MINE = [
  ns(NAMESPACE, "memory"),
  ns("team-notes", "memory"),
  ...Array.from({ length: 10 }, (_, i) => ns(`code-${i}`, "code")),
  ns("tracker-clickhouse", "qa"),
];

/** A to-do task in the e2e inbox, as `task.list` returns it. */
const task = (i: number) => ({
  task_id: `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`,
  inbox: TASK_INBOX,
  kind: "bug_report",
  priority: 2,
  status: "todo",
  reason: "",
  blocked_on: "",
  resume_at: null,
  deadline: null,
  not_before: null,
  defer_count: 0,
  title: `Task ${i}: a regression the public bot found`,
  from_agent: "reporter-public",
  from_subject: "sa:reporter-public-1a2b3c",
  owner: "",
  lease_until: null,
  parent_task_id: null,
  disclosure: "internal",
  created_at: "2026-09-20T01:00:00",
  updated_at: "2026-09-20T01:00:00",
  version: 1,
});

async function open(page: Page, path: string, mine?: typeof MINE) {
  await installApi(page, {
    memoryRows: 8,
    knowledgeRows: 4,
    runRows: 6,
    ...(mine ? { mine, claims: mine.map((r) => r.namespace) } : {}),
  });
  await page.goto(path);
  await expect(page.locator("main h1").first()).toBeVisible();
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(300); // the rows' entry animation
}

for (const phone of [false, true]) {
  test.describe(phone ? "375 px, touch" : "1400 px", () => {
    test.use(
      phone
        ? { viewport: { width: 375, height: 812 }, hasTouch: true, isMobile: true, reducedMotion: "reduce" }
        : { viewport: { width: 1400, height: 900 }, reducedMotion: "reduce" },
    );

    for (const path of ROUTES) {
      test(`${path}: every focus ring is drawn whole`, async ({ page }) => {
        await open(page, path);
        expect(await walk(page, path)).toEqual([]);
      });
    }

    test("the Explorer with a walk: every focus ring is drawn whole", async ({ page }) => {
      await open(page, `/graph?seed=${SMALL_SEED}`);
      await expect(page.getByRole("tree", { name: "Walk outline" }).getByRole("treeitem").first()).toBeVisible();
      expect(await walk(page, "/graph walk")).toEqual([]);
    });

    test("a task lane longer than it shows: every focus ring is drawn whole", async ({ page }) => {
      await open(page, "/tasks");
      // 24 to-do tasks: the lane shows 20 and a "Show all" button.
      await page.route("**/v1/task.list", (route) =>
        route.fulfill({ json: { tasks: Array.from({ length: 24 }, (_, i) => task(i + 1)), truncated: false } }),
      );
      await page.reload();
      await expect(page.getByRole("button", { name: "Show all 24" })).toBeVisible();
      expect(await walk(page, "/tasks long lane")).toEqual([]);
    });

    test("the tasks list with a lane folded: every focus ring is drawn whole", async ({ page }) => {
      await open(page, "/tasks");
      const header = page.getByRole("grid").getByRole("button", { name: /^Triage/ });
      await header.click();
      await expect(header).toHaveAttribute("aria-expanded", "false");
      expect(await walk(page, "/tasks folded")).toEqual([]);
    });

    // A phone shows the list only: a board's columns do not fit it.
    if (!phone)
      test("the tasks board: every focus ring is drawn whole", async ({ page }) => {
        await open(page, "/tasks");
        await page.getByRole("button", { name: "Board", exact: true }).click();
        await expect(page.getByRole("region", { name: /^To do/ })).toBeVisible();
        expect(await walk(page, "/tasks board")).toEqual([]);
      });

    test("the open namespace picker: every focus ring is drawn whole", async ({ page }) => {
      await open(page, "/memories", MINE);
      await page.getByRole("button", { name: /^Namespace: / }).click();
      const dialog = page.getByRole("dialog", { name: "Namespaces" });
      await dialog.getByRole("button", { name: /^Code indexes/ }).click();
      await expect(dialog.getByRole("button", { name: "Show all 10" })).toBeVisible();
      expect(await walk(page, "namespace picker")).toEqual([]);
    });

    test("the open memory inspector: every focus ring is drawn whole", async ({ page }) => {
      await open(page, "/memories");
      await page.locator("[data-memory-open]").first().click();
      await expect(page.getByRole("dialog")).toBeVisible();
      await page.waitForLoadState("networkidle");
      expect(await walk(page, "memory inspector")).toEqual([]);
    });

    test("the open task detail: every focus ring is drawn whole", async ({ page }) => {
      await open(page, "/tasks");
      await page.getByRole("row", { name: /^Task 1:/ }).click();
      await expect(page.getByRole("complementary", { name: "Task detail" }).or(page.getByRole("dialog"))).toBeVisible();
      expect(await walk(page, "task detail")).toEqual([]);
    });
  });
}
