import "./temp-home.mjs";
import assert from "node:assert/strict";
import fs from "node:fs";
import crypto from "node:crypto";
import os from "node:os";
import path from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { afterEach, test } from "node:test";
import {
  bindProtectionAlias,
  createProtectionRoot,
  establishProtectionAlias,
  findProtectionAlias,
  protectionForAlias,
  protectSession,
  sessionIsProtected,
  SessionProtectionUnavailable,
} from "../lib/session-protection.mjs";

const owned = new Set();
afterEach(() => {
  for (const directory of owned) fs.rmSync(directory, { recursive: true, force: true });
  owned.clear();
});
function fresh() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "loom-windows-authority-"));
  owned.add(directory);
  const root = createProtectionRoot({ base: path.join(directory, "private") });
  return { root, directory: path.join(root.base, root.id) };
}

function witnessEnv() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "loom-windows-lifetime-"));
  owned.add(directory);
  return {
    ...process.env,
    LOOM_PRIVATE_COMPILER_BASE: path.join(directory, "compiler"),
    LOOM_PRIVATE_COMPILATION_ID: crypto.randomBytes(16).toString("hex"),
    LOOM_PRIVATE_DEADLINE_UTC: String(Date.now() + 1500),
  };
}

function helperRequest(input) {
  const environment = witnessEnv();
  const system32 = path.join(process.env.SystemRoot, "System32");
  const powershellHome = path.join(system32, "WindowsPowerShell", "v1.0");
  const result = spawnSync(
    path.join(powershellHome, "powershell.exe"),
    [
      "-NoLogo",
      "-NoProfile",
      "-NonInteractive",
      "-File",
      fileURLToPath(new URL("../lib/windows-session-protection.ps1", import.meta.url)),
    ],
    {
      input,
      encoding: "utf8",
      windowsHide: true,
      cwd: system32,
      timeout: 1500,
      maxBuffer: 1024,
      env: {
        SystemRoot: process.env.SystemRoot,
        WINDIR: process.env.SystemRoot,
        PATH: system32,
        PSModulePath: path.join(powershellHome, "Modules"),
        LOOM_PRIVATE_COMPILER_BASE: environment.LOOM_PRIVATE_COMPILER_BASE,
        LOOM_PRIVATE_COMPILATION_ID: environment.LOOM_PRIVATE_COMPILATION_ID,
        LOOM_PRIVATE_DEADLINE_UTC: environment.LOOM_PRIVATE_DEADLINE_UTC,
      },
    },
  );
  assert.equal(result.error, undefined);
  assert.equal(result.signal, null);
  return result;
}

// Retain only protocol phases and observer timing when a real witness exits early.
function nativePhaseTrail(child) {
  const started = performance.now();
  const phases = new Set([
    "entry",
    "encoding-new",
    "encoding-created",
    "encoding-input",
    "encoding-output",
    "startup",
    "bootstrap",
    "worker-bootstrap",
    "guardian-deadline",
    "native-job",
    "native-timer",
    "worker-start",
    "worker-assign",
    "compiler-private",
    "compiler-identity",
    "compiler-path",
    "compiler-security",
    "compiler-parents",
    "compiler-directory",
    "compiler-receipt",
    "compiler-environment",
    "compiler-launch",
    "compiler-stopped",
    "compiler-closed",
    "compiler-scanned",
    "compiler-bytes",
    "compiler-format",
    "compiler-stop",
    "compiler-query",
    "compiler-close",
    "compile",
    "request",
    "request-parse",
    "request-keys",
    "native-op",
    "record-construct",
    "record-dispatch",
    "root-entry",
    "root-flushed",
    "output-encode",
    "cleanup",
    "worker-exit",
    "helper-exit",
  ]);
  const frames = [];
  let pending = "";
  let rejected = 0;
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => {
    pending += chunk;
    if (pending.length > 1024) {
      pending = "";
      rejected = Math.min(1024, rejected + 1);
      return;
    }
    let end;
    while ((end = pending.indexOf("\n")) !== -1) {
      const line = pending.slice(0, end).replace(/\r$/, "");
      pending = pending.slice(end + 1);
      const match = /^LSP1:([a-z-]+):(-?[0-9]{1,10})$/.exec(line);
      const code = match ? Number(match[2]) : NaN;
      if (
        match &&
        phases.has(match[1]) &&
        Number.isSafeInteger(code) &&
        code >= -2147483648 &&
        code <= 2147483647 &&
        (match[1] !== "compiler-bytes" || (code >= 0 && code <= 8192)) &&
        (match[1] !== "compiler-format" || (code >= 0 && code <= 6)) &&
        (!["root-entry", "root-flushed"].includes(match[1]) || code === 0)
      ) {
        if (frames.length === 24) frames.shift();
        frames.push({ phase: match[1], code, observerMs: Math.min(60000, Math.round(performance.now() - started)) });
      } else rejected = Math.min(1024, rejected + 1);
    }
  });
  return () => JSON.stringify({ frames, rejected });
}

