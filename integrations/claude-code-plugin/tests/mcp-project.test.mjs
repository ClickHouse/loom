import "./temp-home.mjs";
import { test } from "node:test";
import { execFileSync } from "node:child_process";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { processIdentity, publishProject, sessionProject } from "../lib/mcp-project.mjs";

test("the native platform supplies this live process's creation identity", () => {
  const failures = [];
  const run = (...args) => {
    const started = Date.now();
    try {
      return execFileSync(...args);
    } catch (error) {
      failures.push({ code: error.code, signal: error.signal, status: error.status, elapsedMs: Date.now() - started });
      throw error;
    }
  };
  const identity = processIdentity(process.pid, { run });
  assert.equal(identity?.pid, process.pid, JSON.stringify(failures));
  assert.equal(identity?.parent, process.ppid);
  assert.ok(identity?.born);
  assert.ok(identity?.name);
  assert.deepEqual(processIdentity(process.pid), identity);
});

test("macOS and Windows process queries parse creation identities without shell interpolation", () => {
  for (const [platform, command, output, expected] of [
    [
      "darwin",
      "ps",
      "  20 Mon Nov 16 19:01:02 2026 claude\n",
      { pid: 30, parent: 20, born: "Mon Nov 16 19:01:02 2026", name: "claude" },
    ],
    [
      "win32",
      "C:\\Windows\\System32\\cscript.exe",
      "claude.exe\t20\t134129772620000000\r\n",
      { pid: 30, parent: 20, born: "134129772620000000", name: "claude.exe" },
    ],
  ]) {
    const run = (bin, args, options) => {
      assert.equal(bin, command);
      assert.equal(options.timeout, 1000);
      assert.equal(options.windowsHide, true);
      assert.equal(options.maxBuffer, 16384);
      if (platform === "darwin") assert.deepEqual(args, ["-p", "30", "-o", "ppid=", "-o", "lstart=", "-o", "ucomm="]);
      else {
        assert.deepEqual(args, [
          "//nologo",
          "//E:JScript",
          "//B",
          fileURLToPath(new URL("../assets/mcp-project-win32.js", import.meta.url)),
          "30",
        ]);
      }
      return output;
    };
    assert.deepEqual(processIdentity(30, { platform, run, env: { SystemRoot: "C:\\Windows" } }), expected);
    assert.equal(processIdentity(30, { platform, run: () => "invalid" }), null);
    assert.equal(
      processIdentity(30, {
        platform,
        run: () => {
          throw new Error("process exited");
        },
      }),
      null,
    );
    assert.equal(processIdentity("30; echo unsafe", { platform, run }), null);
  }
});

test("the Windows helper projects identity metadata and preserves its UTC creation string", () => {
  const source = fs.readFileSync(new URL("../assets/mcp-project-win32.js", import.meta.url), "utf8");
  const output = [];
  const date = "20261117190000.123456+000";
  const processRow = { Name: "客户端.exe", ParentProcessId: 20, CreationDate: date };
  let candidates = [processRow];
  const clock = {
    GetFileTime(local) {
      assert.equal(local, false);
      assert.equal(this.Value, date);
      return "134129772621234560";
    },
  };
  const context = {
    WScript: {
      Arguments: { length: 1, Item: () => "30" },
      StdOut: { WriteLine: (v) => output.push(v) },
      Quit: () => {
        throw new Error("identity unavailable");
      },
    },
    GetObject(uri) {
      assert.equal(uri, "winmgmts:\\\\.\\root\\cimv2");
      return {
        ExecQuery(query, language, flags) {
          assert.equal(query, "SELECT Name,ParentProcessId,CreationDate FROM Win32_Process WHERE ProcessId=30");
          assert.equal(language, "WQL");
          assert.equal(flags, 48);
          return candidates;
        },
      };
    },
    Enumerator: function (rows) {
      let i = 0;
      this.atEnd = () => i >= rows.length;
      this.item = () => rows[i];
      this.moveNext = () => i++;
    },
    ActiveXObject: function (name) {
      assert.equal(name, "WbemScripting.SWbemDateTime");
      return clock;
    },
  };
  vm.runInNewContext(source, context);
  assert.deepEqual(output, ["%E5%AE%A2%E6%88%B7%E7%AB%AF.exe\t20\t134129772621234560"]);
  assert.deepEqual(
    processIdentity(30, { platform: "win32", env: { SystemRoot: "C:\\Windows" }, run: () => output[0] + "\r\n" }),
    {
      pid: 30,
      parent: 20,
      born: "134129772621234560",
      name: "客户端.exe",
    },
  );
  for (const rows of [[], [processRow, processRow]]) {
    candidates = rows;
    output.length = 0;
    assert.throws(() => vm.runInNewContext(source, context), /identity unavailable/);
    assert.equal(output.length, 0);
  }
  context.WScript.Arguments.Item = () => "30; unsafe";
  assert.throws(() => vm.runInNewContext(source, context), /identity unavailable/);
  assert.equal(output.length, 0);
});

