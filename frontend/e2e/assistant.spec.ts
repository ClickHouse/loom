import { expect, test, type Page } from "@playwright/test";
import { violations } from "./axe";
import { installApi } from "./fixtures";

// The Assistant page reads nothing on mount, so the route-wide axe and layout runs see only
// its starters. This spec draws every panel it has, with the server's full-length text and a
// long subject id, and checks it at desktop and phone width: no horizontal scroll, no axe
// violation.

const LONG_ID = `user-${"x".repeat(200)}`;
const OWN_USER =
  "Live rows filed under this user_id, whoever wrote them: what a token pinned to this user (read_scope own_user) reads. Promoted rows (memory.promote) are not counted.";
const OTHERS_USER =
  "Every other live row of the namespace. A member reads these too; a token pinned to this user does not. Promoted rows (memory.promote) are not counted.";

const row = (key: string) => ({
  memory_key: key,
  memory_id: `id-${key}`,
  version: 3,
  content_sha256: "ab12",
  memory_type: "semantic",
  status: "active",
  source_class: "direct",
  agent: "claude-code",
  user_id: LONG_ID,
  session_id: "-",
  actor: "human:o@example.com",
  updated_at: "2026-11-01T10:00:00",
  sensitivity: "normal",
  versions: 3,
  excerpt: "Invoices go out on Fridays, and a reminder goes to the account owner two days before.",
});

const view = (definition: string, keys: string[]) => ({
  definition,
  live_rows: keys.length,
  by_type: keys.length ? [{ name: "semantic", rows: keys.length }] : [],
  by_status: keys.length ? [{ name: "active", rows: keys.length }] : [],
  by_source_class: [],
  top_tags: keys.length ? [{ name: "billing", rows: keys.length }] : [],
  categories: [],
  first_updated_at: null,
  last_updated_at: null,
  newest: keys.map(row),
  complete: true,
});

const INSPECTION = {
  org: "dev",
  namespace: "personal-alice",
  subject: "user",
  id: LONG_ID,
  computed_at: "2026-11-01T10:00:00",
  own: view(OWN_USER, ["billing/invoices/a-rather-long-key-name-that-a-writer-chose"]),
  others: view(OTHERS_USER, ["notes/standup"]),
  activity: {
    window_seconds: 7 * 86_400,
    calls: [{ operation: "memory.search", calls: 9, succeeded: 8 }],
    first_at: "2026-10-28T10:00:00",
    last_at: "2026-11-01T10:00:00",
    gap: "",
  },
  settings: {
    guide_revision: 2,
    guide_summary: "key layout",
    categories_registered: 1,
    extraction_policy: "Keep costs and ages.\nNever store credentials.",
    extraction_policy_unavailable: "",
    extraction_instructions:
      "Per request only: memory.set_from_messages takes `extraction_instructions`; no user, agent or session setting stores them.",
  },
  skipped: [],
};

const INVENTORY = {
  scope: { org: "dev", namespace: "personal-alice", read_scope: "namespace", user_id: null, session_id: null },
  sections: [
    { plane: "memories", count: 42, latest_at: "2026-11-01T10:00:00", complete: true, reason: "" },
    { plane: "records", count: null, latest_at: null, complete: false, reason: "read failed: TimeoutError" },
    { plane: "documents", count: 3, latest_at: null, complete: true, reason: "" },
    { plane: "runs", count: 0, latest_at: null, complete: true, reason: "" },
    { plane: "integrations", count: 1, latest_at: null, complete: true, reason: "" },
  ],
  populations: [
    {
      population: "members",
      total: 2,
      top: [],
      complete: false,
      reason: "shared with everyone, a group or an agent: those members are not counted",
    },
    {
      population: "agents",
      total: 1,
      top: [{ name: "a-very-long-agent-name-from-a-client-config", rows: 40 }],
      complete: true,
      reason: "",
    },
    { population: "authors", total: 1, top: [{ name: "human:o@example.com", rows: 42 }], complete: true, reason: "" },
    {
      population: "subjects",
      total: 2,
      top: [
        { name: LONG_ID, rows: 12 },
        { name: "ada", rows: 3 },
      ],
      complete: true,
      reason: "",
    },
  ],
  capabilities: [
    { name: "llm", enabled: false },
    { name: "embedder", enabled: true },
  ],
  computed_at: "2026-11-01T10:00:00",
};

