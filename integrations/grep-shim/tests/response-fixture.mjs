// Response-path tests replace transport so the real deadline cannot hide a parser branch.
import assert from "node:assert/strict";
import fs from "node:fs";

const mode = process.env.LOOM_TEST_RESPONSE_MODE;
assert.ok(mode === "401" || mode === "garbage");
const record = process.env.LOOM_TEST_RESPONSE_RECORD;
const seen = { requests: [], requestValidated: false, jsonCalls: 0, jsonRejected: false };
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
  return response;
};
