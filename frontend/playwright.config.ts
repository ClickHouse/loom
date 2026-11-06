import { defineConfig } from "@playwright/test";

// The dashboard's e2e specs. One browser and one worker, so the CPU-throttled budgets in
// perf-budget and route-budget run with no second worker competing for the CPU; CI runs
// the specs in parts on separate runners (`--shard`). A retry only on CI. The server is the production build behind `vite preview` — in the dev
// build React StrictMode re-runs the mount effect, so a deep link would read twice there
// and once in production. `E2E_PORT` lets two checkouts on one host run the suite at
// once.
const PORT = Number(process.env.E2E_PORT ?? 4199);
const BASE = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./e2e",
  outputDir: "./test-results",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  use: {
    baseURL: BASE,
    browserName: "chromium",
    headless: true,
    viewport: { width: 1400, height: 900 },
    trace: "retain-on-failure",
    // Classic scrollbars: headless Chromium hides them by default, and the
    // hover flicker loop this guards against needs a scrollbar that can appear.
    launchOptions: { ignoreDefaultArgs: ["--hide-scrollbars"] },
  },
  // The Explorer runs again at Playwright's default 1280 × 720, where its outline and
  // canvas are narrowest beside the facets and the inspector.
  projects: [
    { name: "desktop" },
    { name: "explorer-1280", testMatch: /explorer-.*\.spec\.ts/, use: { viewport: { width: 1280, height: 720 } } },
  ],
  // `--host 127.0.0.1` in e2e:serve, and the same address here: a runner
  // that resolves `localhost` to `::1` would otherwise serve on IPv6 while
  // the probe below waits on IPv4 until the timeout.
  webServer: {
    command: "npm run e2e:serve",
    url: `${BASE}/`,
    reuseExistingServer: false,
    timeout: 180_000,
    stdout: "pipe",
    stderr: "pipe",
  },
});
