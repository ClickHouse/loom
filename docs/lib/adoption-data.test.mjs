import assert from "node:assert/strict";
import { test } from "node:test";

import { loadAdoption } from "./adoption-data.mjs";
import { summarizeOss } from "./adoption-view.mjs";

test("uses fixed aggregate views and reports monthly coverage and missing data", async (t) => {
  const queried = [];
  t.mock.method(globalThis, "fetch", async (input, init) => {
    const url = new URL(input);
    if (url.hostname === "clickhouse.example.test") {
      queried.push(init.body);
      assert.equal(init.redirect, "error");
      assert.match(init.headers.Authorization, /^Basic /);
      if (init.body.includes("public_oss_adoption")) {
        return Response.json({
          data: [
            { day: "2025-01-01", source: "pypi", metric: "sdk_downloads", value: 2 },
            { day: "2026-10-01", source: "pypi", metric: "sdk_downloads", value: 3 },
            { day: "2026-11-03", source: "pypi", metric: "sdk_downloads", value: 4 },
            { day: "2026-11-08", source: "github", metric: "stars", value: 60 },
          ],
        });
      }
      return Response.json({ data: [] });
    }
    assert.equal(init.headers, undefined);
    if (url.hostname === "github.com") {
      return new Response('<span class="label">Total downloads</span><h3 title="1869">1.87K</h3>');
    }
    if (url.pathname.endsWith("/loom")) return Response.json({ stargazers_count: 77, forks_count: 20 });
    if (url.hostname === "pypistats.org") {
      return Response.json({
        data: [
          { date: "2026-11-03", category: "without_mirrors", downloads: 6 },
          { date: "2026-11-09", category: "without_mirrors", downloads: 2 },
          { date: "2026-11-09", category: "with_mirrors", downloads: 700 },
          { date: "2026-11-10", category: "without_mirrors", downloads: 5 },
        ],
      });
    }
    return new Response(null, { status: 502 });
  });
  const result = await loadAdoption(
    {
      LOOM_ADOPTION_PUBLIC_CLICKHOUSE_URL: "https://clickhouse.example.test",
      LOOM_ADOPTION_PUBLIC_CLICKHOUSE_USER: "website",
      LOOM_ADOPTION_PUBLIC_CLICKHOUSE_PASSWORD: "password",
    },
    new Date("2026-11-10T08:00:00Z"),
  );

  assert.equal(queried.length, 6);
  assert.ok(queried.every((sql) => sql.startsWith("SELECT * FROM loom_product_analytics.public_")));
  const archived = result.openSource.find(({ id }) => id === "pypi");
  assert.equal(archived.history[0].day, "2025-01-01");
  const byLabel = Object.fromEntries(
    summarizeOss(result.openSource, 90, result.today).map((metric) => [metric.label, metric]),
  );
  const python = byLabel["Python SDK downloads"];
  assert.equal(python.value, 11);
  assert.equal(python.day, "2026-11-09");
  assert.deepEqual(python.monthly, [
    { day: "2026-10-01", value: 3, days: 1 },
    { day: "2026-11-01", value: 8, days: 2 },
  ]);
  assert.equal(byLabel["TypeScript SDK downloads"].value, null);
  assert.equal(byLabel["Container pulls"].value, 1869);
  assert.deepEqual(byLabel["GitHub stars"].history, [
    { day: "2026-11-08", value: 60 },
    { day: "2026-11-10", value: 77 },
  ]);
});
