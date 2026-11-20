import "./temp-home.mjs";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, test } from "node:test";

import { batchKind, formatShuntBatchLine, formatShuntLine, parseShuntBatch } from "../lib/shunt-ledger.mjs";
import { STALE_STATE_MS, sessionStatePath, sweepStaleStateFiles, tmpStatePath } from "../lib/tmp-state.mjs";

// These descriptors exercise serialization; only the hook checks live authority.
const rootA = Object.freeze({
  base: path.resolve(os.tmpdir(), "loom-shunt-fixture-a"),
  id: "11111111-1111-4111-8111-111111111111",
});
const rootB = Object.freeze({ ...rootA, id: "22222222-2222-4222-8222-222222222222" });
const rootC = Object.freeze({ ...rootA, base: path.resolve(os.tmpdir(), "loom-shunt-fixture-c") });
const scope = { serverUrl: "https://loom.example", token: "fixture-token", org: "o", namespace: "code-x" };
const hit = formatShuntLine({
  ts: "2025-01-01T00:00:00.000Z",
  decision: "hit",
  tool: "Read",
  tokensSaved: 100,
  path: "src/a.cpp",
  startLine: 1,
  endLine: 40,
});
const pass = formatShuntLine({
  ts: "2025-01-01T00:00:01.000Z",
  decision: "pass",
  tool: "Read",
  passReason: "modified",
  path: "src/b.cpp",
  startLine: 2,
  endLine: 8,
});
const row = (value) => JSON.stringify(value) + "\n";
const envelope = (changes = {}) => ({ version: 1, protectionRoots: [rootA], line: hit, ...changes });

const owned = new Set();
afterEach(() => {
  for (const dir of owned) fs.rmSync(dir, { recursive: true, force: true });
  owned.clear();
});

test("a controlled batch retains the union of every producer's immutable roots", () => {
  const first = formatShuntBatchLine(hit, [rootA, rootB]);
  const second = formatShuntBatchLine(pass, [rootB, rootC]);
  assert.equal(first, row(envelope({ protectionRoots: [rootA, rootB] })));
  assert.equal(second, row(envelope({ protectionRoots: [rootB, rootC], line: pass })));
  const parsed = parseShuntBatch(first + second);
  assert.deepEqual(parsed, { roots: [rootA, rootB, rootC], text: hit + pass, lines: 2 });
  assert.equal(Object.isFrozen(parsed), true);
  assert.equal(Object.isFrozen(parsed.roots), true);
  assert.equal(parsed.roots.every(Object.isFrozen), true);
  assert.throws(() => parsed.roots.push(rootA), TypeError);
  assert.throws(() => {
    parsed.roots[0].id = rootB.id;
  }, TypeError);
});

test("serialized producer roots cannot be replaced by later input mutation", () => {
  const producer = { ...rootA };
  const roots = [producer];
  const queued = formatShuntBatchLine(hit, roots);
  producer.id = rootB.id;
  producer.base = rootC.base;
  roots.splice(0, 1, { ...rootB });
  assert.deepEqual(parseShuntBatch(queued), { roots: [rootA], text: hit, lines: 1 });
});

test("legacy TSV and a valid row beside any unattributed or damaged row confer no authority", () => {
  const valid = formatShuntBatchLine(hit, [rootA]);
  for (const unknown of [hit, "not json\n", "null\n", "[]\n", row({ line: pass }), valid.slice(0, -2)]) {
    assert.equal(parseShuntBatch(unknown), null, unknown);
    assert.equal(parseShuntBatch(valid + unknown), null, unknown);
    assert.equal(parseShuntBatch(unknown + valid), null, unknown);
  }
  assert.equal(parseShuntBatch(""), null);
  assert.equal(parseShuntBatch(valid.slice(0, -1)), null, "an incomplete final record is retained");
});