function exactProcesses(identities, kill = false) {
  const source = String.raw`
    $ErrorActionPreference = 'Stop'
    $r = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $live = @()
    foreach ($identity in $r.identities) {
      try {
        $p = [Diagnostics.Process]::GetProcessById([int]$identity.pid)
        if (-not $p.HasExited -and $p.StartTime.ToUniversalTime().Ticks.ToString() -eq $identity.started) {
          $live += [int]$identity.pid
          if ($r.kill) { $p.Kill(); [void]$p.WaitForExit(500) }
        }
      } catch [ArgumentException] {} catch [InvalidOperationException] {}
    }
    ConvertTo-Json -Compress -InputObject @($live)
  `;
  const result = spawnSync(
    path.join(process.env.SystemRoot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
    ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", source],
    {
      input: JSON.stringify({ identities, kill }),
      encoding: "utf8",
      timeout: 2000,
      maxBuffer: 1024,
    },
  );
  assert.equal(result.error, undefined);
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
}

// An independent observer uses real Windows access-control objects. This
// changes actual test-owned DACLs; it does not replace provider observations.
function acl(operation, file) {
  const source = String.raw`
    $ErrorActionPreference = 'Stop'
    $r = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $item = Get-Item -LiteralPath $r.file -Force
    $acl = $item.GetAccessControl()
    if ($r.operation -eq 'broaden') {
      $world = New-Object Security.Principal.SecurityIdentifier('S-1-1-0')
      $rule = New-Object Security.AccessControl.FileSystemAccessRule($world, [Security.AccessControl.FileSystemRights]::Read, [Security.AccessControl.AccessControlType]::Allow)
      $acl.AddAccessRule($rule)
      $item.SetAccessControl($acl)
    } elseif ($r.operation -eq 'unprotect') {
      $acl.SetAccessRuleProtection($false, $true)
      $item.SetAccessControl($acl)
    } elseif ($r.operation -ne 'observe') { throw 'Unexpected operation' }
    $acl = $item.GetAccessControl()
    $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    @{
      owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
      current = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
      protected = $acl.AreAccessRulesProtected
      rules = @($rules | ForEach-Object { @{ sid = $_.IdentityReference.Value; rights = [int]$_.FileSystemRights; inherited = $_.IsInherited; kind = $_.AccessControlType.ToString() } })
    } | ConvertTo-Json -Compress -Depth 4
  `;
  const child = spawnSync(
    path.join(process.env.SystemRoot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
    ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", source],
    {
      input: JSON.stringify({ operation, file }),
      encoding: "utf8",
      timeout: 5000,
      maxBuffer: 4096,
    },
  );
  assert.equal(child.error, undefined);
  assert.equal(child.status, 0, child.stderr);
  return JSON.parse(child.stdout);
}

if (process.platform === "win32") {
  test("forced helper termination also terminates a real child in its compiler job", { timeout: 5000 }, async () => {
    const powershell = path.join(process.env.SystemRoot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
    const witness = spawn(
      powershell,
      [
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        fileURLToPath(new URL("./windows-job-witness.ps1", import.meta.url)),
      ],
      {
        stdio: ["pipe", "pipe", "pipe"],
        windowsHide: true,
        env: witnessEnv(),
      },
    );
    const phases = nativePhaseTrail(witness);
    witness.stdin.end();
    const exited = new Promise((resolve) => witness.once("exit", resolve));
    let child;
    let startupTimer;
    try {
      child = await new Promise((resolve, reject) => {
        let output = "";
        startupTimer = setTimeout(() => reject(new Error(`job witness did not start: ${phases()}`)), 2000);
        witness.once("error", reject);
        witness.once("exit", () => reject(new Error(`job witness exited before announcing its child: ${phases()}`)));
        witness.stdout.on("data", (chunk) => {
          output += chunk.toString("utf8");
          if (output.includes("\n")) {
            try {
              resolve(JSON.parse(output.trim()));
            } catch (error) {
              reject(error);
            }
          }
        });
      });
      clearTimeout(startupTimer);
      assert.ok(Number.isInteger(child.pid) && child.pid > 0);
      assert.equal(typeof child.started, "string");
      assert.equal(witness.kill("SIGKILL"), true);
      await exited;
      let alive = true;
      const until = performance.now() + 500;
      while (alive && performance.now() < until) {
        try {
          process.kill(child.pid, 0);
        } catch (error) {
          if (error.code === "ESRCH") alive = false;
          else throw error;
        }
        if (alive) await new Promise((resolve) => setTimeout(resolve, 20));
      }
      assert.equal(alive, false, "compiler-job child survived helper termination");
    } finally {
      clearTimeout(startupTimer);
      if (witness.exitCode === null && witness.signalCode === null) witness.kill("SIGKILL");
      // On a failing witness, clean only the exact announced PID+start time.
      // The assertion above remains a failure even if this cleanup succeeds.
      if (child) {
        const cleanupSource = String.raw`
          $ErrorActionPreference = 'Stop'
          $r = [Console]::In.ReadToEnd() | ConvertFrom-Json
          try { $p = [Diagnostics.Process]::GetProcessById([int]$r.pid) }
          catch [ArgumentException] { exit 0 }
          if ($p.StartTime.ToUniversalTime().Ticks.ToString() -eq $r.started) { $p.Kill(); [void]$p.WaitForExit(1000) }
        `;
        const cleanup = spawnSync(powershell, ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", cleanupSource], {
          input: JSON.stringify(child),
          encoding: "utf8",
          timeout: 2000,
          maxBuffer: 1024,
        });
        assert.equal(cleanup.error, undefined);
        assert.equal(cleanup.status, 0, cleanup.stderr);
      }
    }
  });

  test("native deadlines stop helper and real compiler after Node-parent death and clean their owned directory", {
    timeout: 5000,
  }, async () => {
    const environment = witnessEnv();
    const caller = spawn(process.execPath, [fileURLToPath(new URL("./windows-job-parent.mjs", import.meta.url))], {
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
      env: environment,
    });
    const phases = nativePhaseTrail(caller);
    const exited = new Promise((resolve) => caller.once("exit", resolve));
    let receipt;
    let timer;
    try {
      receipt = await new Promise((resolve, reject) => {
        let output = "";
        timer = setTimeout(() => reject(new Error(`real compiler witness did not start: ${phases()}`)), 2000);
        caller.once("error", reject);
        caller.once("exit", () => reject(new Error(`Node caller exited before compiler announcement: ${phases()}`)));
        caller.stdout.on("data", (chunk) => {
          output += chunk.toString("utf8");
          if (output.includes("\n")) {
            try {
              resolve(JSON.parse(output.trim()));
            } catch (error) {
              reject(error);
            }
          }
        });
      });
      clearTimeout(timer);
      for (const identity of [receipt, receipt.guardian]) {
        assert.ok(Number.isInteger(identity.pid) && identity.pid > 0);
        assert.equal(typeof identity.started, "string");
      }
      assert.notEqual(receipt.guardian.pid, receipt.pid, "guardian and real compiler must be distinct processes");
      assert.equal(
        receipt.directory,
        path.join(environment.LOOM_PRIVATE_COMPILER_BASE, environment.LOOM_PRIVATE_COMPILATION_ID),
      );
      assert.equal(fs.existsSync(receipt.directory), true);
      assert.deepEqual(
        exactProcesses([receipt.guardian, receipt]).sort((a, b) => a - b),
        [receipt.guardian.pid, receipt.pid].sort((a, b) => a - b),
      );
      assert.equal(caller.kill("SIGKILL"), true);
      await exited;
      // This wait observes the existing 1500ms primary deadline; it does not
      // provide a timer to the surviving guardian.
      await new Promise((resolve) => setTimeout(resolve, 1500));
      assert.deepEqual(exactProcesses([receipt.guardian, receipt]), []);
      assert.equal(fs.existsSync(receipt.directory), false, "owned compiler directory survived parent death");
    } finally {
      clearTimeout(timer);
      if (caller.exitCode === null && caller.signalCode === null) caller.kill("SIGKILL");
      if (receipt) exactProcesses([receipt.guardian, receipt], true);
    }
  });

  test("the real helper accepts reordered escaped request keys and fixed primitive results", () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "loom-windows-codec-"));
    owned.add(directory);
    const base = path.join(directory, "private space 日");
    const result = helperRequest(String.raw`{"\u006fp":"create","\u0062ase":${JSON.stringify(base)}}`);
    assert.equal(result.status, 0, result.stderr);
    const response = JSON.parse(result.stdout);
    assert.deepEqual(Object.keys(response).sort(), ["ok", "value"]);
    assert.equal(response.ok, true);
    assert.equal(typeof response.value, "string");
    assert.equal(response.value.length, 36);
    const root = { base, id: response.value };
    assert.equal(sessionIsProtected(root), false);
    bindProtectionAlias(root, "codex", "codec-results");
    assert.deepEqual(protectionForAlias("codex", "codec-results", { base }), root);
    assert.deepEqual(establishProtectionAlias("codex", "codec-results", { base }), root);
    assert.equal(findProtectionAlias("codex", "missing-codec-result", { base }), undefined);
    protectSession(root);
    assert.equal(sessionIsProtected(root), true);
  });

  test("the real helper refuses malformed and coerced requests before native authority", () => {
    const { root, directory } = fresh();
    bindProtectionAlias(root, "codex", "codec-refusals");
    protectSession(root);
    const state = fs.readFileSync(path.join(directory, "state"));
    const aliases = path.join(root.base, "aliases");
    const [claimName] = fs.readdirSync(aliases).filter((name) => name.endsWith(".claim"));
    const claim = path.join(aliases, claimName);
    const claimBytes = fs.readFileSync(claim);
    const entries = fs.readdirSync(root.base).sort();
    const base = root.base;
    const key = "a".repeat(64);
    const ordinary = { base, op: "find", key };
    for (const [name, input] of [
      ["malformed JSON", '{"op":'],
      ["trailing JSON", JSON.stringify(ordinary) + " true"],
      ["non-object JSON", "[]"],
      [
        "duplicate decoded key",
        String.raw`{"op":"find","\u006fp":"find","base":${JSON.stringify(base)},"key":"${key}"}`,
      ],
      ["unknown operation", JSON.stringify({ ...ordinary, op: "remove" })],
      ["extra key", JSON.stringify({ ...ordinary, extra: "" })],
      ["missing key", JSON.stringify({ base, op: "find" })],
      ["operation case", JSON.stringify({ ...ordinary, op: "FIND" })],
      ["key case", JSON.stringify({ Base: base, op: "find", key })],
      ["base coercion", JSON.stringify({ ...ordinary, base: 1 })],
      ["null base", JSON.stringify({ ...ordinary, base: null })],
      ["relative base", JSON.stringify({ ...ordinary, base: "relative" })],
      ["long base", JSON.stringify({ ...ordinary, base: "C:\\" + "a".repeat(2046) })],
      ["operation coercion", JSON.stringify({ ...ordinary, op: ["find"] })],
      ["id coercion", JSON.stringify({ base, op: "inspect", id: { id: root.id } })],
      ["key coercion", JSON.stringify({ ...ordinary, key: false })],
    ]) {
      const result = helperRequest(input);
      assert.equal(result.status, 1, `${name}: ${result.stderr}`);
      assert.deepEqual(JSON.parse(result.stdout), { ok: false }, name);
      assert.match(result.stderr, /^LSP1:request-parse:0\r?$/m, name);
      assert.doesNotMatch(result.stderr, /^LSP1:(?:native-op|record-construct|record-dispatch):/m, name);
      assert.deepEqual(fs.readdirSync(root.base).sort(), entries, name);
      assert.deepEqual(fs.readFileSync(path.join(directory, "state")), state, name);
      assert.deepEqual(fs.readFileSync(claim), claimBytes, name);
    }
    assert.equal(sessionIsProtected(root), true);
  });

  test("real Windows creation has token owner and one protected owner DACL", () => {
    const { root, directory } = fresh();
    assert.equal(sessionIsProtected(root), false);
    bindProtectionAlias(root, "claude-code", "actual-family");
    const [alias] = fs.readdirSync(path.join(root.base, "aliases")).filter((name) => name.endsWith(".json"));
    for (const file of [
      root.base,
      directory,
      path.join(directory, "root.json"),
      path.join(directory, "state"),
      path.join(root.base, "aliases"),
      path.join(root.base, "aliases", alias),
      path.join(root.base, "aliases", alias.replace(/\.json$/, ".claim")),
    ]) {
      const observed = acl("observe", file);
      assert.equal(observed.owner, observed.current);
      assert.equal(observed.protected, true);
      assert.deepEqual(observed.rules, [{ sid: observed.current, rights: 0x1f01ff, inherited: false, kind: "Allow" }]);
    }
    protectSession(root);
    assert.equal(sessionIsProtected(protectionForAlias("claude-code", "actual-family", { base: root.base })), true);
  });

  test("equivalent exact-key manifest and alias serializations remain usable", () => {
    const { root, directory } = fresh();
    bindProtectionAlias(root, "codex", "formatting");
    const manifest = path.join(directory, "root.json");
    fs.writeFileSync(manifest, JSON.stringify({ id: root.id, version: 1 }, null, 2));
    const [aliasName] = fs.readdirSync(path.join(root.base, "aliases")).filter((name) => name.endsWith(".json"));
    const alias = path.join(root.base, "aliases", aliasName);
    const saved = JSON.parse(fs.readFileSync(alias, "utf8"));
    fs.writeFileSync(alias, JSON.stringify({ root: saved.root, identity: saved.identity, version: 1 }, null, 2));
    assert.equal(sessionIsProtected(root), false);
    bindProtectionAlias(root, "codex", "formatting");
    protectSession(protectionForAlias("codex", "formatting", { base: root.base }));
    assert.equal(sessionIsProtected(root), true);
  });

  test("strict JSON keeps valid escaped keys, duplicate keys and numeric spellings", () => {
    const { root, directory } = fresh();
    const source = `{"version":{"overwritten":[null,true,[],{"x":1}]},"\\u0069d":"${root.id}","version":9,"version":1e0}`;
    assert.deepEqual(JSON.parse(source), { id: root.id, version: 1 });
    fs.writeFileSync(path.join(directory, "root.json"), source);
    assert.equal(sessionIsProtected(root), false);
    bindProtectionAlias(root, "claude-code", "strict-valid");
    protectSession(protectionForAlias("claude-code", "strict-valid", { base: root.base }));
    assert.equal(sessionIsProtected(root), true);
  });

  for (const malformed of [
    (id) => `{id:'${id}',version:1}`,
    (id) => `{"id":"${id}","version":01}`,
    (id) => `{"id":"${id}","version":1,}`,
    (id) => `{"id":"${id}",/*comment*/"version":1}`,
  ]) {
    test("non-JSON manifests cannot confirm clean authority", () => {
      const { root, directory } = fresh();
      const source = malformed(root.id);
      assert.throws(() => JSON.parse(source), SyntaxError);
      fs.writeFileSync(path.join(directory, "root.json"), source);
      assert.equal(sessionIsProtected(root), true);
      assert.throws(() => bindProtectionAlias(root, "claude-code", "bad-json"), SessionProtectionUnavailable);
      assert.throws(() => protectSession(root), SessionProtectionUnavailable);
      assert.equal(fs.readFileSync(path.join(directory, "state"), "utf8"), "0\n");
    });
  }

  test("a non-JSON alias cannot select an otherwise clean real root", () => {
    const { root, directory } = fresh();
    bindProtectionAlias(root, "claude-code", "bad-alias-json");
    const [name] = fs.readdirSync(path.join(root.base, "aliases")).filter((name) => name.endsWith(".json"));
    const alias = path.join(root.base, "aliases", name);
    const record = JSON.parse(fs.readFileSync(alias, "utf8"));
    const source = `{version:1,identity:'${record.identity}',root:'${root.id}'}`;
    assert.throws(() => JSON.parse(source), SyntaxError);
    fs.writeFileSync(alias, source);
    assert.throws(
      () => protectionForAlias("claude-code", "bad-alias-json", { base: root.base }),
      SessionProtectionUnavailable,
    );
    assert.throws(() => bindProtectionAlias(root, "claude-code", "bad-alias-json"), SessionProtectionUnavailable);
    assert.equal(fs.readFileSync(path.join(directory, "state"), "utf8"), "0\n");
  });

  for (const operation of ["broaden", "unprotect"]) {
    test(`an actual ${operation} DACL is refused without changing state`, () => {
      const { root, directory } = fresh();
      const state = path.join(directory, "state");
      acl(operation, state);
      assert.equal(sessionIsProtected(root), true);
      assert.throws(() => protectSession(root), SessionProtectionUnavailable);
      assert.equal(fs.readFileSync(state, "utf8"), "0\n");
    });
  }

  test("a real directory junction cannot substitute a different root", () => {
    const { root, directory } = fresh();
    const other = createProtectionRoot({ base: root.base });
    fs.rmSync(directory, { recursive: true });
    fs.symlinkSync(path.join(root.base, other.id), directory, "junction");
    assert.equal(sessionIsProtected(root), true);
    assert.throws(() => protectSession(root), SessionProtectionUnavailable);
    assert.equal(sessionIsProtected(other), false);
    fs.unlinkSync(directory);
  });

  test("compiler diagnostics expose only bounded numeric codes", () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "loom-windows-scanner-"));
    owned.add(directory);
    const formats =
      [
        "warning CS0168: synthetic-private-message",
        "error CS1234: synthetic-private-message",
        "error CS1234: synthetic-private-message",
        "fatal error CS2345: synthetic-private-message",
        String.raw`C:\synthetic-private\unit.cs(7,9): fatal error CS3456: synthetic-private-message`,
        "error CS4567: synthetic-private-message",
      ].join("\n") + "\n";
    const rejects =
      [
        "fatal warning CS0168: synthetic-private-message",
        "untrusted fatal error CS1234: synthetic-private-message",
        "fatal error CS12345: synthetic-private-message",
        "fatal error CS12x4: synthetic-private-message",
        "fatal error CS-1234: synthetic-private-message",
        "Fatal error CS1234: synthetic-private-message",
        "fatal error CS1234 synthetic-private-message",
      ].join("\n") + "\n";
    const prefix = "fatal error CS6789: synthetic-private-message\n";
    const bounded = prefix + "x".repeat(8192 - prefix.length) + "\nfatal error CS7890: not-read\n";
    const fixtures = { formats, rejects, bounded, empty: "" };
    for (const [label, text] of Object.entries(fixtures)) {
      fs.writeFileSync(path.join(directory, `${label}.log`), text, "ascii");
    }
    const source = String.raw`
      $ErrorActionPreference = 'Stop'
      . $env:LOOM_TEST_COMPILER_JOB
      function Write-LoomProtectionPhase([string]$phase, [int]$code = 0) {
        if ($phase -cnotin @('compile', 'compiler-bytes', 'compiler-format', 'compiler-scanned')) { throw 'Unexpected diagnostic phase' }
        if ($phase -ceq 'compile' -and ($code -lt 0 -or $code -gt 9999)) { throw 'Unexpected diagnostic code' }
        if ($phase -ceq 'compiler-bytes' -and ($code -lt 0 -or $code -gt 8192)) { throw 'Unexpected diagnostic count' }
        if ($phase -ceq 'compiler-format' -and ($code -lt 0 -or $code -gt 6)) { throw 'Unexpected diagnostic format' }
        if ($phase -ceq 'compiler-scanned' -and $code -ne 0) { throw 'Unexpected diagnostic status' }
        $script:scannerFrames.Add([ordered]@{ phase = $phase; code = $code })
      }
      $observed = @()
      foreach ($label in @('formats', 'rejects', 'bounded', 'empty')) {
        $script:scannerFrames = [Collections.Generic.List[object]]::new()
        Write-LoomCompilerCodes ([IO.Path]::Combine($env:LOOM_TEST_SCANNER_DIRECTORY, $label + '.log'))
        $observed += [ordered]@{ 'case' = $label; frames = $script:scannerFrames.ToArray() }
      }
      [Console]::Out.Write((ConvertTo-Json -InputObject $observed -Compress -Depth 4))
    `;
    const system32 = path.join(process.env.SystemRoot, "System32");
    const powershellHome = path.join(system32, "WindowsPowerShell", "v1.0");
    const result = spawnSync(
      path.join(powershellHome, "powershell.exe"),
      ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", source],
      {
        input: "",
        encoding: "utf8",
        windowsHide: true,
        cwd: system32,
        timeout: 2000,
        maxBuffer: 1024,
        env: {
          SystemRoot: process.env.SystemRoot,
          WINDIR: process.env.SystemRoot,
          PATH: system32,
          PSModulePath: path.join(powershellHome, "Modules"),
          LOOM_TEST_COMPILER_JOB: fileURLToPath(new URL("../lib/windows-compiler-job.ps1", import.meta.url)),
          LOOM_TEST_SCANNER_DIRECTORY: directory,
        },
      },
    );
    const childError = result.error
      ? ["ETIMEDOUT", "ENOBUFS", "ENOENT", "EACCES", "EPERM", "EPIPE"].includes(result.error.code)
        ? result.error.code
        : "OTHER"
      : "NONE";
    assert.equal(Boolean(result.error), false, `Scanner child error: ${childError}`);
    assert.equal(result.signal, null);
    assert.equal(result.status, 0);
    assert.equal(result.stderr.length, 0);
    let observed;
    try {
      observed = JSON.parse(result.stdout);
    } catch {
      assert.fail("Scanner observations were not JSON");
    }
    const frames = (codes, bytes, format = 6) => [
      ...codes.map((code) => ({ phase: "compile", code })),
      { phase: "compiler-bytes", code: bytes },
      { phase: "compiler-format", code: format },
      { phase: "compiler-scanned", code: 0 },
    ];
    const expected = [
      { case: "formats", frames: frames([168, 1234, 2345, 3456], Buffer.byteLength(formats, "ascii")) },
      { case: "rejects", frames: frames([], Buffer.byteLength(rejects, "ascii")) },
      { case: "bounded", frames: frames([6789], 8192) },
      { case: "empty", frames: frames([], 0, 0) },
    ];
    assert.equal(
      JSON.stringify(observed) === JSON.stringify(expected),
      true,
      "Scanner observations did not match fixed frames",
    );
  });
}
