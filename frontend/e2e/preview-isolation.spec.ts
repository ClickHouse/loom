import { expect, test, type Page, type Route } from "@playwright/test";
import { installApi } from "./fixtures";

// An uploaded document is attacker-chosen bytes under an attacker-chosen type, and the
// Library previews it on the dashboard's own origin, where the bearer token sits in
// localStorage. Each fixture below tries to run script: set a global on the parent, read
// the token and send it to /exfil, and alert. The mocked server answers with the uploaded
// type as-is (inline), so what is tested is the dashboard's own allowlist: text in an
// empty-sandbox iframe as text/plain, raster images as <img>, everything else downloads.
// Opening a preview on its own — its blob: URL, or the download URL — must not run
// script either.

const PAYLOAD =
  "<script>" +
  "try { parent.__pwned = 'parent'; } catch (e) {}" +
  "try { top.__pwned = 'top'; } catch (e) {}" +
  "fetch('/exfil?t=' + encodeURIComponent(localStorage.getItem('loom_token_v1')));" +
  "alert('xss');" +
  "</script>";
const HTML = `<!doctype html><html><body>${PAYLOAD}<img src=x onerror="fetch('/exfil?img')"></body></html>`;
const SVG =
  `<svg xmlns="http://www.w3.org/2000/svg" onload="fetch('/exfil?svg')">` +
  `<script>fetch('/exfil?svg-script'); alert('svg')</script></svg>`;

interface Fixture {
  title: string;
  type: string;
  body: string;
}

const FIXTURES: Fixture[] = [
  { title: "HTML page", type: "text/html", body: HTML },
  { title: "SVG drawing", type: "image/svg+xml", body: SVG },
  { title: "HTML labelled text", type: "text/plain", body: HTML },
  { title: "HTML labelled PNG", type: "image/png", body: HTML },
];

const uri = (f: Fixture) => `docs/${f.title.toLowerCase().replace(/ /g, "-")}`;

// The /me page's policy, as server/identity/app/me_routes.py::dashboard_csp builds it
// (tests/unit/test_web_exposure.py pins the server side). vite preview sends no headers,
// so the page is served here with it and the nonce'd boot snippet the server injects.
const NONCE = "e2eNonce0123456789";
const DASHBOARD_CSP =
  "default-src 'self'; " +
  `script-src 'self' 'nonce-${NONCE}'; ` +
  "style-src 'self' 'unsafe-inline'; " +
  "font-src 'self' data:; " +
  "img-src 'self' data: blob:; " +
  "frame-src 'self' blob:; " +
  "connect-src 'self'; " +
  "object-src 'none'; " +
  "base-uri 'none'; " +
  "frame-ancestors 'none'; " +
  "form-action 'self'";
const BOOT_SNIPPET =
  `<script nonce="${NONCE}">window.__LOOM_PROXY_AUTH = false;window.__LOOM_AUTH_DISABLED = false;` +
  `window.__LOOM_SELF_SIGNUP = false;window.__LOOM_DEFAULT_NAMESPACE = "local";</script>`;

async function serveUnderDashboardCsp(page: Page) {
  await page.addInitScript(() => {
    window.__cspViolations = [];
    document.addEventListener("securitypolicyviolation", (e) =>
      window.__cspViolations.push(`${e.violatedDirective} ${e.blockedURI}`),
    );
  });
  await page.route("**/me/**", async (r) => {
    if (r.request().resourceType() !== "document") return r.fallback();
    const response = await r.fetch();
    const body = (await response.text()).replace("</head>", `${BOOT_SNIPPET}</head>`);
    return r.fulfill({ response, body, headers: { ...response.headers(), "content-security-policy": DASHBOARD_CSP } });
  });
}

interface Probe {
  exfil: string[];
  dialogs: string[];
}

