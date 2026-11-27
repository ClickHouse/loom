import assert from "node:assert/strict";
import { test } from "node:test";

import { readAdoptionPage } from "./adoption-request.mjs";

import {
  activityForRange,
  adoptionCsv,
  adoptionFilters,
  adoptionGrowthForRange,
  adoptionPaths,
  csvBody,
  relativeChange,
  summarizeOss,
} from "./adoption-view.mjs";

test("normalizes reporting periods and keeps daily charts within 30 days", () => {
  assert.deepEqual(
    adoptionFilters(new URLSearchParams("days=90&grain=day&cohort=week&installation=50000&domain=1000000")),
    {
      days: 90,
      grain: "week",
      cohort: "week",
      installation: 50000,
      domain: 1000000,
    },
  );
  assert.deepEqual(adoptionFilters(new URLSearchParams("days=31&grain=__proto__&domain=50001")), {
    days: 30,
    grain: "day",
    cohort: "week",
    installation: 0,
    domain: 0,
  });
  const form = new FormData();
  form.set("days", "365");
  assert.equal(adoptionFilters(form).grain, "month");
  assert.equal(adoptionFilters(form).cohort, "month");
  form.set("grain", "week");
  assert.equal(adoptionFilters(form).grain, "week");
  assert.equal(adoptionFilters(new URLSearchParams("days=30&cohort=month")).cohort, "month");
  assert.deepEqual(adoptionPaths, { "self-hosted": "/adoption", oss: "/adoption/open-source" });
});

test("keeps the self-hosted route clean when a reporting window is submitted", async (t) => {
  t.mock.method(globalThis, "fetch", async () => new Response(null, { status: 503 }));
  const url = new URL("https://loom.clickhouse.com/adoption");
  const request = new Request(url, {
    method: "POST",
    body: new URLSearchParams({ days: "90", grain: "day" }),
  });
  const result = await readAdoptionPage(request, url, "self-hosted");
  assert.ok(!(result instanceof Response));
  assert.equal(result.path, "/adoption");
  assert.equal(result.filters.days, 90);
  assert.equal(result.filters.grain, "week");
  assert.equal(result.headers.get("Cache-Control"), "private, max-age=0");
  assert.equal(result.headers.has("Vercel-CDN-Cache-Control"), false);
});

test("retains the cumulative baseline and sorts the selected growth periods", () => {
  const rows = [
    { grain: "week", period: "2026-11-16", period_end: "2026-11-23", first_active: 9, cumulative_active: 50 },
    { grain: "week", period: "2026-09-14", period_end: "2026-09-21", first_active: 8, cumulative_active: 30 },
    { grain: "day", period: "2026-11-17", period_end: "2026-11-18", first_active: 4, cumulative_active: 45 },
    { grain: "week", period: "2026-11-09", period_end: "2026-11-16", first_active: 3, cumulative_active: 41 },
    { grain: "week", period: "2026-11-23", period_end: "2026-11-30", first_active: 0, cumulative_active: 50 },
  ];
  const filters = adoptionFilters(new URLSearchParams("grain=week"));
  const series = adoptionGrowthForRange(rows, filters, "2026-11-19");
  assert.deepEqual(
    series.map(({ period, cumulative_active, current }) => [period, cumulative_active, current]),
    [
      ["2026-11-09", 41, false],
      ["2026-11-16", 50, true],
    ],
  );
  assert.deepEqual(adoptionGrowthForRange(null, filters, "2026-11-19"), []);
  const monthly = adoptionFilters(new URLSearchParams("grain=month"));
  const july = {
    grain: "month",
    period: "2026-07-01",
    period_end: "2026-08-01",
    first_active: 4,
    cumulative_active: 50,
  };
  assert.deepEqual(adoptionGrowthForRange([july], monthly, "2026-08-01"), [{ ...july, current: false }]);
});

test("projects only an in-progress period with every completed day represented", () => {
  const rows = [
    {
      period: "2026-11-09",
      period_end: "2026-11-16",
      grain: "week",
      operations: 70,
      days_reported: 7,
      forecast_eligible: true,
      active: 3,
      reporting: 5,
    },
    {
      period: "2026-11-16",
      period_end: "2026-11-23",
      grain: "week",
      operations: 120,
      days_reported: 3,
      complete_days_reported: 3,
      forecast_eligible: true,
      active: 8,
      reporting: 11,
    },
    {
      period: "2026-09-14",
      period_end: "2026-09-21",
      grain: "week",
      operations: 40,
      days_reported: 7,
      forecast_eligible: true,
    },
  ];
  const filters = adoptionFilters(new URLSearchParams("grain=week"));
  const data = activityForRange(rows, filters, "2026-11-19");
  assert.deepEqual(
    data.map(({ estimate, current }) => [estimate, current]),
    [
      [null, false],
      [280, true],
    ],
  );
  assert.equal(activityForRange(rows, filters, "2026-11-20")[1].estimate, null);
  const [header, , forecast] = adoptionCsv("activity", data, [], []);
  assert.equal(forecast[header.indexOf("projected_requests")], 280);
  assert.ok(!header.includes("reporting_installations"));
  assert.equal(adoptionCsv("unknown", [], [], []), null);
});

