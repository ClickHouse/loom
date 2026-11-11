import { expect, test, type Page } from "@playwright/test";
import { installApi, NAMESPACE, ROUTES, SMALL_SEED, TASK_INBOX } from "./fixtures";

// Every focus ring fits inside each box that clips it; after Tab a scroll box counts its whole
// scrollable area, after an arrow key only its visible area.

// A page's whole tab cycle, and each arrow-key list, fits in this many stops; a walk that
// reaches it fails.
const TAB_CAP = 200;

type Mode = "tab" | "arrow";
type Stop = {
  /** The focused element, short; the ring's element after it when an ancestor draws the ring. */
  name: string;
  /** Each clipping box the ring overflows, described. */
  clipped: string[];
  /** The focused element shows focus by no ring, and is not a text field (which shows it by
   *  its border, styles.css; fieldsWithoutFocusLook checks that border). */
  ringless: boolean;
  /** The targets the focused element or its ring's element matches. */
  matched: string[];
  /** Focus is on another element this walk checked already: after a Tab, the cycle is complete. */
  again: boolean;
  /** Focus moved since the last stop. */
  moved: boolean;
  /** Focus is outside `within`. */
  out: boolean;
  /** Focus is on an item of an arrow-key list not roved yet in this walk. */
  fresh: boolean;
  /** The dialogs open. */
  dialogs: number;
};

/** The focused element's stop: its ring checked, as `mode` scrolled it there. `start` makes it
 *  the walk's first stop. */
