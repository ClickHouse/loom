import assert from "node:assert/strict";
import { test } from "node:test";

import { createAdoptionReader } from "./adoption-data.mjs";
import { summarizeOss } from "./adoption-view.mjs";

test("calls the saved aggregate endpoint and reports monthly coverage and missing data", async (t) => {
  const queried = [];
  const registries = [];
  t.mock.method(globalThis, "fetch", async (input, init) => {
    const url = new URL(input);
    if (url.hostname === "queries.clickhouse.cloud") {
      queried.push(url.href);
      assert.equal(init.redirect, "error");
      assert.equal(init.headers.Authorization, `Basic ${Buffer.from("fixture-id:fixture-secret").toString("base64")}`);
      assert.equal(init.body, undefined);
      return Response.json({
        data: [
          {
            totals: [],
            versions: [],
            retention: [],
            activity: [],
            lifecycle: [],
            domains: ["company.example", "other.example", { domain: "private.example", accounts: 7 }],
            oss: [
              { day: "2025-01-01", source: "pypi", metric: "sdk_downloads", value: 2 },
              { day: "2026-10-01", source: "pypi", metric: "sdk_downloads", value: 3 },
              { day: "2026-11-03", source: "pypi", metric: "sdk_downloads", value: 4 },
              { day: "2026-11-08", source: "github", metric: "stars", value: 60 },
            ].map((row) => JSON.stringify(row)),
          },
        ],
      });
    }
    registries.push(url.hostname);
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
  const read = createAdoptionReader(
    {
      LOOM_ADOPTION_QUERY_ENDPOINT_URL:
        "https://queries.clickhouse.cloud/run/00000000-0000-0000-0000-000000000001?format=CSV",
      LOOM_ADOPTION_QUERY_KEY_ID: "fixture-id",
      LOOM_ADOPTION_QUERY_KEY_SECRET: "fixture-secret",
    },
    new Date("2026-11-10T08:00:00Z"),
  );
  const selfHosted = await read("self-hosted");
  assert.deepEqual(selfHosted.selfHosted.domains, ["company.example", "other.example"]);
  assert.deepEqual(selfHosted.openSource, []);
  assert.deepEqual(registries, []);

  const published = read("oss");
  assert.equal(read("oss"), published);
  const result = await published;

  assert.deepEqual(queried, ["https://queries.clickhouse.cloud/run/00000000-0000-0000-0000-000000000001?format=JSON"]);
  assert.deepEqual(result.selfHosted.domains, ["company.example", "other.example"]);
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

test("keeps Query API credentials off unrecognized endpoint URLs", async (t) => {
  const requests = [];
  t.mock.method(console, "warn", () => {});
  t.mock.method(globalThis, "fetch", async (input, init) => {
    requests.push([new URL(input).hostname, init.headers?.Authorization]);
    return new Response(null, { status: 502 });
  });
  const read = createAdoptionReader(
    {
      LOOM_ADOPTION_QUERY_ENDPOINT_URL: "https://external.example/run/00000000-0000-0000-0000-000000000001",
      LOOM_ADOPTION_QUERY_KEY_ID: "fixture-id",
      LOOM_ADOPTION_QUERY_KEY_SECRET: "fixture-secret",
    },
    new Date("2026-11-10T08:00:00Z"),
  );
  const result = await read("oss");
  assert.ok(requests.every(([host, authorization]) => host !== "external.example" && authorization === undefined));
  assert.equal(result.selfHosted.totals, undefined);
  assert.equal(result.selfHosted.domains, null);
});
