const assert = require("node:assert/strict");
const { createRequire } = require("node:module");
const { pathToFileURL } = require("node:url");
const { test } = require("node:test");

const workflowRequire = createRequire(require.resolve("n8n-workflow"));

for (const entry of ["zod", "zod/v3", "zod/v4"]) {
  for (const format of ["require", "import"]) {
    test(`${entry} through ${format} rejects oversized containers before visiting elements`, async () => {
      const module =
        format === "require"
          ? workflowRequire(entry)
          : await import(pathToFileURL(workflowRequire.resolve(entry).replace(/\.cjs$/, ".js")).href);
      const z = module.z;
      let visited = 0;
      let refined = 0;
      const element = z.preprocess((value) => {
        visited += 1;
        return value;
      }, z.string());
      const cases = [
        [z.array(element).max(2), [1, 2, 3]],
        [z.array(element).length(2), [1, 2, 3]],
        [z.set(element).max(2), new Set([1, 2, 3])],
        [z.set(element).size(2), new Set([1, 2, 3])],
        [z.object({ items: z.array(element).max(2) }), { items: [1, 2, 3] }],
      ];
      if (entry === "zod/v4") {
        cases.push([
          z.map(element, element).check(z.maxSize(2)),
          new Map([
            [1, 1],
            [2, 2],
            [3, 3],
          ]),
        ]);
      }
      for (const [container, input] of cases) {
        const schema = container.refine(() => {
          refined += 1;
          return true;
        });
        for (const parse of ["safeParse", "safeParseAsync"]) {
          visited = refined = 0;
          const result = await schema[parse](input);
          assert.equal(result.success, false);
          assert.deepEqual(
            result.error.issues.map((issue) => issue.code),
            ["too_big"],
          );
          assert.equal(visited, 0);
          assert.equal(refined, 0);
        }
      }
      assert.deepEqual(z.array(element).max(2).parse(["a", "b"]), ["a", "b"]);
      assert.equal(z.array(z.string()).safeParse([1, 2, 3]).error.issues.length, 3);
      assert.equal(z.array(z.string()).min(1).safeParse([]).error.issues[0].code, "too_small");
      assert.equal(z.array(z.string()).max(2).safeParse("bad").error.issues[0].code, "invalid_type");
      assert.equal(
        z.intersection(z.array(z.string()).max(2), z.array(z.unknown())).safeParse([1, 2, 3]).success,
        false,
      );
      if (entry === "zod/v4") {
        assert.deepEqual(
          z
            .array(z.number())
            .overwrite((values) => values.slice(0, 2))
            .max(2)
            .parse([1, 2, 3]),
          [1, 2],
        );
      }
    });
  }
}

for (const entry of ["zod/v4-mini", "zod/v4/mini"]) {
  for (const format of ["require", "import"]) {
    test(`${entry} through ${format} rejects oversized containers before visiting elements`, async () => {
      const z =
        format === "require"
          ? workflowRequire(entry)
          : await import(pathToFileURL(workflowRequire.resolve(entry).replace(/\.cjs$/, ".js")).href);
      let visited = 0;
      const element = z.custom((value) => {
        visited += 1;
        return typeof value === "string";
      });
      const cases = [
        [z.array(element).check(z.maxLength(2)), [1, 2, 3]],
        [z.set(element).check(z.maxSize(2)), new Set([1, 2, 3])],
        [
          z.map(element, element).check(z.maxSize(2)),
          new Map([
            [1, 1],
            [2, 2],
            [3, 3],
          ]),
        ],
      ];
      for (const [schema, input] of cases) {
        for (const parse of ["safeParse", "safeParseAsync"]) {
          visited = 0;
          const result = await z[parse](schema, input);
          assert.equal(result.success, false);
          assert.deepEqual(
            result.error.issues.map((issue) => issue.code),
            ["too_big"],
          );
          assert.equal(visited, 0);
        }
      }
      assert.deepEqual(z.parse(z.array(z.string()).check(z.maxLength(2)), ["a", "b"]), ["a", "b"]);
    });
  }
}