async function stopAt(page: Page, mode: Mode, start: boolean, within: string | null, targets: string[]): Promise<Stop> {
  return page.evaluate(
    ({ mode, start, within, targets }) => {
      const styles = new Map<Element, CSSStyleDeclaration>();
      const css = (e: Element) => {
        let s = styles.get(e);
        if (!s) {
          s = getComputedStyle(e);
          styles.set(e, s);
        }
        return s;
      };
      const name = (e: Element) => {
        const label = e.getAttribute("aria-label") ?? (e.textContent ?? "").trim().slice(0, 32);
        const cls = e.classList.length ? `.${[...e.classList].slice(0, 3).join(".")}` : "";
        const attrs = ["role", "data-nav", "aria-expanded"]
          .filter((a) => e.hasAttribute(a))
          .map((a) => `[${a}=${e.getAttribute(a)}]`)
          .join("");
        return `${e.tagName.toLowerCase()}${attrs}${cls} "${label}"`;
      };
      // The boxes that can clip `el`, inner first: the ancestors in its containing-block chain
      // whose overflow is not visible on an axis.
      const clipAncestors = (el: Element): Element[] => {
        const plain = (x: Element) => css(x).transform === "none" && css(x).filter === "none";
        const boxes: Element[] = [];
        for (let e = el; ; ) {
          let p = e.parentElement;
          if (css(e).position === "fixed") while (p && plain(p)) p = p.parentElement;
          else if (css(e).position === "absolute")
            while (p && css(p).position === "static" && plain(p)) p = p.parentElement;
          if (!p || p === document.body || p === document.documentElement) return boxes;
          if (css(p).overflowX !== "visible" || css(p).overflowY !== "visible") boxes.push(p);
          e = p;
        }
      };
      type Ring = { left: number; right: number; top: number; bottom: number; width: number; height: number };
      // The sides of `ring` that `box` clips, and how far scrolling the box moves the ring into
      // its view (what the boxes further out see).
      const overflow = (ring: Ring, box: Element, mode: Mode) => {
        const s = css(box);
        const b = box.getBoundingClientRect();
        const axes = [
          {
            overflow: s.overflowX,
            lo: ring.left,
            hi: ring.right,
            own: ring.width,
            start: b.left + box.clientLeft,
            size: box.clientWidth,
            scroll: box.scrollLeft,
            extent: box.scrollWidth,
            names: ["left", "right"],
          },
          {
            overflow: s.overflowY,
            lo: ring.top,
            hi: ring.bottom,
            own: ring.height,
            start: b.top + box.clientTop,
            size: box.clientHeight,
            scroll: box.scrollTop,
            extent: box.scrollHeight,
            names: ["top", "bottom"],
          },
        ];
        const sides: string[] = [];
        const shift = [0, 0];
        axes.forEach((a, i) => {
          // An element larger than the box (a tab panel taller than the page) is seen a part at a time.
          if (a.overflow === "visible" || a.own > a.size) return;
          const scrolls = mode === "tab" && (a.overflow === "auto" || a.overflow === "scroll");
          const from = scrolls ? a.start - a.scroll : a.start;
          const to = from + (scrolls ? a.extent : a.size);
          if (a.lo < from - 0.5) sides.push(`${a.names[0]} ${(from - a.lo).toFixed(1)}px`);
          if (a.hi > to + 0.5) sides.push(`${a.names[1]} ${(a.hi - to).toFixed(1)}px`);
          if (scrolls)
            shift[i] = a.lo < a.start ? a.start - a.lo : a.hi > a.start + a.size ? a.start + a.size - a.hi : 0;
        });
        return { sides, shift: { x: shift[0], y: shift[1] } };
      };
      // The arrow-key list an item belongs to: its tree, grid or listbox, else the nearest
      // ancestor holding another item.
      const ITEM = '[role="row"], [role="treeitem"], [role="option"], [data-nav], [data-row-open]';
      const listOf = (el: Element): Element | null => {
        const item = el.closest(ITEM);
        if (!item) return null;
        const owner = item.closest('[role="tree"], [role="grid"], [role="listbox"], table, [role="list"], ul');
        if (owner) return owner;
        let p = item.parentElement;
        while (p && p.querySelectorAll(ITEM).length < 2) p = p.parentElement;
        return p;
      };

      type State = { seen: WeakSet<Element>; roved: WeakSet<Element>; last: Element | null };
      const w = window as unknown as { __walk?: State };
      if (start || !w.__walk) w.__walk = { seen: new WeakSet(), roved: new WeakSet(), last: null };
      const walked = w.__walk;
      const at = document.activeElement;
      const el = at && at !== document.body ? at : null;
      const list = el ? listOf(el) : null;
      const stop = {
        name: "",
        clipped: [] as string[],
        ringless: false,
        matched: [] as string[],
        again: !!el && walked.seen.has(el),
        moved: el !== walked.last,
        out: !!within && !el?.closest(within),
        fresh: !!list && !walked.roved.has(list),
        dialogs: document.querySelectorAll('[role="dialog"], [role="alertdialog"]').length,
      };
      walked.last = el;
      if (!(el instanceof HTMLElement) || (stop.again && mode === "tab")) return stop;
      walked.seen.add(el);
      if (list) walked.roved.add(list);
      stop.name = name(el);
      if (!el.matches(":focus-visible")) return stop;

      // The ring's element: the focused one, or the nearest ancestor drawing it (`:has(:focus-visible)`).
      let ringEl: Element | null = el;
      for (; ringEl && ringEl !== document.body; ringEl = ringEl.parentElement) {
        const s = css(ringEl);
        const drawn = s.outlineStyle !== "none" && parseFloat(s.outlineWidth) > 0;
        if (drawn && (ringEl === el || ringEl.matches(":has(:focus-visible)"))) break;
      }
      stop.matched = targets.filter((t) => el.matches(t) || (ringEl !== document.body && !!ringEl?.matches(t)));
      if (!ringEl || ringEl === document.body) {
        stop.ringless = !el.matches('input:not([type="checkbox"]):not([type="radio"]), textarea');
        return stop;
      }
      if (ringEl !== el) stop.name += ` ring on ${name(ringEl)}`;
      const r = ringEl.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) return stop;
      // The outline's outer edge sits outline-offset + outline-width from the border box: out
      // for an outset ring, in for one inset by more than its width.
      const reach = parseFloat(css(ringEl).outlineWidth) + parseFloat(css(ringEl).outlineOffset);
      const ring = {
        left: r.left - reach,
        right: r.right + reach,
        top: r.top - reach,
        bottom: r.bottom + reach,
        width: r.width,
        height: r.height,
      };
      for (const box of clipAncestors(ringEl)) {
        const { sides, shift } = overflow(ring, box, mode);
        if (sides.length) stop.clipped.push(`${name(ringEl)} clipped by ${name(box)}: ${sides.join(", ")}`);
        ring.left += shift.x;
        ring.right += shift.x;
        ring.top += shift.y;
        ring.bottom += shift.y;
      }
      return stop;
    },
    { mode, start, within, targets },
  );
}

type Walk = { failures: string[]; stops: string[]; missed: string[] };