const TAG = "billing-and-invoicing-for-enterprise-accounts";
const TUNE = {
  org: "dev",
  namespace: "personal-alice",
  computed_at: "2026-11-01T10:00:00",
  proposals: [
    {
      id: "2ef4b21f4c7eb774",
      target: "namespace.category",
      scope: "namespace dev/personal-alice",
      title: `Register a category for the tag \`${TAG}\``,
      rationale: `12 live rows carry the tag \`${TAG}\` and no category is named after it; 4 of them are filed under another category. A registered category is what namespace.search_categories finds, and extraction offers the first 32 by name as tags for new rows.`,
      current: "",
      current_revision: null,
      proposed: TAG,
      evidence: {
        finding_codes: [],
        supporting_count: 12,
        supporting: [
          {
            memory_key: "billing/invoices/a-rather-long-key-name-that-a-writer-chose",
            memory_id: "id-billing",
            version: 3,
            content_sha256: "ab12",
            agent: "claude-code",
            user_id: "ada",
            session_id: "-",
            memory_type: "semantic",
            source_class: "direct",
            updated_at: "2026-11-01T10:00:00",
          },
        ],
        counter_count: 4,
      },
      affected_agents: ["a-very-long-agent-name-from-a-client-config"],
      affected_agents_total: 3,
      effect: "future",
      historical:
        "Registering files no row: rows already tagged join the category only through memory.assign_category, a separate action per row.",
      apply: {
        op: "namespace.register_category",
        args: { org: "dev", namespace: "personal-alice", name: TAG, description: `Rows tagged \`${TAG}\`.` },
        args_complete: true,
        required_role: "writer",
        guard:
          "None: registering a name that exists updates that category, so read namespace.list_categories again just before applying.",
      },
      limits: "A tag used for two meanings would need two categories, not one.",
    },
  ],
  guide_revision: 2,
  categories_registered: 1,
  extraction_policy: "",
  extraction_policy_unavailable: "",
  skipped: ["audit.telemetry_unavailable: window within the ledger lag"],
  model_used: false,
};

async function openWithEveryPanel(page: Page) {
  await installApi(page);
  // Routes added after installApi answer first.
  for (const [op, body] of [
    ["memory.inspect", INSPECTION],
    ["namespace.inventory", INVENTORY],
    ["namespace.tune", TUNE],
  ] as const)
    await page.route(`**/v1/${op}`, (r) => r.fulfill({ json: body }));
  await page.goto("/assistant");
  await expect(page.getByRole("heading", { level: 1, name: "Assistant" })).toBeVisible();
  await page.getByRole("button", { name: "What is in this project?" }).click();
  await expect(page.getByRole("table", { name: "What this namespace holds" })).toBeVisible();
  await page.getByLabel("User id").fill(LONG_ID);
  await page.getByLabel("User id").press("Enter");
  await expect(page.getByTestId("assistant-inspection")).toBeVisible();
  await page.getByRole("button", { name: "Configure this project" }).click();
  await expect(page.getByTestId("assistant-review")).toBeVisible();
  await page.waitForTimeout(300); // entry animations
}

// One starter panel is open at a time; each is measured in turn with the rest drawn.
const STARTER_PANELS: Array<[string, (page: Page) => Promise<void>]> = [
  [
    "What is in this project?",
    async (page) => expect(page.getByRole("table", { name: "What this namespace holds" })).toBeVisible(),
  ],
  [
    "Who are the users in this project?",
    async (page) => expect(page.getByTestId("assistant-populations")).toBeVisible(),
  ],
  ["Show relationships between facts", async (page) => expect(page.getByLabel("Entity")).toBeVisible()],
];

