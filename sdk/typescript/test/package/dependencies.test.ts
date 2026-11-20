import { createRequire } from "node:module";
import { expect, it } from "vitest";

const require = createRequire(import.meta.url);

it("the n8n framework dependency rejects undersized UUID output buffers", () => {
  const frameworkRequire = createRequire(require.resolve("@langchain/classic/package.json"));
  const { v5 } = frameworkRequire("uuid");
  expect(() => v5("memory", "6ba7b810-9dad-11d1-80b4-00c04fd430c8", new Uint8Array(8), 4)).toThrow(RangeError);
});
