import { expect, test } from "@playwright/test";
import { installApi, warmUp } from "./fixtures";

// Every route has a budget. One parametrized test over the twenty-two dashboard routes with
// the default fixtures at 4x CPU: the page paints (a heading in <main>), its largest
// contentful paint and its long tasks stay under budget, and its reads stay one per
// question. The budgets are set for CI's slower runners — they catch a doubling, not a
// drift (the drift shows in the ui.vitals rows).

test.beforeAll(({ browser }, testInfo) => warmUp(browser, testInfo));

const LCP_BUDGET_MS = 2000;
const LONG_TASK_BUDGET_MS = 1500;
// Every route reads at most 12 times, the sidebar's Auto-resolved audit.list included
// (Settings, Conversations, Tasks, Review queue and Usage). The budget is that ceiling
// plus one read of headroom, so a second new read on any route fails here.
const CALL_BUDGET = 13;

const ROUTES: Array<[string, string]> = [
  ["Memories", "/memories"],
  ["Recall", "/recall"],
  ["Settings", "/settings"],
  ["Repositories", "/repositories"],
  ["Flow", "/flow"],
  ["Library", "/library"],
  ["Conversations", "/conversations"],
  ["Graph", "/graph"],
  ["Assistant", "/assistant"],
  ["Spaces", "/spaces"],
  ["Profile", "/profile"],
  ["Runs", "/runs"],
  ["Tasks", "/tasks"],
  ["Review queue", "/admin/review_queue"],
  ["Health", "/admin/health"],
  ["Guardrails", "/admin/guardrails"],
  ["Policies", "/admin/policies"],
  ["Activity", "/admin/activity"],
  ["Usage", "/admin/usage"],
  ["Agents", "/admin/agents"],
  ["Users", "/admin/users"],
];

for (const [name, path] of ROUTES) {
  test(`${name}: paints within budget at 4x CPU with one read per question`, async ({ page }) => {
    await page.addInitScript(() => {
      window.__lcp = 0;
      window.__lt = [];
      try {
        new PerformanceObserver((l) => {
          for (const e of l.getEntries()) window.__lcp = e.startTime;
        }).observe({ type: "largest-contentful-paint", buffered: true });
        new PerformanceObserver((l) => {
          for (const e of l.getEntries()) window.__lt.push(e.duration);
        }).observe({ type: "longtask", buffered: true });
      } catch {
        /* not supported */
      }
    });
    // The Runs route renders rows, projected by the request's fields — a field the page reads but did not ask for fails here.
    await installApi(page, path === "/runs" ? { runRows: 12 } : {});
    // A route that crashes still paints its heading (the error boundary's), so the budget listens for the crash itself.
    const crashes: string[] = [];
    page.on("pageerror", (e) => crashes.push(String(e.message).slice(0, 160)));
    page.on("console", (m) => {
      if (m.type() === "error" && /crashed/i.test(m.text())) crashes.push(m.text().slice(0, 160));
    });
    const calls: string[] = [];
    page.on("request", (r) => {
      const u = new URL(r.url());
      if (u.pathname.startsWith("/v1/") && u.pathname !== "/v1/ui.vitals") calls.push(u.pathname.replace("/v1/", ""));
    });
    const cdp = await page.context().newCDPSession(page);
    await cdp.send("Emulation.setCPUThrottlingRate", { rate: 4 });
    await page.goto(path, { waitUntil: "domcontentloaded" });
    await expect(page.locator("main").locator("h1, h2").first()).toBeVisible();
    await page.waitForTimeout(1500);
    const { lcp, lt } = await page.evaluate(() => ({
      lcp: window.__lcp,
      lt: window.__lt.reduce((a, b) => a + b, 0),
    }));
    await cdp.detach();
    test
      .info()
      .annotations.push(
        { type: "lcp_ms", description: String(Math.round(lcp)) },
        { type: "long_task_ms", description: String(Math.round(lt)) },
        { type: "api_calls", description: String(calls.length) },
      );
    expect(crashes, `${name}: the view crashed (${crashes.join(" | ")})`).toEqual([]);
    expect(lcp, `${name}: LCP ${Math.round(lcp)} ms over ${LCP_BUDGET_MS}`).toBeLessThanOrEqual(LCP_BUDGET_MS);
    expect(lt, `${name}: ${Math.round(lt)} ms of long tasks over ${LONG_TASK_BUDGET_MS}`).toBeLessThanOrEqual(
      LONG_TASK_BUDGET_MS,
    );
    expect(
      calls.length,
      `${name}: ${calls.length} reads (${calls.join(", ")}) over ${CALL_BUDGET}`,
    ).toBeLessThanOrEqual(CALL_BUDGET);
  });
}
