// A performance budget on the dashboard's boot.
//
// The API is answered locally, so this measures what the app itself costs
// — script, boot order, first paint — not the server. The CPU is slowed 4x
// to stand in for a laptop; the network is the preview server on localhost.
import { expect, test } from "@playwright/test";
import { codeReposFixture, installApi, stringArray, warmUp } from "./fixtures";

test.beforeAll(({ browser }, testInfo) => warmUp(browser, testInfo));

const LCP_BUDGET_MS = 1500;

// What a page of a hundred rows costs the browser — DOM nodes, and the main-thread time
// in long tasks after the rows land.
const ROWS = 100;
// The node count does not vary between runs, so each budget is the count this spec
// measured (its `dom_nodes` annotation: 3,150 for Memories, 3,413 for Library) plus a small
// margin: about two more nodes per row trip it. A Library row's More button is always
// drawn, since its menu shows without a hover.
const NODE_BUDGET = { memories: 3400, library: 3600 };
// Long tasks vary with the runner, so their budget is about four times what the pages cost
// at 4x CPU.
const LONG_TASK_BUDGET_MS = 2500;
// The shell alone — a page with a handful of rows — costs about 110 ms of long tasks at
// 4x on a production build; the budget is generous for CI's slower runners and trips when
// a closed dialog, a forced layout or a per-row formatter creeps back into every boot.
const SHELL_LONG_TASK_BUDGET_MS = 1200;
const CLS_BUDGET = 0.1;

test("boot on /memories: LCP and layout shift within budget at 4x CPU, fonts preloaded, boot reads leave together", async ({
  page,
}) => {
  await page.addInitScript(() => {
    window.__lcp = 0;
    window.__ls = { entries: 0, value: 0 };
    try {
      new PerformanceObserver((l) => {
        for (const e of l.getEntries()) window.__lcp = e.startTime;
      }).observe({ type: "largest-contentful-paint", buffered: true });
      // lib.dom has no LayoutShift entry type; a layout-shift entry carries `value`.
      new PerformanceObserver((l) => {
        for (const e of l.getEntries() as unknown as Array<{ value: number; hadRecentInput: boolean }>) {
          if (e.hadRecentInput) continue;
          window.__ls.entries += 1;
          window.__ls.value += e.value;
        }
      }).observe({ type: "layout-shift", buffered: true });
    } catch {
      /* not supported */
    }
  });
  await installApi(page);
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Emulation.setCPUThrottlingRate", { rate: 4 });

  const requested: string[] = [];
  page.on("request", (r) => {
    const u = new URL(r.url());
    if (u.pathname.startsWith("/v1/")) requested.push(u.pathname.replace("/v1/", ""));
  });

  await page.goto("/memories", { waitUntil: "domcontentloaded" });
  await expect(page.locator("main").getByRole("heading", { name: "Memories" }).first()).toBeVisible();
  await page.waitForTimeout(1500); // let LCP settle

  const html = await page.content();
  const preloads = html.match(/<link rel="preload" as="font"[^>]*>/g) ?? [];
  expect(
    preloads.some((l) => /inter-latin-wght-normal/.test(l)),
    "Inter latin is preloaded",
  ).toBe(true);
  expect(
    preloads.some((l) => /inconsolata-latin-wght-normal/.test(l)),
    "Inconsolata latin is preloaded",
  ).toBe(true);

  // The membership read is not chained behind whoami — with the token's claims decoded
  // locally it is issued in the same tick.
  const iWho = requested.indexOf("whoami");
  const iMine = requested.indexOf("namespace.list_mine");
  expect(iWho, "whoami requested").toBeGreaterThanOrEqual(0);
  expect(iMine, "list_mine requested").toBeGreaterThanOrEqual(0);
  expect(requested.filter((p) => p === "namespace.list_mine")).toHaveLength(1);

  const lcp = await page.evaluate(() => window.__lcp);
  test.info().annotations.push({ type: "lcp_ms", description: String(Math.round(lcp)) });
  expect(lcp, `LCP ${Math.round(lcp)} ms over the ${LCP_BUDGET_MS} ms budget at 4x CPU`).toBeLessThanOrEqual(
    LCP_BUDGET_MS,
  );
  // The fonts swap in after first paint; their fallbacks must not move the page (CLS "good" is 0.1).
  const shift = await page.evaluate(() => window.__ls.value);
  expect(shift, `layout shift ${shift.toFixed(3)} over the ${CLS_BUDGET} budget`).toBeLessThanOrEqual(CLS_BUDGET);
  await cdp.detach();
});

