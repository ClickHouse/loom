const assert = require("node:assert/strict");
const { once } = require("node:events");
const { createRequire } = require("node:module");
const { Readable } = require("node:stream");
const { test } = require("node:test");

// Resolve the parser used by the development toolchain, not a separate test dependency.
let toolRequire = createRequire(require.resolve("@n8n/node-cli/eslint"));
for (const dependency of ["@n8n/ai-node-sdk", "@n8n/ai-utilities", "@n8n/backend-network", "@n8n/backend-common"]) {
  toolRequire = createRequire(toolRequire.resolve(dependency));
}
const Assembler = toolRequire("stream-json/Assembler");
const { parser } = toolRequire("stream-json");

async function assemble(text, options) {
  const tokens = parser();
  const assembler = Assembler.connectTo(tokens, options);
  Readable.from([text]).pipe(tokens);
  await once(tokens, "end");
  return assembler.current;
}

test("the toolchain parser preserves __proto__ as an own data property", async () => {
  const vectors = [
    '{"__proto__":{"isAdmin":true},"name":"item"}',
    '{"__proto__":null,"name":"item"}',
    '{"__proto__":{"hasOwnProperty":1},"name":"item"}',
    '{"nested":{"__proto__":{"isAdmin":true},"name":"item"}}',
  ];
  for (const options of [undefined, { reviver: (_key, value) => value }]) {
    for (const text of vectors) {
      const actual = await assemble(text, options);
      const expected = JSON.parse(text);
      assert.deepEqual(actual, expected);
      const inner = actual.nested ?? actual;
      assert.equal(Object.getPrototypeOf(inner), Object.prototype);
      assert.equal(Object.hasOwn(inner, "__proto__"), true);
      assert.equal(inner.isAdmin, undefined);
      assert.equal(typeof inner.hasOwnProperty, "function");
      const descriptor = Object.getOwnPropertyDescriptor(inner, "__proto__");
      assert.equal(descriptor.enumerable, true);
      assert.equal(descriptor.writable, true);
      assert.equal(descriptor.configurable, true);
    }
  }
  assert.equal(Object.prototype.isAdmin, undefined);
});

test("the parser retains ordinary values and reviver transformation and deletion", async () => {
  for (const text of ['{"keep":2,"items":[3,null,true,false],"nested":{"value":"x"}}', '["x",2,null]', '"x"']) {
    assert.deepEqual(await assemble(text), JSON.parse(text));
  }
  const text = '{"keep":2,"remove":1,"items":[3,4],"nested":{"value":5}}';
  const reviver = (key, value) => {
    if (key === "remove") return undefined;
    return typeof value === "number" ? value + 1 : value;
  };
  assert.deepEqual(await assemble(text, { reviver }), JSON.parse(text, reviver));
});