async function setup(page: Page): Promise<Probe> {
  await installApi(page);
  await serveUnderDashboardCsp(page);
  const probe: Probe = { exfil: [], dialogs: [] };
  const json = (b: unknown) => ({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
  const rows = FIXTURES.map((f) => ({
    source_uri: uri(f),
    title: f.title,
    description: "",
    tags: [],
    ingested_at: "2026-09-19T00:00:00Z",
    has_blob: true,
    blob_size: f.body.length,
    blob_content_type: f.type,
    original_filename: uri(f).split("/")[1],
  }));
  await page.route("**/v1/knowledge.list", (r) => r.fulfill(json({ rows, total: rows.length, next_cursor: null })));
  await page.route("**/v1/knowledge.get", (r) => r.fulfill(json({ found: true, content: "extracted", tags: [] })));
  // On the page (ahead of installApi's catch-all) and on the context, so the
  // standalone pages opened below are answered the same way.
  const context = page.context();
  const download = (r: Route) => {
    const f = FIXTURES.find((x) => uri(x) === new URL(r.request().url()).searchParams.get("source_uri"));
    if (!f) return r.fulfill({ status: 404 });
    return r.fulfill({
      status: 200,
      headers: { "Content-Type": f.type, "Content-Disposition": "inline" },
      body: f.body,
    });
  };
  await page.route("**/v1/knowledge.download?**", download);
  await context.route("**/v1/knowledge.download?**", download);
  await context.route("**/exfil**", (r) => {
    probe.exfil.push(r.request().url());
    return r.fulfill({ status: 204 });
  });
  const watchDialogs = (p: Page) =>
    p.on("dialog", (d) => {
      probe.dialogs.push(d.message());
      void d.dismiss();
    });
  watchDialogs(page);
  context.on("page", watchDialogs);
  return probe;
}

async function openPreview(page: Page, f: Fixture) {
  await page.getByRole("button", { name: `Preview ${f.title}`, exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Loading…")).toHaveCount(0);
  return dialog;
}

async function closePreview(page: Page) {
  await page.getByRole("dialog").getByRole("button", { name: "Close" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
}

async function expectNothingRan(page: Page, probe: Probe) {
  // Give any script that did start a moment to reach the network.
  await page.waitForTimeout(300);
  expect(probe.exfil).toEqual([]);
  expect(probe.dialogs).toEqual([]);
  expect(await page.evaluate(() => window.__pwned)).toBeUndefined();
}

test("active and mismatched uploads never run script in the preview", async ({ page }) => {
  const probe = await setup(page);
  await page.goto("/me/library", { waitUntil: "domcontentloaded" });

  // HTML and SVG: no frame, no image, a download prompt.
  for (const f of FIXTURES.slice(0, 2)) {
    const dialog = await openPreview(page, f);
    await expectNothingRan(page, probe);
    await expect(dialog.getByText("No preview for this file type")).toBeVisible();
    await expect(dialog.locator("iframe")).toHaveCount(0);
    await expect(dialog.locator("img")).toHaveCount(0);
    await closePreview(page);
  }

  // HTML bytes labelled text/plain: shown as text, in a frame that grants nothing.
  const text = FIXTURES[2];
  let dialog = await openPreview(page, text);
  const frame = dialog.locator("iframe");
  await expect(frame).toHaveCount(1);
  expect(await frame.getAttribute("sandbox")).toBe("");
  const body = page.frameLocator("[role=dialog] iframe").locator("body");
  await expect(body).toContainText("<script>");
  await expectNothingRan(page, probe);
  const textUrl = (await frame.getAttribute("src")) ?? "";
  expect(textUrl.startsWith("blob:")).toBe(true);

  // Opening that blob: URL on its own is a plain-text page.
  const standalone = await page.context().newPage();
  await standalone.goto(textUrl);
  await expect(standalone.locator("body")).toContainText("<script>");
  await expectNothingRan(standalone, probe);
  await standalone.close();
  await closePreview(page);

  // HTML bytes labelled image/png: an <img>, which never runs script.
  const png = FIXTURES[3];
  dialog = await openPreview(page, png);
  await expect(dialog.locator("iframe")).toHaveCount(0);
  const imgUrl = (await dialog.locator("img").getAttribute("src")) ?? "";
  expect(imgUrl.startsWith("blob:")).toBe(true);
  await expectNothingRan(page, probe);
  const asPage = await page.context().newPage();
  await asPage.goto(imgUrl);
  await expectNothingRan(asPage, probe);
  await asPage.close();

  // The token is still where it was, unread, and the app itself ran under the policy
  // without tripping it.
  expect(await page.evaluate(() => localStorage.getItem("loom_token_v1"))).toBe("e2e-token");
  expect(await page.evaluate(() => window.__cspViolations)).toEqual([]);
});

test("the download URL opened on its own cannot run script", async ({ page }) => {
  const probe = await setup(page);
  // The server never sends HTML inline (tests/unit/test_knowledge_download_text.py); this
  // shows the policy it adds to every download stops script even if it did.
  await page.context().route("**/v1/knowledge.download?**", (r) =>
    r.fulfill({
      status: 200,
      headers: {
        "Content-Type": "text/html",
        "Content-Security-Policy": "sandbox; default-src 'none'",
        "X-Content-Type-Options": "nosniff",
      },
      body: HTML,
    }),
  );
  await page.goto("/me/library", { waitUntil: "domcontentloaded" });
  const standalone = await page.context().newPage();
  await standalone.goto("/v1/knowledge.download?org=dev&namespace=bench&source_uri=docs/html-page&inline=true");
  await expectNothingRan(standalone, probe);
  await standalone.close();
});