async function renderBudget(
  page: import("@playwright/test").Page,
  path: string,
  heading: string,
  rowText: RegExp,
  rows: number,
) {
  await page.addInitScript(() => {
    window.__lt = [];
    try {
      new PerformanceObserver((l) => {
        for (const e of l.getEntries()) window.__lt.push(e.duration);
      }).observe({ type: "longtask", buffered: true });
    } catch {
      /* not supported */
    }
  });
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Emulation.setCPUThrottlingRate", { rate: 4 });
  await page.goto(path, { waitUntil: "domcontentloaded" });
  await expect(page.locator("main").getByRole("heading", { name: heading }).first()).toBeVisible();
  await expect(page.locator("main").getByText(rowText).first()).toBeVisible();
  await page.waitForTimeout(1500); // the rows' own effects (previews, badges) settle
  const shown = await page.locator("main").getByText(rowText).count();
  const nodes = await page.evaluate(() => document.querySelectorAll("*").length);
  const longTaskMs = await page.evaluate(() => window.__lt.reduce((a, b) => a + b, 0));
  await cdp.detach();
  test
    .info()
    .annotations.push(
      { type: "dom_nodes", description: String(nodes) },
      { type: "long_task_ms", description: String(Math.round(longTaskMs)) },
    );
  return { shown, nodes, longTaskMs, rows };
}

test("Memories with 100 rows: DOM nodes and long tasks within budget at 4x CPU", async ({ page }) => {
  // The largest page a reader can pick: the worst case the budget is for.
  await page.addInitScript(() => localStorage.setItem("loom_page_size_memories_v1", "100"));
  await installApi(page, { memoryRows: ROWS });
  const m = await renderBudget(page, "/memories", "Memories", /^alerts\/2026-09-19\/\d{4}$/, ROWS);
  expect(m.shown, "every row is in the DOM (no virtualisation — containment instead)").toBe(ROWS);
  expect(
    m.nodes,
    `${m.nodes} DOM nodes over the ${NODE_BUDGET.memories} budget for ${ROWS} memories`,
  ).toBeLessThanOrEqual(NODE_BUDGET.memories);
  expect(
    m.longTaskMs,
    `${Math.round(m.longTaskMs)} ms of long tasks over the ${LONG_TASK_BUDGET_MS} ms budget`,
  ).toBeLessThanOrEqual(LONG_TASK_BUDGET_MS);
});

test("Library with 100 rows: DOM nodes and long tasks within budget at 4x CPU", async ({ page }) => {
  await installApi(page, { knowledgeRows: ROWS });
  const m = await renderBudget(page, "/library", "Library", /^Runbook \d+: lane \d recovery$/, ROWS);
  expect(m.shown, "every row is in the DOM").toBe(ROWS);
  expect(
    m.nodes,
    `${m.nodes} DOM nodes over the ${NODE_BUDGET.library} budget for ${ROWS} documents`,
  ).toBeLessThanOrEqual(NODE_BUDGET.library);
  expect(
    m.longTaskMs,
    `${Math.round(m.longTaskMs)} ms of long tasks over the ${LONG_TASK_BUDGET_MS} ms budget`,
  ).toBeLessThanOrEqual(LONG_TASK_BUDGET_MS);
});

