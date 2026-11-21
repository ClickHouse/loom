import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { sampleCpuPair } from "./cpu-linearity.mjs";

test("CPU calibration keeps the final sampled minimum above its clock floor", () => {
  let calibrationBurst = true;
  const calls = [];
  const measured = sampleCpuPair(
    (text, runs) => {
      calls.push([text, runs]);
      if (text === "small" && runs === 32 && calibrationBurst) {
        calibrationBurst = false;
        return 125;
      }
      if (text === "large" && runs === 4) return 94;
      return text === "small" ? (31 * runs) / 32 : (31 * runs) / 4;
    },
    "small",
    "large",
  );
  assert.ok(measured.small >= 100, JSON.stringify(measured));
  assert.deepEqual(measured, { n: 16, small: 124, large: 124 });
  // The three short/long pairs are preserved at each calibrated size.
  const pairs = (n) =>
    Array.from({ length: 3 }, () => [
      ["small", 8 * n],
      ["large", n],
    ]).flat();
  assert.deepEqual(calls, [
    ["small", 8],
    ["small", 16],
    ["small", 32],
    ...pairs(4),
    ["small", 64],
    ["small", 128],
    ...pairs(16),
  ]);
});

test("a valid nonlinear CPU sample is returned for the unchanged ratio rejection", () => {
  let calls = 0;
  const measured = sampleCpuPair(
    (text, runs) => {
      calls++;
      assert.ok(calls <= 10, "a valid nonlinear sample must not trigger another calibration");
      return text === "small" ? 2 * runs : 64 * runs;
    },
    "small",
    "large",
  );
  assert.deepEqual(measured, { n: 8, small: 128, large: 512 });
  assert.equal(calls, 10);
  assert.throws(() => assert.ok(measured.large < 3 * measured.small), { code: "ERR_ASSERTION" });
});

test("the CPU ratio boundary remains strict", () => {
  const measured = sampleCpuPair((text, runs) => (text === "small" ? 2 * runs : 48 * runs), "small", "large");
  assert.deepEqual(measured, { n: 8, small: 128, large: 384 });
  assert.throws(() => assert.ok(measured.large < 3 * measured.small), { code: "ERR_ASSERTION" });
});
