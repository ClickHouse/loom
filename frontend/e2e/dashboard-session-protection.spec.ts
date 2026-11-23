import { expect, test, type BrowserContext, type Page } from "@playwright/test";
import { fileURLToPath } from "node:url";
import { build } from "vite";
import { mcpConnection, mcpDiscovery, mcpPreviewValue, mcpProfile, mcpResult, mcpSource } from "../src/test/mcpSources";

type HarnessWindow = Window & {
  DashboardProtection: typeof import("./sessionProtectionHarness");
};

const DATABASE = "loom-dashboard-session-protection";
const WITNESS = "loom-dashboard-session-protection-v1";
let bundle: string;

// Compile the production modules, then execute them against real browser storage.
// This suite runs only in the hosted frontend environment with its normal dependencies.
test.beforeAll(async () => {
  const result = await build({
    configFile: false,
    logLevel: "error",
    define: { "process.env.NODE_ENV": JSON.stringify("production") },
    build: {
      rollupOptions: { output: { inlineDynamicImports: true } },
      write: false,
      minify: false,
      target: "esnext",
      lib: {
        entry: fileURLToPath(new URL("./sessionProtectionHarness.ts", import.meta.url)),
        name: "DashboardProtection",
        formats: ["iife"],
      },
    },
  });
  const outputs = Array.isArray(result) ? result : [result];
  const chunks = outputs.flatMap((output) => "output" in output
    ? output.output.filter((chunk) => chunk.type === "chunk") : []);
  expect(chunks).toHaveLength(1);
  const chunk = chunks[0];
  if (!chunk || chunk.type !== "chunk") throw new Error("protection harness did not compile to one script");
  bundle = chunk.code;
});

async function installPage(context: BrowserContext, page: Page) {
  await context.route("**/protection-test", (route) => route.fulfill({
    contentType: "text/html", body: "<!doctype html><title>Session protection</title>",
  }));
  await page.goto("/protection-test");
  await page.addScriptTag({ content: bundle });
}

test("actual POST observes private responses, keeps explicit intent local and persists across tabs/reload", async ({ context, page }) => {
  const requests: { path: string; declaration: string | undefined }[] = [];
  await context.route("**/v1/**", async (route) => {
    requests.push({ path: new URL(route.request().url()).pathname,
      declaration: route.request().headers()["x-loom-session-protection"] });
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({ mirrored: true, text: "private" }) });
  });
  await installPage(context, page);
  const first = await page.evaluate(async (witness) => {
    const { post, mcpRead, DashboardSessionProtectionStore } = (window as HarnessWindow).DashboardProtection;
    const response = await mcpRead({ org: "one", namespace: "one", name: "source", resource_id: "private" });
    const store = await DashboardSessionProtectionStore.open();
    let automatic: string | undefined;
    try { await post("/v1/memory.set", { namespace: "two", session_id: "new", value: "private" }); }
    catch (error) { automatic = (error as Error).name; }
    return { response, automatic, state: await store.read(), family: localStorage.getItem(witness),
      storage: Object.values(localStorage) };
  }, WITNESS);
  expect(first.response).toEqual({ mirrored: true, text: "private" });
  expect(first.automatic).toBe("AutomaticCaptureSuppressed");
  expect(first.state).toBe("protected");
  expect(first.family).toBeTruthy();
  expect(first.storage).toEqual([first.family]);
  expect(requests).toEqual([{ path: "/v1/knowledge.source.mcp_read", declaration: "v1" }]);

  const second = await context.newPage();
  await second.goto("/protection-test");
  await second.addScriptTag({ content: bundle });
  const restored = async (target: Page) => target.evaluate(async (witness) => {
    const { post, DashboardSessionProtectionStore } = (window as HarnessWindow).DashboardProtection;
    const state = await (await DashboardSessionProtectionStore.open()).read();
    try { await post("/v1/conversation.create", { namespace: "another", thread_id: "new" }); }
    catch (error) { return { state, family: localStorage.getItem(witness), error: (error as Error).name }; }
    throw new Error("automatic write escaped");
  }, WITNESS);
  expect(await restored(second)).toEqual({ state: "protected", family: first.family, error: "AutomaticCaptureSuppressed" });
  await page.reload();
  await page.addScriptTag({ content: bundle });
  expect(await restored(page)).toEqual({ state: "protected", family: first.family, error: "AutomaticCaptureSuppressed" });
  expect(requests).toHaveLength(1);
});

