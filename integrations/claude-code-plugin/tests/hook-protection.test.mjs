import "./temp-home.mjs";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { afterEach, test } from "node:test";
import { hostEnv } from "./host-env.mjs";

import { bindHookProtection } from "../lib/hook-protection.mjs";
import {
  bindProtectionAlias,
  createProtectionRoot,
  findProtectionAlias,
  protectSession,
  sessionIsProtected,
  SessionProtectionUnavailable,
} from "../lib/session-protection.mjs";

const owned = new Set();
function fixture() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "loom-hook-lineage-"));
  owned.add(directory);
  const base = path.join(directory, ".loom", "session-protection", "v1");
  return { directory, base, root: createProtectionRoot({ base }) };
}
afterEach(() => {
  for (const directory of owned) fs.rmSync(directory, { recursive: true, force: true });
  owned.clear();
});
const payload = (source = "startup") => ({ session_id: "known", hook_event_name: "SessionStart", source });

test("an unbound resumed hook identity never acquires a new family", () => {
  assert.throws(
    () => bindHookProtection(payload("resume"), { host: "codex", event: "SessionStart" }),
    SessionProtectionUnavailable,
  );
});
{
  test("canonical existing alias reopens the same family without creating state", () => {
    const { base, root } = fixture();
    bindProtectionAlias(root, "codex", "known");
    const before = fs.readdirSync(base);
    for (const source of ["startup", "resume", "fork", "clear", "compact"]) {
      assert.deepEqual(bindHookProtection(payload(source), { host: "codex", event: "SessionStart", base }), root);
    }
    assert.deepEqual(fs.readdirSync(base), before);
    protectSession(root);
    assert.deepEqual(bindHookProtection(payload("resume"), { host: "codex", event: "SessionStart", base }), root);
    assert.equal(sessionIsProtected(findProtectionAlias("codex", "known", { base })), true);
  });

  for (const source of ["resume", "fork", "clear", "compact"]) {
    test(`unbound ${source} refuses without a root factory`, () => {
      const { base, root } = fixture();
      const before = fs.readdirSync(base);
      assert.throws(
        () => bindHookProtection(payload(source), { host: "codex", event: "SessionStart", base }),
        SessionProtectionUnavailable,
      );
      assert.deepEqual(fs.readdirSync(base), before);
      assert.equal(sessionIsProtected(root), false);
    });
  }

  test("normalization and ambient parent names never choose an alias", () => {
    const { base, root } = fixture();
    bindProtectionAlias(root, "codex", "42");
    for (const input of [{ session_id: 42 }, { sessionId: "42" }, { session_id: "missing", parent_id: "42" }]) {
      assert.throws(
        () => bindHookProtection(input, { host: "codex", event: "SessionStart", base }),
        SessionProtectionUnavailable,
      );
    }
    assert.equal(sessionIsProtected(root), false);
    assert.equal(findProtectionAlias("codex", "missing", { base }), undefined);
  });

  test("all conflicting known roots are marked without selecting a clean replacement", () => {
    const { base, root } = fixture();
    const second = createProtectionRoot({ base });
    const third = createProtectionRoot({ base });
    bindProtectionAlias(root, "codex", "known");
    assert.throws(
      () => bindHookProtection(payload(), { host: "codex", event: "SessionStart", base, knownRoots: [second, third] }),
      SessionProtectionUnavailable,
    );
    for (const anchor of [root, second, third]) assert.equal(sessionIsProtected(anchor), true);
    assert.deepEqual(findProtectionAlias("codex", "known", { base }), root);
  });

  test("invalid event metadata still protects every independently known anchor", () => {
    const { base, root } = fixture();
    const other = createProtectionRoot({ base });
    bindProtectionAlias(root, "codex", "known");
    assert.throws(
      () =>
        bindHookProtection(
          { ...payload(), hook_event_name: "PostToolUse" },
          {
            host: "codex",
            event: "SessionStart",
            base,
            knownRoots: [other],
          },
        ),
      SessionProtectionUnavailable,
    );
    assert.equal(sessionIsProtected(root), true);
    assert.equal(sessionIsProtected(other), true);
  });

  test("an unreadable first anchor does not skip the remaining durable marks", () => {
    const { base, root } = fixture();
    const second = createProtectionRoot({ base });
    const third = createProtectionRoot({ base });
    bindProtectionAlias(root, "codex", "known");
    fs.unlinkSync(path.join(base, root.id, "state"));
    assert.throws(
      () =>
        bindHookProtection(payload(), {
          host: "codex",
          event: "SessionStart",
          base,
          knownRoots: [root, second, third],
        }),
      SessionProtectionUnavailable,
    );
    assert.equal(sessionIsProtected(second), true);
    assert.equal(sessionIsProtected(third), true);
  });

  test("corrupt aliases and missing manifests refuse instead of becoming clean history", () => {
    const { base, root } = fixture();
    bindProtectionAlias(root, "codex", "known");
    fs.unlinkSync(path.join(base, root.id, "root.json"));
    const before = fs.readdirSync(base);
    assert.throws(
      () => bindHookProtection(payload("resume"), { host: "codex", event: "SessionStart", base }),
      SessionProtectionUnavailable,
    );
    assert.deepEqual(fs.readdirSync(base), before);
  });

  for (const [state, response, shouldRun] of [
    ["clean", { answer: "ordinary" }, true],
    ["clean", { mirrored: true, content: "private" }, false],
    ["protected", { answer: "ordinary" }, false],
    ["unknown", { answer: "ordinary" }, false],
  ]) {
    test(`hook entry ${state}/${Boolean(response.mirrored)} guards before request or journal`, () => {
      const { directory, root } = fixture();
      if (state !== "unknown") bindProtectionAlias(root, "claude-code", "known");
      if (state === "protected") protectSession(root);
      const journal = path.join(directory, "journal");
      const sends = path.join(directory, "sends");
      const clientModule = new URL("../lib/loom-client.mjs", import.meta.url).href;
      const code = `
        import fs from "node:fs";
        import { postBounded, runHookMain } from ${JSON.stringify(clientModule)};
        globalThis.fetch = async () => {
          fs.appendFileSync(${JSON.stringify(sends)}, "sent\\n");
          return { status: 200, json: async () => ({ ok: true }) };
        };
        await runHookMain(async () => {
          fs.writeFileSync(${JSON.stringify(journal)}, "entered");
          await postBounded("observe.record", {}, { token: "fixture", serverUrl: "http://inert.invalid" });
        });
      `;
      const result = spawnSync(
        process.execPath,
        ["--input-type=module", "-e", code, "hook-entry.mjs", "PostToolUse", "claude-code"],
        {
          env: hostEnv({ PATH: process.env.PATH, HOME: directory }),
          input: JSON.stringify({ session_id: "known", hook_event_name: "PostToolUse", tool_response: response }),
          encoding: "utf8",
          timeout: 5000,
        },
      );
      assert.equal(result.error, undefined);
      assert.equal(result.status, 0, result.stderr);
      assert.equal(result.stderr, "");
      assert.equal(fs.existsSync(journal), shouldRun);
      assert.equal(fs.existsSync(sends), shouldRun);
      assert.equal(sessionIsProtected(root), state === "protected" || Boolean(response.mirrored));
    });
  }
}
