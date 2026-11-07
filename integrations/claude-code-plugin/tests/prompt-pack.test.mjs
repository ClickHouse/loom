import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { accept, renderPack, searchBody, searchQuery, shouldAsk, SEEN_CAP } from "../lib/prompt-pack.mjs";

const RES = {
  abstain: false,
  retrieval_confidence: 0.81,
  results: [
    { path: "src/A.cpp", start_line: 10, end_line: 40, symbol: "DB::A::run", signature: "void run()" },
    { path: "src/B.cpp", start_line: 5, end_line: 9, symbol: "DB::B::go", signature: "" },
    { path: "src/C.cpp", start_line: 1, end_line: 3, symbol: "", signature: "" },
    { path: "src/D.cpp", start_line: 7, end_line: 8, symbol: "DB::D", signature: "" },
  ],
};

test("shouldAsk skips short prompts and slash commands", () => {
  assert.equal(shouldAsk("yes"), false);
  assert.equal(shouldAsk("/compact"), false);
  assert.equal(shouldAsk("where do we retry s3 uploads"), true);
});

test("accept follows the index's own abstain and confidence", () => {
  assert.equal(accept(RES), true);
  assert.equal(accept({ ...RES, abstain: true }), false);
  assert.equal(accept({ ...RES, retrieval_confidence: 0.3 }), false);
  assert.equal(accept({ ...RES, results: [] }), false);
  assert.equal(accept(null), false);
});

test("renderPack emits at most cap unseen pointers and remembers them", () => {
  const first = renderPack(RES, [], { cap: 3 });
  assert.match(first.text, /confidence 0\.81/);
  assert.match(first.text, /- src\/A.cpp:10-40 DB::A::run — void run\(\)/);
  assert.equal(first.text.split("\n").length, 5, "header + 3 pointers + the instruction");
  assert.match(first.text, /do not run the search again/);
  assert.deepEqual(first.seen, ["src/A.cpp:10-40", "src/B.cpp:5-9", "src/C.cpp:1-3"]);
  const second = renderPack(RES, first.seen, { cap: 3 });
  assert.equal(second.text.split("\n").length, 3, "only the unseen fourth hit");
  assert.match(second.text, /src\/D.cpp:7-8/);
  const third = renderPack(RES, second.seen, { cap: 3 });
  assert.equal(third.text, "", "nothing new -> silence, not a repeat");
});

test("seen list is bounded", () => {
  const seen = Array.from({ length: SEEN_CAP }, (_, i) => `x${i}:1`);
  const out = renderPack(RES, seen, { cap: 3 });
  assert.equal(out.seen.length, SEEN_CAP);
});

test("the index is asked about the request, not about how to answer it", async () => {
  const { searchQuery } = await import("../lib/prompt-pack.mjs");
  const prompt =
    "Below is a question about the repository in the current directory. Find the source files " +
    "that answer it. Do not edit any file and do not run tests or builds.\n\n" +
    'When you are done, end your reply with one line holding a JSON list of at most 3 paths, for example ["src/a.cpp"].\n' +
    "<question>\nTables with ENGINE = Alias don't get parallel replicas reads. What do I need to change?\n</question>";
  assert.equal(
    searchQuery(prompt),
    "Tables with ENGINE = Alias don't get parallel replicas reads. What do I need to change?",
  );
  const plain = "where is the merge selector decided? Do not edit anything.";
  assert.equal(searchQuery(plain), "where is the merge selector decided?");
  assert.equal(searchQuery("why is FINAL slow on ReplacingMergeTree"), "why is FINAL slow on ReplacingMergeTree");
  assert.equal(
    searchQuery("<task>\n\n  why does the merge selector skip small parts?  \n\n</task>"),
    "why does the merge selector skip small parts?",
  );
  // Trimmed, the body is shorter than MIN_PROMPT_CHARS, so the whole prompt is searched.
  assert.equal(
    searchQuery("<task>" + " ".repeat(12) + "fix it</task> in the merge selector code"),
    "<task> fix it</task> in the merge selector code",
  );
  const task = "why does the merge selector skip parts?";
  assert.equal(searchQuery(`<question> see <question> below <task>${task}</task>`), task, "an unclosed tag is skipped");
  assert.equal(searchQuery(`<Task>${task}</TASK>`), task, "tags pair across case");
  assert.equal(
    searchQuery(`<task>x <task>${task}</task> and more</task>`),
    `x <task>${task}`,
    "the first open tag pairs with the nearest close",
  );
});

test("a wrapper tag followed by a long whitespace run and no close tag is matched in linear time", () => {
  const prompt = "please look at this <task>" + " ".repeat(2000) + "why";
  const start = performance.now();
  shouldAsk(prompt);
  searchQuery(prompt);
  assert.ok(performance.now() - start < 500, "the wrapper match backtracks over the whitespace run");
});

test("a question about a change in history gets no code pointers", async () => {
  const { shouldAsk } = await import("../lib/prompt-pack.mjs");
  for (const q of [
    "Which change made HTTP clients get 403 instead of 500 for ACCESS_DENIED?",
    "When did FINAL queries stop using the lazy plan?",
    "since which release does use_partition_pruning also disable the minmax condition?",
    "<question>What PR introduced the S3 schema check?</question>",
  ]) {
    assert.equal(shouldAsk(q), false, q);
  }
  for (const q of [
    "how is an API token authenticated on the chat routes?",
    "where is max_threads applied to the pipeline",
  ]) {
    assert.equal(shouldAsk(q), true, q);
  }
});

test("searchBody sends the prompt redacted: code.search keeps its query in code_search_trace", () => {
  const body = searchBody({ org: "o", namespace: "code-x" }, "why does login fail with password=hunter2 here", {
    topK: 2,
    token: "",
  });
  assert.equal(body.query, "why does login fail with password=[redacted] here");
  assert.equal(body.top_k, 2);
  assert.equal(body.namespace, "code-x");
});