async function openStarter(page: Page, [name, shown]: (typeof STARTER_PANELS)[number]) {
  await page.getByRole("button", { name }).click();
  await shown(page);
  await page.waitForTimeout(300);
}

async function clipped(page: Page): Promise<string[]> {
  // Nothing runs past the box that clips or scrolls it: text cut off inside a panel
  // does not widen the page, so the page width alone would miss it.
  return page.evaluate(() => {
    const main = document.querySelector("main");
    if (!main) return ["no main"];
    const clipOf = (el: Element): Element => {
      for (let a = el.parentElement; a && a !== main; a = a.parentElement) {
        if (getComputedStyle(a).overflowX !== "visible") return a;
      }
      return main;
    };
    return [...main.querySelectorAll("*")]
      .filter((el) => el.getBoundingClientRect().width > 0)
      .filter((el) => el.getBoundingClientRect().right > clipOf(el).getBoundingClientRect().right + 1)
      .map((el) => `${el.tagName}.${el.className} ${(el.textContent ?? "").slice(0, 50)}`);
  });
}

for (const width of [1400, 375]) {
  test.describe(`${width} px`, () => {
    test.use({ viewport: { width, height: 900 } });

    test("every panel fits the page width", async ({ page }) => {
      await openWithEveryPanel(page);
      for (const starter of STARTER_PANELS) {
        await openStarter(page, starter);
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
        expect(overflow, starter[0]).toBeLessThanOrEqual(0);
        expect(await clipped(page), starter[0]).toEqual([]);
      }
    });

    test("every panel has no WCAG A/AA violations", async ({ page }) => {
      await openWithEveryPanel(page);
      for (const starter of STARTER_PANELS) {
        await openStarter(page, starter);
        expect(await violations(page), starter[0]).toEqual([]);
      }
    });
  });
}

test("Back from the graph lands on the Relationships starter", async ({ page }) => {
  await installApi(page);
  await page.goto("/assistant");
  await page.getByRole("button", { name: "Show relationships between facts" }).click();
  await page.getByLabel("Entity").fill("invoices");
  await page.getByRole("button", { name: "Open the graph" }).click();
  await expect(page).toHaveURL(/\/graph\?.*seed=invoices/);
  await page.goBack();
  await expect(page.getByRole("button", { name: "Show relationships between facts" })).toBeFocused();
});

test("a listed or cited row opens in the inspector under its whole writer identity", async ({ page }) => {
  const gets: Record<string, unknown>[] = [];
  await installApi(page);
  for (const [op, body] of [
    ["memory.inspect", INSPECTION],
    ["namespace.tune", TUNE],
  ] as const)
    await page.route(`**/v1/${op}`, (r) => r.fulfill({ json: body }));
  await page.route("**/v1/memory.get", (r) => {
    gets.push(r.request().postDataJSON() as Record<string, unknown>);
    return r.fallback();
  });
  await page.goto("/assistant");
  await page.getByLabel("User id").fill(LONG_ID);
  await page.getByLabel("User id").press("Enter");
  const key = "billing/invoices/a-rather-long-key-name-that-a-writer-chose";
  await page
    .getByTestId("assistant-inspection")
    .getByRole("button", { name: `Open memory ${key}` })
    .click();
  await expect.poll(() => gets.length).toBeGreaterThan(0);
  expect(gets.at(-1)).toMatchObject({ memory_key: key, agent: "claude-code", user_id: LONG_ID, session_id: "-" });
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Configure this project" }).click();
  const before = gets.length;
  await page
    .getByTestId("assistant-review")
    .getByRole("button", { name: `Open memory ${key}` })
    .click();
  await expect.poll(() => gets.length).toBeGreaterThan(before);
  expect(gets.at(-1)).toMatchObject({ memory_key: key, agent: "claude-code", user_id: "ada", session_id: "-" });
});
