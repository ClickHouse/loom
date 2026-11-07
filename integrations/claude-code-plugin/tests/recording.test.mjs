import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  RECORDING_TTL_MS,
  readRecording,
  recordingCachePath,
  recordingFromAppends,
  recordingFromNamespace,
  recordingPrimerLine,
  writeRecording,
} from "../lib/recording.mjs";

const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), "loom-rec-"));

test("an append reply that kept nothing because recording is off reads as off", () => {
  assert.equal(recordingFromAppends([{ msg_id: "a", stored: false, reason: "logging_disabled" }]), false);
  assert.equal(
    recordingFromAppends([
      { msg_id: "a", stored: true, reason: "" },
      { msg_id: "b", stored: true, reason: "duplicate" },
    ]),
    true,
  );
  // a reply without `stored` says nothing
  assert.equal(recordingFromAppends([{ msg_id: "a" }]), null);
  // failures say nothing
  assert.equal(recordingFromAppends([null, null]), null);
  assert.equal(recordingFromAppends([]), null);
});

test("the switch from namespace.get", () => {
  assert.equal(recordingFromNamespace({ found: true, conversation_logging_enabled: false }), false);
  assert.equal(recordingFromNamespace({ found: true, conversation_logging_enabled: true }), true);
  assert.equal(recordingFromNamespace({ found: false }), null);
  assert.equal(recordingFromNamespace(null), null);
  assert.equal(recordingFromNamespace({ found: true }), null);
});

test("the cache is per server + org + namespace and expires", () => {
  const dir = tmp();
  const now = 1_000_000;
  writeRecording("https://a", "acme", "ns1", false, { now, tmp: dir });
  assert.equal(readRecording("https://a", "acme", "ns1", { now: now + 1000, tmp: dir }), false);
  assert.equal(readRecording("https://a", "acme", "ns2", { now: now + 1000, tmp: dir }), null);
  assert.equal(readRecording("https://b", "acme", "ns1", { now: now + 1000, tmp: dir }), null);
  assert.equal(readRecording("https://a", "acme", "ns1", { now: now + RECORDING_TTL_MS + 1, tmp: dir }), null);
  // unknown is never written; a garbage file reads as unknown
  writeRecording("https://a", "acme", "ns3", null, { now, tmp: dir });
  assert.equal(fs.existsSync(recordingCachePath("https://a", "acme", "ns3", dir)), false);
  fs.writeFileSync(recordingCachePath("https://a", "acme", "ns4", dir), "{nope");
  assert.equal(readRecording("https://a", "acme", "ns4", { now, tmp: dir }), null);
});

test("the primer states the switch instead of claiming automatic logging", () => {
  const off = recordingPrimerLine(false, "agent-x");
  assert.match(
    off,
    /conversation recording is off for agent-x \(the owner's Recording switch in the dashboard\); turns are not kept/,
  );
  assert.doesNotMatch(off, /automatic/);
  assert.match(recordingPrimerLine(true, "agent-x"), /conversation logging is automatic \(handled by the Stop hook\)/);
  assert.match(recordingPrimerLine(null, "agent-x"), /records this conversation when recording is on for agent-x/);
  for (const on of [true, false, null]) {
    assert.match(
      recordingPrimerLine(on, "n"),
      /Do NOT call loom_conversation_create \/ loom_conversation_append yourself/,
    );
  }
});

import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { hostEnv } from "./host-env.mjs";

test("the Stop hook sends no turns while recording is cached off, and moves past them", () => {
  const dir = tmp();
  const home = tmp();
  const hook = fileURLToPath(new URL("../hooks/loom-conversation.mjs", import.meta.url));
  const transcript = path.join(dir, "t.jsonl");
  fs.writeFileSync(
    transcript,
    [
      { type: "user", uuid: "u1", message: { role: "user", content: "hello loom" } },
      {
        type: "assistant",
        uuid: "a1",
        message: { role: "assistant", content: [{ type: "text", text: "hello back" }] },
      },
    ]
      .map((o) => JSON.stringify(o))
      .join("\n") + "\n",
  );
  const env = hostEnv({
    PATH: process.env.PATH,
    HOME: home,
    TMPDIR: dir,
    LOOM_BASE_URL: "http://127.0.0.1:9",
    LOOM_API_KEY: "x.y.z",
    LOOM_ORG: "acme",
    LOOM_NAMESPACE: "agent-x",
    LOOM_CONV_SETTLE_MS: "0",
  });
  const run = () =>
    spawnSync(process.execPath, [hook, "Stop"], {
      input: JSON.stringify({ session_id: "s-recording", transcript_path: transcript, cwd: dir }),
      env,
      timeout: 10_000,
    });
  writeRecording("http://127.0.0.1:9", "acme", "agent-x", false, { tmp: dir });
  run();
  const names = fs.readdirSync(dir);
  assert.deepEqual(
    names.filter((n) => n.startsWith("loom-flush-")),
    [],
    "no flush job while off",
  );
  // The cursor (a byte offset) moves past the whole transcript.
  assert.equal(
    fs.readFileSync(path.join(dir, "loom-conv-s-recording.offset"), "utf8"),
    String(fs.statSync(transcript).size),
  );
  // on (or unknown) → the turns are handed to the flusher as before
  fs.unlinkSync(path.join(dir, "loom-conv-s-recording.offset"));
  writeRecording("http://127.0.0.1:9", "acme", "agent-x", true, { tmp: dir });
  run();
  const jobs = fs.readdirSync(dir).filter((n) => n.startsWith("loom-flush-"));
  const cursor = path.join(dir, "loom-conv-s-recording.offset");
  assert.ok(jobs.length === 1 || fs.existsSync(cursor) === false, "the flusher got the job");
});