/**
 * Tab from the focus until it lands on a stop checked already, the cycle done (or leaves
 * `within`), roving with the arrow keys into each arrow-key list met until the focus stops
 * moving, and check every stop's ring. `fromFocus` starts at the element focused now instead
 * of pressing Tab first. Returns the clipped and missing rings, the stops checked, and the
 * `targets` (selectors) no stop matched; a walk or a list that reaches TAB_CAP, or an arrow
 * key that opens a dialog, is a failure.
 */
async function walk(
  page: Page,
  where: string,
  {
    targets = [],
    within = null,
    fromFocus = false,
  }: { targets?: string[]; within?: string | null; fromFocus?: boolean } = {},
): Promise<Walk> {
  const failures = new Set<string>();
  const stops: string[] = [];
  const reached = new Set<string>();
  const visit = async (mode: Mode, start = false) => {
    const s = await stopAt(page, mode, start, within, targets);
    if ((s.again && mode === "tab") || !s.moved) return s;
    if (s.name) stops.push(`${mode === "arrow" ? "  ↓ " : ""}${s.name}`);
    for (const t of s.matched) reached.add(t);
    for (const c of s.clipped) failures.add(`${where}: ${c}`);
    if (s.ringless) failures.add(`${where}: no ring on ${s.name}`);
    return s;
  };
  if (!fromFocus) await page.keyboard.press("Tab");
  let s = await visit("tab", true);
  // A Tab from the last stop (an autofocused field, a clicked control) reaches the body first.
  if (!s.name) {
    await page.keyboard.press("Tab");
    s = await visit("tab");
  }
  if (!s.name) failures.add(`${where}: the walk starts on no element`);
  const dialogs = s.dialogs;
  for (let i = 0; ; i++) {
    if (s.fresh)
      for (let j = 0; ; j++) {
        if (j === TAB_CAP) {
          failures.add(`${where}: ${TAB_CAP} ArrowDown stops and the list has not ended`);
          break;
        }
        await page.keyboard.press("ArrowDown");
        const a = await visit("arrow");
        if (a.dialogs > dialogs) failures.add(`${where}: ArrowDown to ${a.name} opened a dialog`);
        // The list's end, or a list that wraps back to an item roved already.
        if (!a.moved || a.again) break;
      }
    if (i === TAB_CAP) {
      failures.add(`${where}: ${TAB_CAP} Tab stops and the cycle is not done`);
      break;
    }
    await page.keyboard.press("Tab");
    s = await visit("tab");
    // A date field's Tab moves between its parts, on one element.
    if (s.moved && (s.again || s.out)) break;
  }
  await test.info().attach(`${where} stops`, { body: stops.join("\n"), contentType: "text/plain" });
  return { failures: [...failures], stops, missed: targets.filter((t) => !reached.has(t)) };
}