// Opening a memory card issues memory.get and nothing else until a section is opened.
// Seven reads and about 118 MB of ClickHouse per open (history, related, provenance,
// lineage, a 500-row list for the tag vocabulary) would be the cost of reading every
// section; this keeps them off the open.
test("opening a card is one read until a section is opened", async ({ page }) => {
  await installApi(page, { memoryRows: 12 });
  const requested: string[] = [];
  page.on("request", (r) => {
    const u = new URL(r.url());
    if (u.pathname.startsWith("/v1/")) requested.push(u.pathname.replace("/v1/", ""));
  });
  await page.goto("/memories", { waitUntil: "domcontentloaded" });
  const first = page.locator("[data-memory-key]").first();
  await expect(first).toBeVisible();
  await page.waitForTimeout(500);
  const before = requested.length;
  await first.click();
  const drawer = page.getByRole("dialog");
  await expect(drawer.getByRole("button", { name: /History/ })).toBeVisible();
  await page.waitForTimeout(800);
  const onOpen = requested.slice(before);
  const reads = (op: string) => onOpen.filter((p) => p === op).length;
  expect(reads("memory.get"), `memory.get once on open (saw ${onOpen.join(", ")})`).toBe(1);
  for (const op of [
    "memory.history",
    "memory.related",
    "memory.provenance",
    "memory.lineage",
    "namespace.taxonomy",
    "memory.list",
  ]) {
    expect(reads(op), `${op} is not read on open (saw ${onOpen.join(", ")})`).toBe(0);
  }
  // conflicts.list (the keys) is the badge's role-gated read; at most one.
  expect(reads("conflicts.list")).toBeLessThanOrEqual(1);
  const beforeHistory = requested.length;
  await drawer.getByRole("button", { name: /History/ }).click();
  await expect.poll(() => requested.slice(beforeHistory).filter((p) => p === "memory.history").length).toBe(1);
  expect(requested.slice(beforeHistory).filter((p) => p === "memory.related").length).toBe(0);
});

// A keystroke in the Memories box asks for the legs the page can show, and a retype aborts the stale search instead of racing it.
test("the Memories search asks for memory legs only and aborts a stale search", async ({ page }) => {
  const calls = await installApi(page, { memoryRows: 12, searchDelayMs: 1500 });
  const aborted: string[] = [];
  page.on("requestfailed", (r) => {
    const u = new URL(r.url());
    if (u.pathname === "/v1/memory.search") aborted.push(r.failure()?.errorText ?? "failed");
  });
  await page.goto("/memories", { waitUntil: "domcontentloaded" });
  await expect(page.locator("[data-memory-key]").first()).toBeVisible();
  const box = page.getByLabel("Search memories");
  await box.fill("e"); // one character is not a query
  await page.waitForTimeout(400);
  expect(calls.search).toHaveLength(0);
  await box.fill("exporter");
  await expect.poll(() => calls.search.length).toBe(1);
  expect(calls.search[0]).toMatchObject({ query: "exporter", legs: ["memory"], top_k: 50 });
  // retype while the first answer is still held: the first search is aborted
  await box.fill("exporter lane");
  await expect.poll(() => calls.search.length).toBe(2);
  await expect.poll(() => aborted.length).toBe(1);
  expect(aborted[0]).toMatch(/ABORTED/i);
  expect(calls.search[1]).toMatchObject({ query: "exporter lane", legs: ["memory"] });
});

// The Memories page asks for the twelve fields its cards read and the preview length they
// show (CARD_VALUE_CHARS); the list answers nothing else.
test("the Memories list asks for the twelve fields the page reads and a 320-character preview", async ({ page }) => {
  const calls = await installApi(page, { memoryRows: 12 });
  await page.goto("/memories", { waitUntil: "domcontentloaded" });
  await expect(page.locator("[data-memory-key]").first()).toBeVisible();
  await expect.poll(() => calls.list.length).toBeGreaterThanOrEqual(1);
  const body = calls.list[0];
  expect(body.preview_chars, "preview_chars rides on the list request").toBe(320);
  const fields = stringArray(body.fields) ?? [];
  expect(fields.length > 0, "fields rides on the list request").toBe(true);
  for (const f of ["memory_key", "value", "tags", "agent", "created_at", "pinned", "verification"]) {
    expect(fields, `the page asks for ${f}`).toContain(f);
  }
  expect(fields.length, "twelve fields, no more").toBe(12);
  for (const f of [
    "entities",
    "source_trace",
    "evidence_count",
    "last_recalled_at",
    "valid_at",
    "updated_at",
    "version",
    "expires_at",
    "status",
    "value_truncated",
  ]) {
    expect(fields, `the page does not ask for ${f}`).not.toContain(f);
  }
});

