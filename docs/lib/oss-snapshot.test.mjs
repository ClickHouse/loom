import assert from "node:assert/strict";
import { test } from "node:test";

import { snapshotOss } from "./oss-snapshot.mjs";

const endpoint = "https://telemetry.example.test/api/oss-snapshot";
const env = {
  CRON_SECRET: "job-secret",
  LOOM_TELEMETRY_CLICKHOUSE_URL: "https://analytics.example.test:8443",
  LOOM_TELEMETRY_CLICKHOUSE_USER: "writer",
  LOOM_TELEMETRY_CLICKHOUSE_PASSWORD: "write-password",
};

async function runSnapshot(sources) {
  const writes = [];
  const fetcher = async (url, init) => {
    const address = new URL(url);
    if (address.hostname === "analytics.example.test") {
      assert.match(address.searchParams.get("query"), /INSERT INTO loom_product_analytics\.oss_metrics/);
      writes.push(init.body.split("\n").map((row) => JSON.parse(row)));
      return new Response("");
    }
    const source = sources[address.pathname];
    assert.ok(source, `Unexpected snapshot source ${address.pathname}`);
    assert.equal(init.headers?.Authorization, undefined);
    return source(address, init);
  };
  const request = new Request(endpoint, { headers: { Authorization: "Bearer job-secret" } });
  const response = await snapshotOss(request, env, fetcher, new Date("2026-11-24T02:30:00Z"));
  return { response, writes };
}

test("allows the scheduled job to record repository and distribution counters for the UTC date", async () => {
  const releaseRequests = [];
  const { response, writes } = await runSnapshot({
    "/repos/ClickHouse/loom": () => Response.json({ stargazers_count: 42, forks_count: 7 }),
    "/repos/ClickHouse/loom/releases": (address, { signal }) => {
      releaseRequests.push({ address: address.href, signal });
      if (address.searchParams.get("page") === "1") {
        return Response.json([{ assets: [{ download_count: 3 }, { download_count: 5 }] }], {
          headers: { Link: '<https://untrusted.example/releases?page=2>; rel="next"' },
        });
      }
      return Response.json([{ assets: [{ download_count: 11 }] }]);
    },
    "/ClickHouse/loom/pkgs/container/loom": () =>
      new Response('<span class="label">Total downloads</span><h3 title="1869">1.87K</h3>'),
  });
  assert.equal(response.status, 204);
  assert.deepEqual(
    releaseRequests.map(({ address }) => address),
    [
      "https://api.github.com/repos/ClickHouse/loom/releases?per_page=100&page=1",
      "https://api.github.com/repos/ClickHouse/loom/releases?per_page=100&page=2",
    ],
  );
  assert.equal(releaseRequests[0].signal, releaseRequests[1].signal);
  assert.deepEqual(writes, [
    [
      { day: "2026-11-24", source: "github", artifact: "ClickHouse/loom", metric: "stars", value: 42 },
      { day: "2026-11-24", source: "github", artifact: "ClickHouse/loom", metric: "forks", value: 7 },
      {
        day: "2026-11-24",
        source: "github",
        artifact: "ClickHouse/loom",
        metric: "release_asset_downloads_total",
        value: 19,
      },
      {
        day: "2026-11-24",
        source: "ghcr",
        artifact: "ghcr.io/clickhouse/loom",
        metric: "container_pulls_total",
        value: 1869,
      },
    ],
  ]);
});

test("preserves verified snapshots when the repository is unavailable or release results are partial", async () => {
  const releasePages = [];
  const { response, writes } = await runSnapshot({
    "/repos/ClickHouse/loom": () => new Response(null, { status: 503 }),
    "/repos/ClickHouse/loom/releases": (address) => {
      const page = address.searchParams.get("page");
      releasePages.push(page);
      if (page === "2") return new Response(null, { status: 503 });
      return Response.json([{ assets: [{ download_count: 7 }] }], {
        headers: { Link: '<https://api.github.com/repos/ClickHouse/loom/releases?page=2>; rel="next"' },
      });
    },
    "/ClickHouse/loom/pkgs/container/loom": () => new Response('<span>Total downloads</span><h3 title="0">0</h3>'),
  });
  assert.equal(response.status, 204);
  assert.deepEqual(releasePages, ["1", "2"]);
  assert.deepEqual(writes, [
    [
      {
        day: "2026-11-24",
        source: "ghcr",
        artifact: "ghcr.io/clickhouse/loom",
        metric: "container_pulls_total",
        value: 0,
      },
    ],
  ]);
});

test("rejects malformed totals and skips the warehouse when no source has an observation", async () => {
  const { response, writes } = await runSnapshot({
    "/repos/ClickHouse/loom": () => Response.json({ stargazers_count: -1, forks_count: "3" }),
    "/repos/ClickHouse/loom/releases": () => Response.json([{ assets: [{ download_count: 7 }, {}] }]),
    "/ClickHouse/loom/pkgs/container/loom": () => new Response("<span>Total downloads</span><h3>Unavailable</h3>"),
  });
  assert.equal(response.status, 502);
  assert.deepEqual(writes, []);
});

test("rejects unauthenticated calls before making requests", async () => {
  const fetcher = () => assert.fail("unauthorized request reached the network");
  assert.equal((await snapshotOss(new Request(endpoint), env, fetcher)).status, 401);
  assert.equal((await snapshotOss(new Request(endpoint), {}, fetcher)).status, 401);
});
