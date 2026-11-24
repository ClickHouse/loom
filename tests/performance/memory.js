import { check } from "k6";
import http from "k6/http";
import exec from "k6/execution";

const base = "http://127.0.0.1:7777/v1";
const identity = { org: "ci-load", namespace: "memory", agent: "k6", session_id: "load" };
const seed = "synthetic release checklist with an owner and rollback plan";
const query = { ...identity, query: seed, top_k: 5, auto_touch: false };
const headers = { "Content-Type": "application/json" };

export const options = {
  scenarios: {
    memory: {
      executor: "constant-arrival-rate",
      rate: 4,
      timeUnit: "1s",
      duration: "2m",
      preAllocatedVUs: 8,
      maxVUs: 12,
    },
  },
  // Report latency without gating it until the hosted baseline is repeatable.
  thresholds: {
    checks: ["rate==1"],
    http_req_failed: ["rate==0"],
    iterations: ["count>0"],
  },
  summaryTrendStats: ["med", "p(95)", "p(99)"],
};

function post(op, payload) {
  return http.post(`${base}/${op}`, JSON.stringify(payload), {
    headers,
    redirects: 0,
    timeout: "5s",
    tags: { name: op },
  });
}

function stored(response) {
  return response.status === 200 && Boolean(response.json("memory_id"));
}

export function setup() {
  const response = post("memory.set", {
    ...identity,
    memory_key: "seed",
    value: seed,
  });
  if (!stored(response)) {
    throw new Error(`Memory fixture setup failed (HTTP ${response.status})`);
  }
}

export default function () {
  const n = exec.scenario.iterationInTest;
  if (n % 4 === 0) {
    const response = post("memory.set", {
      ...identity,
      memory_key: `write-${n}`,
      value: `Synthetic observation number ${n}`,
      memory_type: "episodic",
    });
    check(response, { "memory write returns a row ID": stored });
    return;
  }
  const response = post("memory.search", query);
  check(response, {
    "memory search finds the seed": (result) =>
      result.status === 200 && result.json("results").some((row) => row.memory_key === "seed"),
  });
}

export function handleSummary({ metrics }) {
  const rows = [
    ["Completed iterations", metrics.iterations?.values.count ?? 0],
    ["Dropped iterations", metrics.dropped_iterations?.values.count ?? 0],
    ["Requests per second", metrics.http_reqs?.values.rate?.toFixed(2) ?? "n/a"],
    ["Request failure rate", metrics.http_req_failed?.values.rate ?? "n/a"],
    ["Request p95 (ms)", metrics.http_req_duration?.values["p(95)"]?.toFixed(2) ?? "n/a"],
  ];
  return {
    "load-summary.md": [
      "## Synthetic memory load",
      "",
      "| Metric | Result |",
      "| --- | ---: |",
      ...rows.map(([name, value]) => `| ${name} | ${value} |`),
      "",
    ].join("\n"),
  };
}