// The Profile page builds its sections from the list; a row is read only when the list says its value was cut.
test("the Profile page reads its sections once when they fit the preview", async ({ page }) => {
  const calls = await installApi(page, { profileRows: 3 });
  await page.goto("/profile", { waitUntil: "domcontentloaded" });
  await expect(page.getByLabel("Select section role")).toBeVisible();
  await page.waitForTimeout(600);
  const lists = calls.memory.filter((p) => p === "/v1/memory.list").length;
  const gets = calls.memory.filter((p) => p === "/v1/memory.get").length;
  expect(lists, `memory.list once on load (saw ${calls.memory.join(", ")})`).toBe(1);
  expect(gets, `no memory.get behind the list (saw ${calls.memory.join(", ")})`).toBe(0);
});

// A fresh document is at the top; the shell's scroll reset runs only on a view change,
// never on the boot (a scrollTo there forced a layout: 42 ms at 4x on three routes).
test("the shell issues no scrollTo on a fresh boot", async ({ page }) => {
  await installApi(page);
  await page.addInitScript(() => {
    window.__scrolls = 0;
    const orig = Element.prototype.scrollTo;
    Element.prototype.scrollTo = function (this: Element, ...args: unknown[]) {
      window.__scrolls++;
      // Forward whichever overload the caller used; apply() can't pick one for unknown[].
      Reflect.apply(orig, this, args);
    };
  });
  await page.goto("/runs", { waitUntil: "domcontentloaded" });
  await expect(page.locator("main").locator("h1, h2").first()).toBeVisible();
  await page.waitForTimeout(600);
  expect(await page.evaluate(() => window.__scrolls), "no scrollTo before or after first paint on a fresh boot").toBe(
    0,
  );
});

// The Library page asks for the eleven fields its rows read; the 24 kB content_preview no row renders stays on the server.
test("the Library list asks for the fields its rows read and no content preview", async ({ page }) => {
  const calls = await installApi(page, { knowledgeRows: 12 });
  await page.goto("/library", { waitUntil: "domcontentloaded" });
  await expect(page.getByText("Runbook 0: lane 0 recovery")).toBeVisible();
  await expect.poll(() => calls.knowledge.length).toBeGreaterThanOrEqual(1);
  const fields = stringArray(calls.knowledge[0].fields) ?? [];
  expect(fields.length > 0, "fields rides on the list request").toBe(true);
  for (const f of ["source_uri", "title", "description", "tags", "ingested_at", "chunk_count"]) {
    expect(fields, `the page asks for ${f}`).toContain(f);
  }
  expect(fields, "the page does not ask for content_preview").not.toContain("content_preview");
});

