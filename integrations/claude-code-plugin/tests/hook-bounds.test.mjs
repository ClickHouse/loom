import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  boundMs,
  derivedBoundMs,
  rttCachePath,
  DEFAULT_BOUND_MS,
  MAX_BOUND_MS,
  MIN_BOUND_MS,
} from "../lib/hook-bounds.mjs";

test("a measured round trip derives the bound, clamped to the band", () => {
  assert.equal(derivedBoundMs(100), MIN_BOUND_MS); // a LAN client keeps the floor
  assert.equal(derivedBoundMs(1200), 6000); // a tailnet laptop gets room
  assert.equal(derivedBoundMs(5000), MAX_BOUND_MS); // never past the ceiling
  assert.equal(derivedBoundMs(0), DEFAULT_BOUND_MS);
  assert.equal(derivedBoundMs("nope"), DEFAULT_BOUND_MS);
});

test("env wins, then a fresh cache, then the default", () => {
  const now = 1_000_000;
  assert.deepEqual(boundMs({ envValue: "8000", cache: { rtt_ms: 100, ts: now }, now }), { ms: 8000, source: "env" });
  assert.deepEqual(boundMs({ envValue: "", cache: { rtt_ms: 900, ts: now - 1000 }, now }), { ms: 4500, source: "rtt" });
  assert.deepEqual(
    boundMs({ envValue: undefined, cache: { rtt_ms: 900, ts: now - 7 * 3600 * 1000 }, now }),
    { ms: DEFAULT_BOUND_MS, source: "default" },
    "a stale measurement is not trusted",
  );
  assert.deepEqual(boundMs({ envValue: "0", cache: null, now }), { ms: DEFAULT_BOUND_MS, source: "default" });
});

test("the cache is one file per server", () => {
  const a = rttCachePath("/tmp", "https://loom.example");
  const b = rttCachePath("/tmp", "https://other.example");
  assert.match(a, /^\/tmp\/loom-rtt-[0-9a-f]{12}\.json$/);
  assert.notEqual(a, b);
  assert.equal(a, rttCachePath("/tmp", "https://loom.example"));
});