test("real source controls and ordinary manual submit dispatch through one durable protected family", async ({ context, page }) => {
  const sent: { op: string; body: Record<string, unknown>; declaration: string | undefined }[] = [];
  await context.route("**/v1/**", async (route) => {
    const request = route.request();
    const op = new URL(request.url()).pathname.slice(4);
    sent.push({ op, body: request.postDataJSON(), declaration: request.headers()["x-loom-session-protection"] });
    const responses: Record<string, object> = {
      "knowledge.source.mcp_profiles": { profiles: [mcpProfile], next_cursor: null, self_service_enabled: true },
      "knowledge.connection.mcp_list": { connections: [], next_cursor: null },
      "knowledge.connection.mcp_connect": { connection: mcpConnection },
      "knowledge.connection.mcp_inspect": { found: true, connection: mcpConnection, reconciliation: "present" },
      "knowledge.connection.mcp_discover": mcpDiscovery,
      "knowledge.source.mcp_preview": mcpPreviewValue,
      "knowledge.source.mcp_add": mcpResult,
      "collection.list": { collections: [{ namespace: "col-team", title: "Team", parent: "", personal: false, role: "admin" }], next_cursor: null },
      "knowledge.connection.mcp_browse": { items: [], limitations: [], supported: true, complete: false, truncated: false },
      "namespace.taxonomy": { tags: [], entities: [], communities: [] },
      "memory.list": { rows: [], total: 0 },
      "memory.set": { version: 1, pinned: false },
    };
    expect(Object.hasOwn(responses, op)).toBe(true);
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({ ...responses[op], mirrored: true }) });
  });
  await installPage(context, page);
  await page.evaluate(() => {
    const unmount = (window as HarnessWindow).DashboardProtection.mountSourceSetup();
    Object.assign(window, { unmountSource: unmount });
  });
  await page.getByRole("radio", { name: "Approved remote server" }).click();
  await page.getByLabel("Preissued upstream bearer").fill("selected-secret");
  await page.getByRole("button", { name: "Connect a new account" }).click();
  await page.getByRole("button", { name: "Discover capabilities" }).click();
  await page.getByLabel("Explicit resource IDs (one per line)").fill(mcpSource.resource_ids[0] ?? "");
  await page.getByRole("radio", { name: "Selected snapshot" }).click();
  await page.getByRole("radio", { name: "Team (col-team)" }).click();
  await page.getByRole("button", { name: "Review effective access" }).click();
  await expect(page.getByRole("region", { name: "Effective access preview" })).toBeVisible();
  await page.getByRole("button", { name: "Register source" }).click();
  await expect(page.getByText(/Server registration state: registered/)).toBeVisible();
  expect(sent.filter((r) => r.op === "knowledge.source.mcp_add")).toEqual([{
    op: "knowledge.source.mcp_add", declaration: "v1", body: expect.objectContaining({
      namespace: "col-team", reader_policy: "owner_only", confirm_copy_authority: false, resource_ids: mcpSource.resource_ids,
    }),
  }]);
  const family = await page.evaluate((key) => localStorage.getItem(key), WITNESS);
  await page.evaluate(async () => {
    const api = (window as HarnessWindow).DashboardProtection;
    await api.post("/v1/memory.list", { namespace: "another" });
    (window as Window & { unmountSource: () => void }).unmountSource();
    api.mountMemory();
  });
  await page.getByLabel("Key (lower_snake_case)").fill("manual_value");
  await page.getByLabel("Value", { exact: true }).fill("human supplied value");
  await page.getByRole("button", { name: "Add memory" }).click();
  await expect.poll(() => sent.filter((r) => r.op === "memory.set").length).toBe(1);
  expect(sent.find((r) => r.op === "memory.set")).toEqual({ op: "memory.set", declaration: "v1",
    body: expect.objectContaining({ memory_key: "manual_value", value: "human supplied value" }) });
  const count = sent.length;
  expect(await page.evaluate(async () => {
    try { await (window as HarnessWindow).DashboardProtection.post("/v1/memory.set", { namespace: "other", session_id: "new" }); }
    catch (error) { return (error as Error).name; }
    throw new Error("automatic write escaped");
  })).toBe("AutomaticCaptureSuppressed");
  expect(sent).toHaveLength(count);
  expect(sent.every((r) => r.declaration === "v1")).toBe(true);
  expect(await page.evaluate((key) => localStorage.getItem(key), WITNESS)).toBe(family);
});