// An open tab costs nothing. Idle after settle, the Memories tab polls its watermark (8
// s, 111 B) and nothing else runs; the Repositories tab issues no read at all.
test("an idle Memories tab issues only its watermark poll and runs no long task", async ({ page }) => {
  await installApi(page, { memoryRows: 12 });
  await page.addInitScript(() => {
    window.__lt = [];
    try {
      new PerformanceObserver((l) => {
        for (const e of l.getEntries()) window.__lt.push(e.duration);
      }).observe({ type: "longtask", buffered: true });
    } catch {
      /* not supported */
    }
  });
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Emulation.setCPUThrottlingRate", { rate: 4 });
  await page.goto("/memories", { waitUntil: "domcontentloaded" });
  await expect(page.locator("[data-memory-key]").first()).toBeVisible();
  await page.waitForTimeout(2500);
  const calls: string[] = [];
  page.on("request", (r) => {
    const u = new URL(r.url());
    if (u.pathname.startsWith("/v1/") && u.pathname !== "/v1/ui.vitals") calls.push(u.pathname.replace("/v1/", ""));
  });
  const ltBefore = await page.evaluate(() => window.__lt.length);
  await page.waitForTimeout(20_000);
  const lt = await page.evaluate(() => window.__lt.slice(0));
  await cdp.detach();
  const idleLong = lt.slice(ltBefore).filter((d) => d > 50);
  expect(
    calls.filter((c) => c !== "memory.list"),
    `only the watermark poll runs idle (saw ${calls.join(", ")})`,
  ).toEqual([]);
  expect(calls.length, `at most three polls in 20 s (saw ${calls.length})`).toBeLessThanOrEqual(3);
  expect(idleLong, `no long task over 50 ms while idle (saw ${idleLong.map(Math.round).join(", ")})`).toEqual([]);
});

// The Activity page asks for the four fields its rows read.
test("the Activity list asks for the fields its rows read", async ({ page }) => {
  const calls = await installApi(page);
  await page.goto("/admin/activity", { waitUntil: "domcontentloaded" });
  await expect.poll(() => calls.audit.length).toBeGreaterThanOrEqual(1);
  expect(calls.audit[0].fields).toEqual(["ts", "event_type", "actor", "subject"]);
});

// Interactions are counted. On the 100-row fixture at 4x, the longest event-timing entry
// of each interaction stays under the INP "good" line (typically 40–120 ms with a hundred
// cards).
const INP_BUDGET_MS = 200;

/** Records every event-timing entry from the first script on, and slows the CPU 4x. */
async function watchInteractions(page: import("@playwright/test").Page) {
  await page.addInitScript(() => {
    window.__ev = [];
    try {
      // The cast below: lib.dom's PerformanceObserverInit lacks Event Timing's durationThreshold.
      new PerformanceObserver((l) => {
        for (const e of l.getEntries()) window.__ev.push(e.duration);
      }).observe({ type: "event", buffered: true, durationThreshold: 16 } as PerformanceObserverInit);
    } catch {
      /* not supported */
    }
  });
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Emulation.setCPUThrottlingRate", { rate: 4 });
  return cdp;
}

/** Runs `fn`, then fails when its longest interaction is over the INP budget. */
async function worstInteraction(page: import("@playwright/test").Page, label: string, fn: () => Promise<void>) {
  await page.evaluate(() => {
    window.__ev = [];
  });
  await fn();
  await page.waitForTimeout(1500);
  const ev = await page.evaluate(() => window.__ev);
  const w = ev.length ? Math.max(...ev) : 0;
  test.info().annotations.push({ type: `inp_${label}`, description: String(Math.round(w)) });
  expect(w, `${label}: longest interaction ${Math.round(w)} ms over ${INP_BUDGET_MS}`).toBeLessThanOrEqual(
    INP_BUDGET_MS,
  );
}

/** Types `text` into the field a key at a time, as a reader does. */
async function typeInto(field: import("@playwright/test").Locator, text: string) {
  await field.click();
  await field.pressSequentially(text, { delay: 120 });
}

test("Memories interactions: typing, clearing, opening and closing a card stay under the INP budget at 4x", async ({
  page,
}) => {
  await installApi(page, { memoryRows: 100 });
  const cdp = await watchInteractions(page);
  await page.goto("/memories", { waitUntil: "domcontentloaded" });
  await expect(page.locator("[data-memory-key]").first()).toBeVisible();
  await page.waitForTimeout(2000);
  const box = page.getByLabel("Search memories");
  await worstInteraction(page, "type", () => typeInto(box, "exporter"));
  await worstInteraction(page, "clear", () => box.fill(""));
  await worstInteraction(page, "open", async () => {
    await page.locator("[data-memory-key]").first().click();
    await expect(page.getByRole("dialog")).toBeVisible();
  });
  await worstInteraction(page, "close", () => page.keyboard.press("Escape"));
  await cdp.detach();
});

