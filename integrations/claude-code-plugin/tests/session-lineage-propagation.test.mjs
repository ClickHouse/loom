import "./temp-home.mjs";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, test } from "node:test";

import { dispatchFlush, runFlushJob } from "../lib/flush-job.mjs";
import { requestJson } from "../lib/loom-client.mjs";
import {
  bindProtectionAlias,
  createProtectionRoot,
  protectSession,
  sessionIsProtected,
} from "../lib/session-protection.mjs";
import {
  observeSessionResponse,
  withAutomaticSessionProtection,
  withSessionProtection,
} from "../lib/session-response.mjs";

const owned = new Set();
function fixture() {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "loom-lineage-test-"));
  owned.add(parent);
  const directory = process.platform === "win32" ? path.join(parent, "private") : parent;
  return { directory, a: createProtectionRoot({ base: directory }), b: createProtectionRoot({ base: directory }) };
}
afterEach(() => {
  for (const directory of owned) fs.rmSync(directory, { recursive: true, force: true });
  owned.clear();
});
const response = (body) => async () => ({ status: 200, json: async () => body });

function queued(directory, a, b) {
  let job;
  const result = withAutomaticSessionProtection(a, () =>
    withAutomaticSessionProtection(b, () =>
      dispatchFlush(
        {
          posts: [{ op: "memory.set_from_messages", body: { messages: ["private"] } }],
          cursorPath: "/cursor",
          advanceTo: 99,
        },
        {
          tmp: directory,
          spawnImpl(_command, args) {
            job = JSON.parse(fs.readFileSync(args[1], "utf8"));
            const child = new EventEmitter();
            child.unref = () => {};
            return child;
          },
        },
      ),
    ),
  );
  assert.equal(result, true);
  return job;
}