test("actual GET JSON and opaque historical downloads remain observed", async ({ context, page }) => {
  await context.route("**/v1/**", async (route) => {
    expect(route.request().headers()["x-loom-session-protection"]).toBe("v1");
    await route.fulfill(route.request().url().includes("knowledge.download")
      ? { contentType: "text/plain", body: "historical original bytes" }
      : { contentType: "application/json", body: '{"mirrored":true,"found":true}' });
  });
  await installPage(context, page);
  expect(await page.evaluate(async () => {
    const api = (window as HarnessWindow).DashboardProtection;
    const original = await api.getResponse("/v1/knowledge.download?revision=1&original=true");
    const bytes = await original.text();
    const json = await api.getJson("/v1/me/sharing-summary");
    return { bytes, json, state: await (await api.DashboardSessionProtectionStore.open()).read() };
  })).toEqual({ bytes: "historical original bytes", json: { mirrored: true, found: true }, state: "protected" });
});

test("missing durable row refuses explicit POST before and after restore", async ({ context, page }) => {
  let requests = 0;
  await context.route("**/v1/**", async (route) => {
    requests++;
    await route.fulfill({ contentType: "application/json", body: "{}" });
  });
  await installPage(context, page);
  const failure = await page.evaluate(async (database) => {
    const { post } = (window as HarnessWindow).DashboardProtection;
    await post("/v1/memory.get", {}, undefined, "explicit");
    await new Promise<void>((resolve, reject) => {
      const opening = indexedDB.open(database, 1);
      opening.onerror = () => reject(opening.error);
      opening.onsuccess = () => {
        const transaction = opening.result.transaction("authority", "readwrite", { durability: "strict" });
        transaction.objectStore("authority").delete("dashboard");
        transaction.oncomplete = () => { opening.result.close(); resolve(); };
        transaction.onabort = () => reject(transaction.error);
      };
    });
    try { await post("/v1/memory.set", {}, undefined, "explicit"); }
    catch (error) { return (error as Error).name; }
    throw new Error("lost proof accepted");
  }, DATABASE);
  expect(failure).toBe("SessionProtectionUnavailable");
  await page.reload();
  await page.addScriptTag({ content: bundle });
  const restored = await page.evaluate(async () => {
    try { await (window as HarnessWindow).DashboardProtection.post("/v1/memory.set", {}, undefined, "explicit"); }
    catch (error) { return (error as Error).name; }
    throw new Error("lost proof was reinitialized");
  });
  expect(restored).toBe("SessionProtectionUnavailable");
  expect(requests).toBe(1);
});

test("missing witness refuses restore instead of creating another family", async ({ context, page }) => {
  await installPage(context, page);
  await page.evaluate(async (witness) => {
    await (window as HarnessWindow).DashboardProtection.DashboardSessionProtectionStore.open();
    localStorage.removeItem(witness);
  }, WITNESS);
  await page.reload();
  await page.addScriptTag({ content: bundle });
  expect(await page.evaluate(async () => {
    try { await (window as HarnessWindow).DashboardProtection.DashboardSessionProtectionStore.open(); }
    catch (error) { return (error as Error).name; }
    throw new Error("missing witness accepted");
  })).toBe("SessionProtectionUnavailable");
});

