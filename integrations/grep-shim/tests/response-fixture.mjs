// Control response handling and stalled fetches without HTTP startup timing.
import assert from "node:assert/strict";
import fs from "node:fs";

const mode = process.env.LOOM_TEST_RESPONSE_MODE;
assert.ok(mode === "401" || mode === "garbage" || mode === "hang");
const record = process.env.LOOM_TEST_RESPONSE_RECORD;
const seen = {
  requests: [],
  requestValidated: false,
  jsonCalls: 0,
  jsonRejected: false,
  pendingEntered: false,
  signalAborted: false,
  abortName: "",
  watchdogExpired: false,
};
const save = () => fs.writeFileSync(record, JSON.stringify(seen));
const response = new Response(mode === "401" ? "{}" : "{not json", {
  status: mode === "401" ? 401 : 200,
  headers: { "content-type": "application/json" },
});
const parse = response.json.bind(response);
response.json = async () => {
  seen.jsonCalls++;
  save();
  try {
    return await parse();
  } catch (error) {
    seen.jsonRejected = true;
    save();
    throw error;
  }
};

globalThis.fetch = async (url, options) => {
  seen.requests.push(url);
  save();
  assert.equal(url, "http://response.invalid/v1/code.enclosing");
  assert.equal(options.method, "POST");
  assert.equal(options.headers.authorization, "Bearer t");
  assert.ok(JSON.parse(options.body).locations.length > 0);
  options.signal.throwIfAborted();
  seen.requestValidated = true;
  save();
  if (mode === "hang") {
    seen.pendingEntered = true;
    save();
    return new Promise((_, reject) => {
      const onAbort = () => {
        clearTimeout(watchdog);
        seen.signalAborted = options.signal.aborted;
        seen.abortName = options.signal.reason?.name || "";
        save();
        reject(options.signal.reason);
      };
      // This test watchdog keeps Node alive; only the shim's signal proves its deadline.
      const watchdog = setTimeout(() => {
        options.signal.removeEventListener("abort", onAbort);
        seen.watchdogExpired = true;
        save();
        reject(new Error("stalled fetch did not receive the shim's abort"));
      }, 3000);
      options.signal.addEventListener("abort", onAbort, { once: true });
    });
  }
  return response;
};
