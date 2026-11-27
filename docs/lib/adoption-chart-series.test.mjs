import assert from "node:assert/strict";
import { test } from "node:test";
import { activityPoints, groupDailySeries, metricGrowth, periodEnd, periodStart } from "./adoption-chart-series.mjs";

test("calendar buckets keep the same UTC start for both charts", () => {
  assert.equal(periodStart("2026-01-04", "week"), "2025-12-29");
  assert.equal(periodEnd("2025-12-29", "week"), "2026-01-05");
  assert.equal(periodStart("2025-12-31", "month"), "2025-12-01");
  assert.equal(periodEnd("2025-12-01", "month"), "2026-01-01");
  assert.equal(periodEnd("2026-01-31", "day"), "2026-02-01");
});

test("source intervals sum observed events or use the last observed gauge", () => {
  const history = [
    { day: "2026-01-05", value: 900 },
    { day: "2026-01-03", value: 12 },
    { day: "2025-12-31", value: 7 },
    { day: "2026-01-04", value: 0 },
    { day: "2025-12-29", value: 900 },
  ];
  const options = { start: "2025-12-30", end: "2026-01-05" };

  assert.deepEqual(groupDailySeries(history, "week", options), [
    { day: "2025-12-29", value: 19, days: 3, current: false, partial: true },
  ]);
  assert.deepEqual(groupDailySeries(history, "week", { ...options, kind: "gauge" }), [
    { day: "2025-12-29", value: 0, days: 3, current: false, partial: false },
  ]);
  assert.deepEqual(
    groupDailySeries(history, "day", options).map(({ day }) => day),
    ["2025-12-31", "2026-01-03", "2026-01-04"],
  );
});

test("cumulative observed counts preserve gaps and reported zeros", () => {
  const rows = [
    { day: "2026-01-04", value: 5, partial: true },
    { day: "2026-01-01", value: 2 },
    { day: "2026-01-02", value: 0 },
  ];

  assert.deepEqual(metricGrowth(rows, "day", "additive"), [
    { day: "2026-01-01", value: 2 },
    { day: "2026-01-02", value: 2 },
    { day: "2026-01-04", value: 7, partial: true },
  ]);
});

test("gauge change preserves signed and zero deltas between comparable reports", () => {
  const rows = [
    { day: "2026-01-01", value: 12 },
    { day: "2026-01-02", value: 4 },
    { day: "2026-01-03", value: 4 },
    { day: "2026-01-05", value: 20 },
    { day: "2026-01-06", value: 25, partial: true },
    { day: "2026-01-07", value: 19 },
    { day: "2026-01-08", value: 23, current: true },
  ];

  assert.deepEqual(metricGrowth(rows, "day", "gauge"), [
    { day: "2026-01-02", value: -8 },
    { day: "2026-01-03", value: 0 },
  ]);
});

test("missing activity fields stay unknown while observed zero still plots", () => {
  const rows = [
    { period: "2026-01-05", requests: 0, days_reported: 7, current: false },
    { period: "2026-01-12", requests: null, days_reported: 7, current: false },
    { period: "2026-01-19", requests: 4, days_reported: 2, current: true },
    { period: "2026-01-26", days_reported: 7, current: false },
  ];

  assert.deepEqual(activityPoints(rows, "requests", "week"), [
    { day: "2026-01-05", value: 0, days: 7, current: false, partial: false },
    { day: "2026-01-19", value: 4, days: 2, current: true, partial: true },
  ]);
});

test("account plots use the coverage of account reports", () => {
  const rows = [
    { period: "2026-01-05", active_accounts: 11, days_reported: 7, accounts_days_reported: 1, current: false },
    { period: "2026-01-12", active_accounts: 10, days_reported: 7, accounts_days_reported: 7, current: false },
  ];
  const points = activityPoints(rows, "active_accounts", "week", "accounts_days_reported");
  assert.deepEqual(
    points.map(({ days, partial }) => [days, partial]),
    [
      [1, true],
      [7, false],
    ],
  );
  assert.deepEqual(metricGrowth(points, "week", "gauge"), []);
});