test("complete browser storage loss never provisions automatic-write permission", async ({ context, page }) => {
  await installPage(context, page);
  await page.evaluate(async () => {
    await (window as HarnessWindow).DashboardProtection.DashboardSessionProtectionStore.open();
  });
  // Leave this document so its database handles close before browser-owned deletion.
  await page.goto("about:blank");
  await page.goto("/protection-test");
  await page.evaluate(async (database) => {
    localStorage.clear();
    await new Promise<void>((resolve, reject) => {
      const deletion = indexedDB.deleteDatabase(database);
      deletion.onsuccess = () => resolve();
      deletion.onerror = () => reject(deletion.error);
      deletion.onblocked = () => reject(new Error("browser still owns an open handle"));
    });
  }, DATABASE);
  await page.addScriptTag({ content: bundle });
  expect(await page.evaluate(async () => {
    try { await (window as HarnessWindow).DashboardProtection.post("/v1/memory.set", {}); }
    catch (error) { return (error as Error).name; }
    throw new Error("storage loss created clean permission");
  })).toBe("AutomaticCaptureSuppressed");
});

for (const damage of [{ state: "clean" }, { version: 2 }, { family: "other" }, { text: "private" }]) {
  test(`damaged durable authority refuses dispatch: ${JSON.stringify(damage)}`, async ({ context, page }) => {
    let requests = 0;
    await context.route("**/v1/**", async (route) => {
      requests++;
      await route.fulfill({ contentType: "application/json", body: "{}" });
    });
    await installPage(context, page);
    const failure = await page.evaluate(async ({ database, damage }) => {
      const { post } = (window as HarnessWindow).DashboardProtection;
      await post("/v1/memory.get", {}, undefined, "explicit");
      await new Promise<void>((resolve, reject) => {
        const opening = indexedDB.open(database, 1);
        opening.onerror = () => reject(opening.error);
        opening.onsuccess = () => {
          const transaction = opening.result.transaction("authority", "readwrite", { durability: "strict" });
          const store = transaction.objectStore("authority");
          const read = store.get("dashboard");
          read.onsuccess = () => store.put({ ...read.result, ...damage }, "dashboard");
          transaction.oncomplete = () => { opening.result.close(); resolve(); };
          transaction.onabort = () => reject(transaction.error);
        };
      });
      try { await post("/v1/memory.set", {}, undefined, "explicit"); }
      catch (error) { return (error as Error).name; }
      throw new Error("damaged proof accepted");
    }, { database: DATABASE, damage });
    expect(failure).toBe("SessionProtectionUnavailable");
    expect(requests).toBe(1);
  });
}

test("actual error/status and stream paths are observed; file writes remain guarded", async ({ context, page }) => {
  await context.route("**/v1/**", async (route) => {
    if (route.request().url().endsWith("/assistant.chat")) {
      await route.fulfill({ contentType: "text/event-stream", body: 'data: {"mirrored":true,"delta":"private"}\n\n' });
    } else {
      await route.fulfill({ status: 503, contentType: "application/json", body: '{"mirrored":true,' });
    }
  });
  await installPage(context, page);
  const result = await page.evaluate(async () => {
    const { post, postStreaming, postFile } = (window as HarnessWindow).DashboardProtection;
    let error: string | undefined;
    try { await post("/v1/knowledge.source.mcp_read", {}, undefined, "explicit"); }
    catch (failure) { error = (failure as Error).name; }
    const stream = await postStreaming("/v1/assistant.chat", {}, {}, new AbortController().signal, "explicit");
    const text = await stream.text();
    const unread = await postStreaming("/v1/assistant.chat", {}, {}, new AbortController().signal, "explicit");
    await unread.body?.cancel();
    let file: string | undefined;
    try { await postFile("/v1/knowledge.upload", new Blob(["private"])); }
    catch (failure) { file = (failure as Error).name; }
    return { error, text, file };
  });
  expect(result).toEqual({ error: "SessionProtectionUnavailable",
    text: 'data: {"mirrored":true,"delta":"private"}\n\n', file: "AutomaticCaptureSuppressed" });
});
