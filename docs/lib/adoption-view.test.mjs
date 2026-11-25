import assert from "node:assert/strict";
import { test } from "node:test";

import {
  activityForRange,
  adoptionCsv,
  adoptionFilters,
  adoptionUrl,
  csvBody,
  relativeChange,
  summarizeOss,
} from "./adoption-view.mjs";

test("allowlists shareable ranges, grains, and thresholds", () => {
  const filters = adoptionFilters(
    new URLSearchParams("tab=oss&days=90&grain=day&cohort=week&installation=50000&domain=1000000"),
  );
  assert.deepEqual(filters, {
    tab: "oss",
    days: 90,
    grain: "day",
    cohort: "week",
    installation: 50000,
    domain: 1000000,
  });
  assert.equal(
    adoptionUrl(filters, {}, "#downloads"),
    "?tab=oss&days=90&grain=day&cohort=week&installation=50000&domain=1000000#downloads",
  );
  assert.deepEqual(adoptionFilters(new URLSearchParams("days=31&grain=__proto__&domain=50001")), {
    tab: "self-hosted",
    days: 30,
    grain: "week",
    cohort: "month",
    installation: 0,
    domain: 0,
  });
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
  const filters = adoptionFilters(new URLSearchParams());
  const data = activityForRange(rows, filters, "2026-11-19");
  assert.deepEqual(
    data.map(({ estimate, current }) => [estimate, current]),
    [
      [null, false],
      [280, true],
    ],
  );
  assert.equal(activityForRange(rows, filters, "2026-11-20")[1].estimate, null);
  assert.equal(adoptionCsv("activity", data, [], [])[2][5], 280);
  assert.equal(adoptionCsv("unknown", [], [], []), null);
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
