/**
 * PR history's watermark is a merge time, so its card has no commit lag: the
 * pill reads the worker's cadence, and the watermark shows to the minute.
 */
import { describe, expect, it } from "vitest";

import type { CodeWorkerStatus } from "../api";
import { passFreshness, watermarkText } from "./Code";

const NOW = Date.parse("2030-01-01T12:00:00Z");
const worker = (over: Partial<CodeWorkerStatus>): CodeWorkerStatus =>
  ({
    enabled: true,
    consecutive_failures: 0,
    interval_seconds: 900,
    last_run_at: "2030-01-01T11:48:00Z",
    last_duration_ms: 214056,
    last_error: "",
    ...over,
  }) as CodeWorkerStatus;

describe("passFreshness", () => {
  it("is current while a pass ran within two intervals", () => {
    expect(passFreshness(worker({}), NOW)).toEqual({ text: "Current", tone: "ok" });
  });
  it("is stale once passes stop", () => {
    expect(passFreshness(worker({ last_run_at: "2030-01-01T10:00:00Z" }), NOW)).toEqual({
      text: "Stale",
      tone: "warn",
    });
  });
  it("names failing, disabled, pending and absent workers", () => {
    expect(passFreshness(worker({ consecutive_failures: 2 }), NOW).text).toBe("Failing");
    expect(passFreshness(worker({ enabled: false }), NOW).text).toBe("Disabled");
    expect(passFreshness(worker({ last_run_at: "" }), NOW).text).toBe("Pending");
    expect(passFreshness(undefined, NOW).text).toBe("Not on this replica");
  });
});

describe("watermarkText", () => {
  it("shortens a sha and keeps a time to the minute", () => {
    expect(watermarkText("49b3ec4f9aa1bb2cc3dd4ee5ff6a7b8c9d0e1f2a")).toBe("49b3ec4f9aa1");
    expect(watermarkText("2030-01-01T11:21:07Z")).toBe("2030-01-01 11:21");
  });
});