test("batch parsing rejects unknown fields, versions, roots and malformed TSV lines", () => {
  const withoutVersion = envelope();
  delete withoutVersion.version;
  const withoutRoots = envelope();
  delete withoutRoots.protectionRoots;
  const withoutLine = envelope();
  delete withoutLine.line;
  const damaged = [
    withoutVersion,
    withoutRoots,
    withoutLine,
    envelope({ version: 2 }),
    envelope({ version: "1" }),
    envelope({ extra: true }),
    envelope({ protectionRoots: [] }),
    envelope({ protectionRoots: null }),
    envelope({ protectionRoots: [null] }),
    envelope({ protectionRoots: [{ id: rootA.id }] }),
    envelope({ protectionRoots: [{ base: rootA.base }] }),
    envelope({ protectionRoots: [{ ...rootA, base: "relative" }] }),
    envelope({ protectionRoots: [{ ...rootA, id: "not-a-root" }] }),
    envelope({ protectionRoots: [{ ...rootA, id: 42 }] }),
    envelope({ protectionRoots: [{ ...rootA, authority: true }] }),
    envelope({ line: 42 }),
    envelope({ line: "" }),
    envelope({ line: "missing\tcells\n" }),
    envelope({ line: hit.slice(0, -1) }),
    envelope({ line: hit + pass }),
  ];
  const valid = formatShuntBatchLine(pass, [rootB]);
  for (const value of damaged) {
    const encoded = row(value);
    assert.equal(parseShuntBatch(encoded), null, encoded);
    assert.equal(parseShuntBatch(valid + encoded), null, encoded);
  }
});

test("a producer cannot format an unattributed or malformed batch row", () => {
  for (const roots of [undefined, null, [], [null], [{ ...rootA, base: "relative" }], [{ ...rootA, extra: true }]]) {
    assert.equal(formatShuntBatchLine(hit, roots), null);
  }
  for (const line of [undefined, null, "", hit.slice(0, -1), hit + pass, "not\tnine\tcells\n"]) {
    assert.equal(formatShuntBatchLine(line, [rootA]), null);
  }
});

test("a controlled queue name cannot match an old consumer's pending selector", () => {
  const kind = batchKind(scope);
  assert.match(kind, /^shunt-protected-[0-9a-f]{12}$/);
  const file = path.basename(sessionStatePath(kind, "session-a", { ext: ".log.control" }));
  const oldKind = kind.replace("shunt-protected-", "shunt-pending-");
  assert.equal(file.startsWith(`loom-${oldKind}-`) && file.endsWith(".log"), false);
  assert.match(file, /^loom-shunt-protected-[0-9a-f]{12}-[0-9a-f]{12}\.log\.control$/);
  const oldStateFileRe =
    /^loom-(?!shunt-log-)(?:[a-z0-9-]+-[0-9a-f]{12}(?:\.[a-z]+)?|(?:recalled|code-trace)-[\w-]+\.json)$/;
  assert.equal(oldStateFileRe.test(file), false, "an old TTL sweeper cannot delete a controlled queue");
  const claim = path.basename(tmpStatePath("shunt-protected-claim", "batch", { ext: ".log.control" }));
  assert.equal(oldStateFileRe.test(claim), false, "an old TTL sweeper cannot delete a controlled claim");
  assert.equal(file.includes(scope.token), false);
});

test("the TTL sweep retains pending, controlled and claimed batches while ordinary state expires", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-shunt-sweep-"));
  owned.add(dir);
  const now = Date.now();
  const old = new Date(now - 2 * STALE_STATE_MS);
  const kind = batchKind(scope);
  const retained = [
    sessionStatePath(kind.replace("shunt-protected-", "shunt-pending-"), "old-session", { ext: ".log", tmp: dir }),
    sessionStatePath(kind, "protected-session", { ext: ".log.control", tmp: dir }),
    sessionStatePath(kind, "old-protected-session", { ext: ".log", tmp: dir }),
    tmpStatePath("shunt-claim", "old-claimed", { ext: ".log", tmp: dir }),
    tmpStatePath("shunt-protected-claim", "controlled-claim", { ext: ".log.control", tmp: dir }),
  ];
  for (const file of retained) {
    fs.writeFileSync(file, "unacknowledged batch\n", { mode: 0o600 });
    fs.utimesSync(file, old, old);
  }
  const expired = sessionStatePath("file-context", "expired", { tmp: dir });
  const fresh = sessionStatePath("run", "active", { tmp: dir });
  fs.writeFileSync(expired, "{}", { mode: 0o600 });
  fs.writeFileSync(fresh, "{}", { mode: 0o600 });
  fs.utimesSync(expired, old, old);
  fs.utimesSync(fresh, new Date(now), new Date(now));
  assert.equal(sweepStaleStateFiles({ tmp: dir, now }), 1);
  assert.equal(fs.existsSync(expired), false);
  assert.equal(fs.readFileSync(fresh, "utf8"), "{}");
  for (const file of retained) assert.equal(fs.readFileSync(file, "utf8"), "unacknowledged batch\n");
});