test("Windows identity rejects malformed or multiple records and out-of-range PIDs", () => {
  for (const output of [
    "",
    "node.exe\t20\t123\n\n",
    "node.exe\t20\t123\nnode.exe\t20\t123",
    "node.exe\t4294967296\t123",
    "node.exe\t-1\t123",
    "node.exe\t20\t0",
    "%FF.exe\t20\t123",
    "%ZZ.exe\t20\t123",
    "node.exe\t20\t123\textra",
  ])
    assert.equal(
      processIdentity(30, { platform: "win32", env: { SystemRoot: "C:\\Windows" }, run: () => output }),
      null,
    );
  let ran = false;
  assert.equal(
    processIdentity(4294967296, {
      platform: "win32",
      run: () => {
        ran = true;
      },
    }),
    null,
  );
  assert.equal(ran, false);
});

test("Windows identity runs only the absolute system host from the parent OS environment", () => {
  for (const SystemRoot of [undefined, "", "Windows", "C:Windows", "\\\\server\\share\\Windows", "/Windows"]) {
    let ran = false;
    assert.equal(
      processIdentity(30, {
        platform: "win32",
        env: { SystemRoot },
        run: () => {
          ran = true;
          return "node.exe\t20\t123";
        },
      }),
      null,
    );
    assert.equal(ran, false);
  }
  for (const SystemRoot of ["C:\\Windows", "D:\\OS Location"]) {
    const identity = processIdentity(30, {
      platform: "win32",
      env: { SystemRoot },
      run: (host) => {
        assert.equal(host, path.win32.join(SystemRoot, "System32", "cscript.exe"));
        return "node.exe\t20\t123";
      },
    });
    assert.equal(identity?.pid, 30);
  }
});

test("concurrent clients retain exact quote/dollar paths and separate sessions", async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "loom-project-test-"));
  try {
    const identities = new Map([
      [10, { pid: 10, parent: 1, born: "first" }],
      [11, { pid: 11, parent: 10, born: "shell1", name: "sh" }],
      [20, { pid: 20, parent: 1, born: "second" }],
      [21, { pid: 21, parent: 20, born: "shell2", name: "sh" }],
    ]);
    const identity = (pid) => identities.get(pid) || null;
    const first = path.join(tmp, 'one "quoted" $literal');
    const second = path.join(tmp, "two 'quoted' $literal");
    assert.ok(publishProject({ cwd: first, session_id: "one" }, { pid: 10, identity, tmp }));
    assert.ok(publishProject({ cwd: second, session_id: "two" }, { pid: 20, identity, tmp }));
    assert.deepEqual(
      await Promise.all([
        sessionProject({ pid: 11, identity, tmp, waitMs: 0 }),
        sessionProject({ pid: 21, identity, tmp, waitMs: 0 }),
      ]),
      [first, second],
    );
    if (process.platform !== "win32")
      for (const file of fs.readdirSync(tmp)) assert.equal(fs.statSync(path.join(tmp, file)).mode & 0o777, 0o600);
    identities.set(30, { pid: 30, parent: 10, born: "nested-client", name: "claude" });
    identities.set(31, { pid: 31, parent: 30, born: "nested-shell", name: "sh" });
    assert.equal(
      await sessionProject({ pid: 31, identity, tmp, waitMs: 0 }),
      "",
      "a nested client never inherits an outer client's context",
    );
    identities.set(10, { pid: 10, parent: 1, born: "reused" });
    assert.equal(await sessionProject({ pid: 11, identity, tmp, waitMs: 0 }), "");
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test("missing client ancestors return without scheduling a context poll", async (t) => {
  t.mock.method(globalThis, "setTimeout", () => {
    assert.fail("a context write cannot resolve a missing client identity");
  });
  for (const identity of [
    () => null,
    (pid) => ({ pid, parent: 1, born: "shell", name: "sh" }),
    (pid) => ({ pid, parent: pid - 1, born: String(pid), name: "sh" }),
  ]) {
    assert.equal(await sessionProject({ pid: 30, identity, waitMs: 1500 }), "");
  }
});

test("a later context write can complete while the helper waits", async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "loom-project-test-"));
  const identity = (pid) => ({ pid, parent: 1, born: "live" });
  try {
    const pending = sessionProject({ pid: 10, identity, tmp, waitMs: 250 });
    publishProject({ cwd: tmp, session_id: "later" }, { pid: 10, identity, tmp });
    assert.equal(await pending, tmp);
    assert.equal(await sessionProject({ pid: 99, identity, tmp, waitMs: 0 }), "");
    assert.equal(publishProject({ cwd: "relative", session_id: "bad" }, { pid: 10, identity, tmp }), false);
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});