// Typing in a list's search box renders the box, not every row under it: the rows render
// again only when the settled search changes what they show.
test("Runs: typing and clearing the search stay under the INP budget at 4x with 100 runs", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("loom_page_size_runs_v1", "100"));
  await installApi(page, { runRows: 100 });
  const cdp = await watchInteractions(page);
  await page.goto("/runs", { waitUntil: "domcontentloaded" });
  await expect(page.locator("main").getByText("run-0099")).toBeVisible();
  await page.waitForTimeout(2000);
  const box = page.getByLabel("Search runs");
  await worstInteraction(page, "type", () => typeInto(box, "triage lane"));
  await worstInteraction(page, "clear", () => box.fill(""));
  await cdp.detach();
});

test("Library: typing and clearing the search stay under the INP budget at 4x with 100 documents", async ({ page }) => {
  await installApi(page, { knowledgeRows: 100 });
  const cdp = await watchInteractions(page);
  await page.goto("/library", { waitUntil: "domcontentloaded" });
  await expect(page.locator("main").getByText("Runbook 99: lane 4 recovery")).toBeVisible();
  await page.waitForTimeout(2000);
  const box = page.getByLabel("Search library");
  await worstInteraction(page, "type", () => typeInto(box, "lane 4"));
  await worstInteraction(page, "clear", () => box.fill(""));
  await cdp.detach();
});

test("Conversations: typing, searching and clearing stay under the INP budget at 4x with a page of threads", async ({
  page,
}) => {
  await installApi(page, { threadRows: 30 });
  const cdp = await watchInteractions(page);
  await page.goto("/conversations", { waitUntil: "domcontentloaded" });
  const threads = page.locator("[data-thread-row]");
  await expect(threads).toHaveCount(30);
  await page.waitForTimeout(2000);
  const box = page.getByLabel("Search conversation transcripts");
  await worstInteraction(page, "type", () => typeInto(box, "exporter backlog"));
  // Enter renders the hits: one row per thread, up to three turns each.
  await worstInteraction(page, "search", async () => {
    await box.press("Enter");
    await expect(page.getByText("replay 2 drained it", { exact: false }).first()).toBeVisible();
  });
  await worstInteraction(page, "clear", async () => {
    await box.fill("");
    await expect(threads).toHaveCount(30);
  });
  await cdp.detach();
});

test("Activity: typing and clearing the filter stay under the INP budget at 4x with 100 events", async ({ page }) => {
  await installApi(page, { auditRows: 100 });
  const cdp = await watchInteractions(page);
  await page.goto("/admin/activity", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("table", { name: "Audit events" }).locator("tbody tr")).toHaveCount(100);
  await page.waitForTimeout(2000);
  const box = page.getByRole("textbox", { name: "Filter audit events" });
  await worstInteraction(page, "type", () => typeInto(box, "alice"));
  await worstInteraction(page, "clear", () => box.fill(""));
  await cdp.detach();
});

test("Spaces: typing and clearing the search stay under the INP budget at 4x with 200 namespaces", async ({ page }) => {
  await installApi(page, { browseRows: 200 });
  const cdp = await watchInteractions(page);
  await page.goto("/spaces", { waitUntil: "domcontentloaded" });
  await expect(page.locator("main").getByText("team-199", { exact: true })).toBeVisible();
  await page.waitForTimeout(2000);
  const box = page.getByLabel("Search namespaces");
  await worstInteraction(page, "type", () => typeInto(box, "team-1"));
  await worstInteraction(page, "clear", () => box.fill(""));
  await cdp.detach();
});