/** Walk and expect no clipped ring and every target visited. */
async function expectWhole(page: Page, where: string, opts: Parameters<typeof walk>[2] = {}) {
  const { failures, missed } = await walk(page, where, opts);
  expect(failures).toEqual([]);
  expect(missed, "targets no stop reached").toEqual([]);
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

// Per page, the stops its walk must reach: its row list, its tabs.
const ROW = "[data-row-open]";
const TAB = '[role="tab"]';
const ROUTE_TARGETS: Record<string, string[]> = {
  "/memories": [ROW, TAB],
  "/recall": [TAB],
  "/settings": [TAB],
  "/library": [ROW],
  "/spaces": [ROW],
  "/profile": [ROW],
  "/runs": [ROW],
  "/tasks": [".tq-row", '[data-nav^="group:"]'],
  "/admin/health": [TAB],
  "/admin/activity": [TAB],
};

async function open(page: Page, path: string, mine?: { namespace: string }[]) {
  await installApi(page, {
    memoryRows: 8,
    knowledgeRows: 4,
    runRows: 6,
    ...(mine ? { mine, claims: mine.map((r) => r.namespace) } : {}),
  });
  await page.goto(path);
  await expect(page.locator("main h1").first()).toBeVisible();
  await page.waitForLoadState("networkidle");
  await page.waitForFunction(() => document.getAnimations().length === 0);
}

// The detector itself: a full-width button in a box that clips it, its ring drawn out, then in.
for (const [offset, clipped] of [
  ["2px", true],
  ["-2px", false],
] as const)
  test(`a ring drawn ${offset} out of a full-width button in an overflow: hidden box ${clipped ? "is" : "is not"} clipped`, async ({
    page,
  }) => {
    await page.setContent(`
      <style>button:focus-visible { outline: 2px solid blue; outline-offset: ${offset}; }</style>
      <div style="overflow: hidden; width: 200px; margin: 40px"><button style="width: 100%">Wide</button></div>`);
    const { failures, stops } = await walk(page, "probe");
    expect(stops).toEqual(['button "Wide"']);
    if (clipped)
      expect(failures).toEqual([
        'probe: button "Wide" clipped by div "Wide": left 4.0px, right 4.0px, top 4.0px, bottom 4.0px',
      ]);
    else expect(failures).toEqual([]);
  });

test("a focused button that draws no ring fails the walk; a text field shows focus by its border", async ({ page }) => {
  await page.setContent(`
    <style>button:focus-visible, input:focus-visible { outline: none; }</style>
    <input aria-label="Name"><button>Bare</button>`);
  const { failures, stops } = await walk(page, "probe");
  expect(stops).toEqual(['input "Name"', 'button "Bare"']);
  expect(failures).toEqual(['probe: no ring on button "Bare"']);
});

/** The text fields on the page, outside any dialog, whose look does not change on focus:
 *  the walk passes a ringless field because it shows focus by its border, so the border,
 *  box shadow or outline the field draws, or its wrapper draws (Click UI's field box, the
 *  chip field), must differ focused from blurred. Only what is drawn counts: a border's
 *  colour follows the text colour even with no border, and a box further out (a toolbar
 *  with a :focus-within style) is not the field's own look. Transitions are off while it
 *  compares. */
async function fieldsWithoutFocusLook(page: Page): Promise<{ checked: number; flat: string[] }> {
  return page.evaluate(() => {
    const off = document.createElement("style");
    off.textContent = "*, *::before, *::after { transition: none !important; animation: none !important; }";
    document.head.append(off);
    const drawn = (s: CSSStyleDeclaration) => {
      const sides = (["Top", "Right", "Bottom", "Left"] as const)
        .filter((side) => s[`border${side}Style`] !== "none" && s[`border${side}Width`] !== "0px")
        .map((side) => `${side} ${s[`border${side}Width`]} ${s[`border${side}Color`]}`);
      const outline =
        s.outlineStyle !== "none" && s.outlineWidth !== "0px" ? `${s.outlineWidth} ${s.outlineColor}` : "";
      return [...sides, s.boxShadow, outline].join(" ");
    };
    // The field and the box right around it: Click UI draws the border on its wrapper.
    const look = (el: Element) => [el, el.parentElement].map((e) => (e ? drawn(getComputedStyle(e)) : "")).join(" | ");
    const fields = [
      ...document.querySelectorAll<HTMLElement>(
        'input:not([type="checkbox"]):not([type="radio"]):not([type="hidden"]):not([disabled]), textarea:not([disabled])',
      ),
    ].filter((el) => !el.closest('[role="dialog"], [role="alertdialog"]') && el.getClientRects().length > 0);
    const flat: string[] = [];
    for (const el of fields) {
      el.blur();
      const blurred = look(el);
      el.focus();
      const focused = look(el);
      el.blur();
      if (focused === blurred)
        flat.push(el.getAttribute("aria-label") || el.getAttribute("placeholder") || el.outerHTML.slice(0, 80));
    }
    off.remove();
    return { checked: fields.length, flat };
  });
}

test("a text field whose own look does not change on focus is found; one whose border or wrapper changes is not", async ({
  page,
}) => {
  await page.setContent(`
    <style>
      input { border: 1px solid #ccc; outline: none; }
      #good:focus { border-color: #000; }
      .box input { border: none; }
      .box { border: 1px solid #ccc; }
      .box:focus-within { border-color: #000; }
      .bar:focus-within { color: #00f; box-shadow: 0 0 0 1px #00f; }
    </style>
    <input id="good" aria-label="Good"><input aria-label="Flat">
    <div class="box"><input aria-label="Wrapped"></div>
    <div class="bar"><div><div><input aria-label="In a bar"></div></div></div>`);
  expect(await fieldsWithoutFocusLook(page)).toEqual({ checked: 4, flat: ["Flat", "In a bar"] });
});

test.describe("text fields", () => {
  test.use({ reducedMotion: "reduce" });
  for (const path of ROUTES) {
    test(`${path}: every text field changes its border or shadow on focus`, async ({ page }) => {
      await open(page, path);
      const { checked, flat } = await fieldsWithoutFocusLook(page);
      test.info().annotations.push({ type: "text_fields", description: String(checked) });
      expect(flat).toEqual([]);
    });
  }
});

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
        await expectWhole(page, path, { targets: ROUTE_TARGETS[path] });
      });
    }

    test("Spaces with a member's guide summary: every focus ring is drawn whole", async ({ page }) => {
      // A member of team-002, the last row, which carries a guide summary: its link sits
      // above the row's open button and truncates its own text. The walk arrows down the
      // rows and tabs into the last one's controls.
      await installApi(page, {
        browseRows: 3,
        mine: [
          { namespace: NAMESPACE, role: "writer", status: "active", visibility: "private", kind: "memory" },
          { namespace: "team-002", role: "writer", status: "active", visibility: "public", kind: "memory" },
        ],
        claims: [NAMESPACE, "team-002"],
      });
      await page.goto("/spaces");
      await expect(page.getByRole("button", { name: /^Guide: One runbook per service/ })).toBeVisible();
      await page.waitForLoadState("networkidle");
      await expectWhole(page, "/spaces guide", { targets: [ROW, "[data-guide-link]"] });
    });

    test("the token form with a collection to tick: every focus ring is drawn whole", async ({ page }) => {
      // A direct grant on a collection with a long title: the title's tooltip target is a stop.
      await installApi(page, {
        mine: [
          { namespace: NAMESPACE, role: "writer", status: "active", visibility: "private", kind: "memory" },
          {
            namespace: "col-research",
            role: "writer",
            status: "active",
            visibility: "private",
            kind: "collection",
            title: `Research ${"notes and papers ".repeat(6)}`,
          },
        ],
        claims: [NAMESPACE, "col-research"],
      });
      await page.goto("/settings");
      await page.getByRole("radio", { name: "Only what you choose" }).click();
      await expect(page.getByRole("checkbox", { name: /^Read the collection Research/ })).toBeVisible();
      // A long title never squeezes out the place, which tells same-named collections apart.
      const place = await page.locator("[data-collection-place]").first().boundingBox();
      expect(place?.width ?? 0).toBeGreaterThan(20);
      await expectWhole(page, "/settings collections", { targets: ['fieldset [role="note"]'] });
    });

    test("Sharing with a long collection title: the card holds it and its ring is drawn whole", async ({ page }) => {
      const title = `Research ${"notes and papers ".repeat(10)}`;
      await installApi(page, {
        mine: [
          { namespace: NAMESPACE, role: "writer", status: "active", visibility: "private", kind: "memory" },
          {
            namespace: "col-research",
            role: "writer",
            status: "active",
            visibility: "private",
            kind: "collection",
            title,
          },
        ],
        claims: [NAMESPACE, "col-research"],
        sharingNamespaces: [{ namespace: "col-research", role: "writer" }],
      });
      await page.goto("/flow");
      const open = page.getByRole("button", { name: /^Research notes/ });
      await expect(open).toBeVisible();
      // The title's link and the role badge after it stay inside their card.
      const card = await open.locator("xpath=ancestor::div[contains(@class,'rounded-xl')][1]").boundingBox();
      const badge = await open.locator("xpath=following-sibling::*[1]").boundingBox();
      if (!card || !badge) throw new Error("card not drawn");
      expect(badge.x + badge.width).toBeLessThanOrEqual(card.x + card.width);
      await expectWhole(page, "/flow collections", { targets: ["section button"] });
    });

    test("the Explorer with a walk: every focus ring is drawn whole", async ({ page }) => {
      await open(page, `/graph?seed=${SMALL_SEED}`);
      await expect(page.getByRole("tree", { name: "Walk outline" }).getByRole("treeitem").first()).toBeVisible();
      await expectWhole(page, "/graph walk", { targets: ['.explorer-outline [role="treeitem"]'] });
    });

    test("a task lane longer than it shows: every focus ring is drawn whole", async ({ page }) => {
      await open(page, "/tasks");
      // 24 to-do tasks: the lane shows 20 and a "Show all" button.
      await page.route("**/v1/task.list", (route) =>
        route.fulfill({ json: { tasks: Array.from({ length: 24 }, (_, i) => task(i + 1)), truncated: false } }),
      );
      await page.reload();
      await expect(page.getByRole("button", { name: "Show all 24" })).toBeVisible();
      await expectWhole(page, "/tasks long lane", {
        targets: ['[data-nav="more:todo"]', '[data-nav="group:todo"]', ".tq-row"],
      });
    });

    test("the tasks list with a lane folded: every focus ring is drawn whole", async ({ page }) => {
      await open(page, "/tasks");
      const header = page.getByRole("grid").getByRole("button", { name: /^Triage/ });
      await header.click();
      await expect(header).toHaveAttribute("aria-expanded", "false");
      await expectWhole(page, "/tasks folded", {
        targets: [
          '[data-nav="group:triage"][aria-expanded="false"]',
          '[data-nav^="group:"][aria-expanded="true"]',
          ".tq-row",
        ],
      });
    });

    // A phone shows the list only: a board's columns do not fit it.
    if (!phone)
      test("the tasks board: every focus ring is drawn whole", async ({ page }) => {
        await open(page, "/tasks");
        await page.getByRole("button", { name: "Board", exact: true }).click();
        await expect(page.getByRole("region", { name: /^To do/ })).toBeVisible();
        await expectWhole(page, "/tasks board", { targets: [".tq-card"] });
      });

    test("the open namespace picker: every focus ring is drawn whole", async ({ page }) => {
      await open(page, "/memories", MINE);
      await page.getByRole("button", { name: /^Namespace: / }).click();
      const dialog = page.getByRole("dialog", { name: "Namespaces" });
      await dialog.getByRole("button", { name: /^Repositories/ }).click();
      await expect(dialog.getByRole("button", { name: "Show all 10" })).toBeVisible();
      // Into the list from its search field, at its tab stop (the group just opened), and Home
      // to its first option, so the arrows rove over every item.
      await dialog.getByLabel("Search namespaces").focus();
      await page.keyboard.press("ArrowDown");
      await expect(dialog.getByRole("button", { name: /^Repositories/ })).toBeFocused();
      await page.keyboard.press("Home");
      await expectWhole(page, "namespace picker", {
        fromFocus: true,
        within: '[role="dialog"][aria-label="Namespaces"]',
        targets: [
          '[data-nav^="ns:"]',
          '[data-nav="group:code"]',
          '[data-nav="group:tracker"]',
          '[data-nav="more:code"]',
        ],
      });
    });

    // Two lists whose last control is not an arrow-key item, at the list's clipped bottom edge.
    const owned = { ...ns(NAMESPACE, "memory"), role: "admin" };
    const pending = { ...ns("team-notes", "memory"), role: "viewer", status: "pending", pending_role: "writer" };
    for (const [list, mine, last] of [
      ["ends on a pending request", [owned, pending], "pending writer"],
      ["has nothing joined", [owned], "Find public namespaces"],
    ] as const)
      test(`a namespace picker that ${list}: every focus ring is drawn whole`, async ({ page }) => {
        await open(page, "/memories", [...mine]);
        await page.getByRole("button", { name: /^Namespace: / }).click();
        await expect(page.getByRole("dialog", { name: "Namespaces" }).getByText(last)).toBeVisible();
        // The search field takes the focus on open; the walk starts there.
        const { failures, stops } = await walk(page, `picker ${list}`, {
          fromFocus: true,
          within: '[role="dialog"][aria-label="Namespaces"]',
        });
        expect(failures).toEqual([]);
        expect(stops.filter((s) => s.includes(last))).toHaveLength(1);
      });

    test("the open memory inspector: every focus ring is drawn whole", async ({ page }) => {
      await open(page, "/memories");
      await page.locator("[data-memory-open]").first().click();
      await expect(page.getByRole("dialog")).toBeVisible();
      await page.waitForLoadState("networkidle");
      await expectWhole(page, "memory inspector", { targets: ['[role="dialog"] button'] });
    });

    test("the open Library text preview: every focus ring is drawn whole", async ({ page }) => {
      await open(page, "/library");
      await page.route("**/v1/knowledge.download**", (route) =>
        route.fulfill({ status: 200, contentType: "text/plain; charset=utf-8", body: "Drain the lane.\n".repeat(400) }),
      );
      await page
        .getByRole("button", { name: /^Preview / })
        .first()
        .click();
      await expect(page.getByRole("dialog").locator("pre")).toBeVisible();
      await expectWhole(page, "library text preview", {
        targets: ['[role="dialog"] button', '[role="dialog"] section[tabindex="0"]'],
      });
    });

    test("the open task detail: every focus ring is drawn whole", async ({ page }) => {
      await open(page, "/tasks");
      await page.getByRole("row", { name: /^Task 1:/ }).click();
      await expect(page.getByRole("complementary", { name: "Task detail" }).or(page.getByRole("dialog"))).toBeVisible();
      await expectWhole(page, "task detail", { targets: [".tq-panel button"] });
    });
  });
}