test("automatic contexts refuse missing protection roots", () => {
  let ran = false;
  withAutomaticSessionProtection({ base: path.resolve("missing"), id: "00000000-0000-0000-0000-000000000000" }, () => {
    ran = true;
  });
  assert.equal(ran, false);
});
{
  test("nested automatic callbacks retain a protected producer across await", async () => {
    const { a, b } = fixture();
    let sends = 0;
    const result = await withAutomaticSessionProtection(a, () =>
      withAutomaticSessionProtection(b, async () => {
        await Promise.resolve();
        protectSession(a);
        return requestJson("http://inert.invalid/v1/conversation.append", {
          fetchImpl: async () => {
            sends++;
            return { status: 200, json: async () => ({ stored: true }) };
          },
        });
      }),
    );
    assert.deepEqual(result, { status: 0, body: null, suppressed: true });
    assert.equal(sends, 0);
    assert.equal(sessionIsProtected(b), false);
    assert.equal(
      (
        await withAutomaticSessionProtection(b, () =>
          requestJson("http://inert.invalid/v1/code.repos", { fetchImpl: response({ repos: [] }) }),
        )
      ).status,
      200,
    );
  });

  test("a protected parent prevents a nested automatic callback from beginning", () => {
    const { a, b } = fixture();
    let ran = false;
    withAutomaticSessionProtection(a, () => {
      protectSession(a);
      withAutomaticSessionProtection(b, () => {
        ran = true;
      });
    });
    assert.equal(ran, false);
  });

  test("a marked nested reply protects every known family before return", async () => {
    const { a, b } = fixture();
    const result = await withAutomaticSessionProtection(a, () =>
      withAutomaticSessionProtection(b, () =>
        requestJson("http://inert.invalid/v1/knowledge.get", {
          fetchImpl: response({ mirrored: true, content: "private" }),
        }),
      ),
    );
    assert.equal(result.status, 0);
    assert.equal(result.body, null);
    assert.equal(result.error, "Session protection state could not be confirmed");
    assert.equal(sessionIsProtected(a), true);
    assert.equal(sessionIsProtected(b), true);
  });

  test("a malformed bound JSON reply protects before withholding while unbound behavior stays ordinary", async () => {
    const { a } = fixture();
    const fetchImpl = async () => ({
      status: 200,
      json: async () => {
        throw new SyntaxError("truncated");
      },
    });
    const unbound = await requestJson("http://inert.invalid/v1/knowledge.get", { fetchImpl });
    assert.deepEqual(unbound, { status: 200, body: null });
    const bound = await withSessionProtection(a, () =>
      requestJson("http://inert.invalid/v1/knowledge.get", { fetchImpl }),
    );
    assert.equal(bound.status, 0);
    assert.equal(bound.body, null);
    assert.equal(bound.error, "Session protection state could not be confirmed");
    assert.equal(sessionIsProtected(a), true);
  });

  test("an ambiguous canonical flag durably protects its observer", () => {
    const { a } = fixture();
    assert.throws(() => observeSessionResponse(a, { mirrored: "true" }), {
      message: "Session protection state could not be confirmed",
    });
    assert.equal(sessionIsProtected(a), true);
  });

  test("a failed earlier observer cannot leave another known family clean", async () => {
    const { directory, a, b } = fixture();
    const result = await withSessionProtection(a, () =>
      withSessionProtection(b, async () => {
        fs.unlinkSync(path.join(directory, a.id, "state"));
        return requestJson("http://inert.invalid/v1/knowledge.get", {
          fetchImpl: response({ mirrored: true, content: "private" }),
        });
      }),
    );
    assert.equal(result.status, 0);
    assert.equal(result.body, null);
    assert.equal(sessionIsProtected(b), true);
  });

  test("detached job control records all producer families", () => {
    const { directory, a, b } = fixture();
    const job = queued(directory, a, b);
    assert.deepEqual(job.protectionRoots, [a, b]);
    assert.equal(Object.hasOwn(job, "protectionRoot"), false);
  });

  test("later protection of any queued producer prevents delivery under a clean caller", async () => {
    const { directory, a, b } = fixture();
    const job = queued(directory, a, b);
    const unrelated = createProtectionRoot({ base: directory });
    protectSession(a);
    let sends = 0;
    const cursor = {
      value: "10",
      read() {
        return this.value;
      },
      write(_path, value) {
        this.value = String(value);
      },
    };
    const result = await withSessionProtection(unrelated, () =>
      runFlushJob(job, {
        cursor,
        post: async () => {
          sends++;
          return { stored: true };
        },
      }),
    );
    assert.deepEqual(result, { postsOk: false, appendsOk: false });
    assert.equal(sends, 0);
    assert.equal(cursor.value, "10");
    assert.equal(sessionIsProtected(b), false);
  });

  test("an unrelated protected flusher cannot suppress a clean captured producer", async () => {
    const { directory, a, b } = fixture();
    const job = queued(directory, a, b);
    const unrelated = createProtectionRoot({ base: directory });
    protectSession(unrelated);
    let sends = 0;
    const cursor = {
      value: "10",
      read() {
        return this.value;
      },
      write(_path, value) {
        this.value = String(value);
      },
    };
    const result = await withSessionProtection(unrelated, () =>
      runFlushJob(job, {
        cursor,
        post: async () => {
          sends++;
          return (
            await requestJson("http://inert.invalid/v1/memory.set_from_messages", {
              fetchImpl: response({ stored: true }),
            })
          ).body;
        },
      }),
    );
    assert.deepEqual(result, { postsOk: true, appendsOk: false });
    assert.equal(sends, 1);
    assert.equal(sessionIsProtected(a), false);
    assert.equal(sessionIsProtected(b), false);
  });

  test("queued root arrays cannot switch across an awaited post", async () => {
    const { directory, a, b } = fixture();
    const job = queued(directory, a, b);
    job.appends = [{}];
    const replacement = createProtectionRoot({ base: directory });
    let sends = 0;
    const result = await runFlushJob(job, {
      cursor: {
        read: () => "10",
        write() {
          assert.fail("tainted cursor advanced");
        },
      },
      post: async () => {
        sends++;
        protectSession(a);
        job.protectionRoots.splice(0, job.protectionRoots.length, replacement);
        await Promise.resolve();
        return { stored: true };
      },
    });
    assert.deepEqual(result, { postsOk: false, appendsOk: false });
    assert.equal(sends, 1);
    assert.equal(sessionIsProtected(replacement), false);
  });

  test("an explicit nested reply still observes its automatic parent", async () => {
    const { a, b } = fixture();
    const result = await withAutomaticSessionProtection(a, () =>
      withSessionProtection(b, () =>
        requestJson("http://inert.invalid/v1/knowledge.get", {
          fetchImpl: response({ mirrored: true, content: "private" }),
        }),
      ),
    );
    assert.equal(result.body.content, "private");
    assert.equal(sessionIsProtected(a), true);
    assert.equal(sessionIsProtected(b), true);
  });

  for (const body of [null, [], "not an envelope"]) {
    test(`bound JSON ${JSON.stringify(body)} refuses without assuming an absent mark`, async () => {
      const { a } = fixture();
      const result = await withSessionProtection(a, () =>
        requestJson("http://inert.invalid/v1/knowledge.get", { fetchImpl: response(body) }),
      );
      assert.equal(result.status, 0);
      assert.equal(result.body, null);
      assert.equal(result.error, "Session protection state could not be confirmed");
      assert.equal(sessionIsProtected(a), true);
    });
  }

  test("empty HTTP statuses and absent hook output do not invent mirrored activity", async () => {
    const { a } = fixture();
    for (const status of [204, 304]) {
      const result = await withSessionProtection(a, () =>
        requestJson("http://inert.invalid/v1/health", {
          fetchImpl: async () => ({ status, json: async () => assert.fail("a bodyless status was decoded") }),
        }),
      );
      assert.deepEqual(result, { status, body: null });
    }
    assert.equal(observeSessionResponse(a, undefined), undefined);
    assert.equal(sessionIsProtected(a), false);
  });

  for (const [name, readable, marked] of [
    ["an unreadable event-fed transcript", false, true],
    ["a transcript without appendable turns", true, true],
    ["an ordinary transcript without appendable turns", true, false],
  ]) {
    test(`conversation settle rechecks protection for ${name}`, () => {
      const { directory } = fixture();
      const home = path.join(directory, "home");
      const tmp = path.join(directory, "tmp");
      fs.mkdirSync(home, { mode: 0o700 });
      fs.mkdirSync(tmp, { mode: 0o700 });
      const root = createProtectionRoot({ base: path.join(home, ".loom", "session-protection", "v1") });
      const transcript = path.join(directory, "transcript.jsonl");
      if (readable) fs.writeFileSync(transcript, JSON.stringify({ type: "progress" }) + "\n");
      const sessionId = "settle-control";
      bindProtectionAlias(root, "claude-code", sessionId);
      const stateModule = new URL("../lib/session-protection.mjs", import.meta.url).href;
      const hookModule = new URL("../hooks/loom-conversation.mjs", import.meta.url).href;
      const source = `
      import fs from "node:fs";
      import { protectSession } from ${JSON.stringify(stateModule)};
      const realTimeout = globalThis.setTimeout;
      globalThis.setTimeout = (callback, delay, ...args) => realTimeout(() => {
        if (delay === 37) {
          fs.writeFileSync(${JSON.stringify(path.join(directory, "settled"))}, "entered");
          if (${marked}) protectSession(${JSON.stringify(root)});
        }
        callback(...args);
      }, delay);
      await import(${JSON.stringify(hookModule)});
    `;
      const result = spawnSync(
        process.execPath,
        ["--input-type=module", "-e", source, "hook-entry.mjs", "Stop", "claude-code"],
        {
          env: {
            PATH: process.env.PATH,
            SystemRoot: process.env.SystemRoot,
            HOME: home,
            USERPROFILE: home,
            TMPDIR: tmp,
            TMP: tmp,
            TEMP: tmp,
            LOOM_CONV_SETTLE_MS: "37",
          },
          input: JSON.stringify({
            session_id: sessionId,
            hook_event_name: "Stop",
            transcript_path: transcript,
            last_assistant_message: readable ? "" : "private reply",
          }),
          encoding: "utf8",
          timeout: 5000,
        },
      );
      assert.equal(result.error, undefined);
      assert.equal(result.status, 0, result.stderr);
      assert.equal(result.stderr, "");
      assert.equal(fs.readFileSync(path.join(directory, "settled"), "utf8"), "entered");
      assert.equal(sessionIsProtected(root), marked);
      assert.equal(fs.existsSync(path.join(tmp, `loom-conv-${sessionId}.offset`)), !marked);
      assert.equal(
        fs.readdirSync(tmp).some((name) => name.includes("turns")),
        false,
      );
    });
  }
}