// The Recall Lab scores as the text settles and has no Enter of its own: typing and
// clearing are its interactions over the table.
test("Recall Lab: typing and clearing over a table of 100 scores stay under the INP budget at 4x", async ({ page }) => {
  await installApi(page, { memoryRows: 100 });
  const cdp = await watchInteractions(page);
  await page.goto("/recall", { waitUntil: "domcontentloaded" });
  const box = page.getByLabel("Recall query");
  await box.fill("alerts");
  await expect(page.getByRole("table", { name: "Recall scores" }).locator("tbody tr")).toHaveCount(100);
  await page.waitForTimeout(2000);
  await worstInteraction(page, "type", () => typeInto(box, " lane"));
  await worstInteraction(page, "clear", () => box.fill(""));
  await cdp.detach();
});

test("Repositories: typing and clearing the filter stay under the INP budget at 4x with 100 repositories", async ({
  page,
}) => {
  await page.addInitScript(() => localStorage.setItem("loom_page_size_repositories_v1", "100"));
  const one = codeReposFixture()[0];
  await installApi(page, {
    codeRepos: Array.from({ length: 100 }, (_, i) => ({
      ...one,
      namespace: `code-repo-${i}`,
      repo: `acme/repo-${String(i).padStart(2, "0")}`,
    })),
  });
  const cdp = await watchInteractions(page);
  await page.goto("/repositories", { waitUntil: "domcontentloaded" });
  await expect(page.locator("main").getByText("acme/repo-99")).toBeVisible();
  await page.waitForTimeout(2000);
  const box = page.getByLabel("Filter repositories");
  await worstInteraction(page, "type", () => typeInto(box, "acme/repo"));
  await worstInteraction(page, "clear", () => box.fill(""));
  await cdp.detach();
});

test("Tasks: typing and clearing the search stay under the INP budget at 4x with 80 open tasks", async ({ page }) => {
  // Twenty in each open lane, all of them shown (a lane draws 20 before its Show all).
  await installApi(page, { taskRows: 80 });
  const cdp = await watchInteractions(page);
  await page.goto("/tasks", { waitUntil: "domcontentloaded" });
  await expect(page.locator(".tq-row")).toHaveCount(80);
  await page.waitForTimeout(2000);
  const box = page.getByLabel("Search tasks");
  await worstInteraction(page, "type", () => typeInto(box, "exporter"));
  await worstInteraction(page, "clear", () => box.fill(""));
  await cdp.detach();
});

test("Profile: typing and clearing the search stay under the INP budget at 4x with 100 sections", async ({ page }) => {
  await installApi(page, { profileRows: 100 });
  const cdp = await watchInteractions(page);
  await page.goto("/profile", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("checkbox", { name: "Select section role-6", exact: true })).toBeVisible();
  await page.waitForTimeout(2000);
  const box = page.getByLabel("Search profile sections");
  await worstInteraction(page, "type", () => typeInto(box, "team"));
  await worstInteraction(page, "clear", () => box.fill(""));
  await cdp.detach();
});

test("the shell alone: long tasks within budget at 4x CPU on a light page", async ({ page }) => {
  await installApi(page);
  await page.addInitScript(() => {
    window.__lt = [];
    try {
      new PerformanceObserver((l) => {
        for (const e of l.getEntries()) window.__lt.push(e.duration);
      }).observe({ type: "longtask", buffered: true });
    } catch {
      /* not supported */
    }
  });
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Emulation.setCPUThrottlingRate", { rate: 4 });
  await page.goto("/runs", { waitUntil: "domcontentloaded" });
  await expect(page.locator("main").getByRole("heading", { name: "Runs" }).first()).toBeVisible();
  await page.waitForTimeout(1500);
  const longTaskMs = await page.evaluate(() => window.__lt.reduce((a, b) => a + b, 0));
  await cdp.detach();
  test.info().annotations.push({ type: "shell_long_task_ms", description: String(Math.round(longTaskMs)) });
  expect(
    longTaskMs,
    `${Math.round(longTaskMs)} ms of long tasks over the ${SHELL_LONG_TASK_BUDGET_MS} ms shell budget`,
  ).toBeLessThanOrEqual(SHELL_LONG_TASK_BUDGET_MS);
});