test("keeps a reported opening calendar month that overlaps the rolling window", () => {
  const rows = [
    {
      grain: "month",
      period: "2026-11-01",
      period_end: "2026-12-01",
      reporting: 6,
      active: 3,
      operations: 61,
      days_reported: 15,
    },
    {
      grain: "month",
      period: "2026-12-01",
      period_end: "2027-01-01",
      reporting: 2,
      active: 0,
      operations: 0,
      days_reported: 2,
    },
    {
      grain: "month",
      period: "2027-01-01",
      period_end: "2027-02-01",
      reporting: 99,
      active: 11,
      operations: 100,
      days_reported: 1,
    },
  ];
  const filters = adoptionFilters(new URLSearchParams("days=30&grain=month"));
  assert.deepEqual(
    activityForRange(rows, filters, "2027-01-01").map(({ period, reporting, active, operations, opening }) => [
      period,
      reporting,
      active,
      operations,
      opening,
    ]),
    [["2026-12-01", 2, 0, 0, true]],
  );
});

test("exports lifecycle thresholds at the selected interval", () => {
  const weekly = { grain: "week", bucket: "2026-11-09", new: 1, retained: 0, reactivated: 0, inactive: 0 };
  const monthly = { ...weekly, grain: "month", bucket: "2026-11-01" };
  const rows = adoptionCsv("lifecycle", [], [{ entity: "domain", threshold: 50_000, rows: [weekly, monthly] }], []);
  assert.equal(rows[1][2], 11_666);
  assert.equal(rows[2][2], 50_000);
});

test("exports every usage period with coverage, exact zeros, and blank missing values", () => {
  const activity = Array.from({ length: 22 }, (_, index) => ({
    period: new Date(Date.UTC(2026, 10, index + 1)).toISOString().slice(0, 10),
    period_end: new Date(Date.UTC(2026, 10, index + 2)).toISOString().slice(0, 10),
    days_reported: 1,
    current: index === 21,
    memory_requests: index,
    embedding_tokens: index === 21 ? 40 : null,
  }));
  const [header, ...rows] = adoptionCsv("usage", activity, [], []);
  assert.deepEqual(header.slice(0, 4), ["period_start_utc", "period_end_utc", "days_reported", "in_progress"]);
  assert.equal(new Set(header).size, header.length);
  assert.ok(header.includes("median_total_namespaces"));
  assert.ok(header.includes("server_errors"));
  assert.equal(rows.length, 22);
  assert.deepEqual(rows[0].slice(0, 4), ["2026-11-01", "2026-11-02", 1, false]);
  assert.deepEqual(rows.at(-1).slice(0, 4), ["2026-11-22", "2026-11-23", 1, true]);
  const memory = header.indexOf("memory_requests");
  const embeddings = header.indexOf("embedding_tokens");
  assert.equal(rows[0][memory], 0);
  assert.equal(rows[0][embeddings], null);
  assert.equal(rows.at(-1)[memory], 21);
  assert.equal(rows.at(-1)[embeddings], 40);
});

test("compares downloads with equal complete coverage and encodes public exports", () => {
  const history = Array.from({ length: 60 }, (_, index) => ({
    day: new Date(Date.UTC(2026, 10, 1 + index)).toISOString().slice(0, 10),
    value: index < 30 ? 3 : 6,
  }));
  const input = [{ id: "npm", source: "npm", artifact: "@clickhouse/loom", isDownload: true, history }];
  const [downloads] = summarizeOss(input, 30, "2026-12-31");
  assert.equal(downloads.value, 180);
  assert.equal(downloads.change, 1);
  assert.equal(summarizeOss([{ ...input[0], history: history.slice(1) }], 30, "2026-12-31")[0].change, null);
  assert.equal(relativeChange(7, 0), null);
  assert.match(
    csvBody(adoptionCsv("downloads", [], [], [downloads])),
    /^"registry","package","day_utc","downloads"\r\n"npm","'@clickhouse\/loom","2026-12-01","6"/,
  );
  assert.equal(csvBody([["=1+1", null, -2, 'A"B']]), '"\'=1+1","","-2","A""B"\r\n');
});

test("compares full annual SDK windows when historical daily rows are available", () => {
  const history = Array.from({ length: 730 }, (_, index) => ({
    day: new Date(Date.UTC(2025, 0, 1 + index)).toISOString().slice(0, 10),
    value: index < 365 ? 2 : 3,
  }));
  const [downloads] = summarizeOss([{ id: "npm", isDownload: true, history }], 365, "2027-01-01");
  assert.equal(downloads.days, 365);
  assert.equal(downloads.change, 0.5);
});
