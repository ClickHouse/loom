// One checked native operation per Windows session-protection call.
import { spawnSync } from "node:child_process";
import crypto from "node:crypto";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

// Startup configuration is a locator under the process-owner trust model.
// Hook/session data can select neither the executable nor the fixed source.
const startupSystemRoot = process.env.SystemRoot;
const startupHome = os.homedir();
const script = fileURLToPath(new URL("./windows-session-protection.ps1", import.meta.url));
const operationTimeoutMs = 2000;
const cleanupReserveMs = 500;
const failureDiagnostics = new WeakMap();
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
  "protect-open",
  "protect-flushed",
  "output-encode",
  "cleanup",
  "protocol",
]);
const boundedCode = (value) => (Number.isInteger(value) && value >= -2147483648 && value <= 2147483647 ? value : null);
const boundedCount = (value) => Math.min(1024, Math.max(0, Number.isSafeInteger(value) ? value : 0));
const unavailable = (diagnostic) => {
  const error = new Error("Session protection state could not be confirmed");
  if (diagnostic) failureDiagnostics.set(error, Object.freeze(diagnostic));
  return error;
};

export const windowsFailureDiagnostic = (error) => failureDiagnostics.get(error);

function helperDiagnostic(stderr) {
  let phase = "unknown",
    failurePhase = null,
    errorCode = null,
    workerExit = null;
  let unexpected = false;
  let validFrames = 0,
    rejectedLines = 0;
  const lines = (stderr ?? "").replace(/^\uFEFF/, "").split(/\r?\n/);
  if (lines.at(-1) === "") lines.pop();
  for (const line of lines) {
    const match = /^LSP1:([a-z-]+):(-?[0-9]{1,10})$/.exec(line);
    const code = match ? boundedCode(Number(match[2])) : null;
    if (
      !match ||
      code === null ||
      (match[1] === "compiler-bytes" && (code < 0 || code > 8192)) ||
      (match[1] === "compiler-format" && (code < 0 || code > 6)) ||
      (["root-entry", "root-flushed", "protect-open", "protect-flushed"].includes(match[1]) && code !== 0) ||
      (!phases.has(match[1]) && match[1] !== "worker-exit" && match[1] !== "helper-exit")
    ) {
      unexpected = true;
      rejectedLines = boundedCount(rejectedLines + 1);
      continue;
    }
    validFrames = boundedCount(validFrames + 1);
    if (match[1] === "compiler-bytes" || match[1] === "compiler-format") continue;
    if (match[1] === "worker-exit") {
      workerExit = code;
      if (code !== 0 && failurePhase === null) failurePhase = phase;
    } else if (match[1] === "helper-exit") {
      if (code !== 0 && failurePhase === null) failurePhase = phase;
    } else if (phases.has(match[1])) {
      phase = match[1];
      if (code !== 0) {
        if (failurePhase === null) failurePhase = phase;
        if (errorCode === null) errorCode = code;
      }
    } else unexpected = true;
  }
  return {
    phase: failurePhase ?? phase,
    lastValidatedPhase: phase,
    errorCode,
    workerExit,
    unexpected,
    failed: failurePhase !== null,
    validFrames,
    rejectedLines,
  };
}

function failedOperation(result, started, phase) {
  const helper = helperDiagnostic(result.stderr);
  return unavailable({
    phase: phase ?? helper.phase,
    lastValidatedPhase: helper.lastValidatedPhase,
    exitCode: boundedCode(result.status),
    workerExit: helper.workerExit,
    signal: result.signal === "SIGTERM" ? 15 : result.signal === "SIGKILL" ? 9 : result.signal ? -1 : 0,
    errorCode: boundedCode(result.error?.errno) ?? helper.errorCode,
    helperErrorCode: helper.errorCode,
    elapsedMs: Math.min(60000, Math.max(0, Math.round(performance.now() - started))),
    timedOut: result.error?.code === "ETIMEDOUT",
    stdoutUnits: boundedCount(result.stdout?.length),
    stderrUnits: boundedCount(result.stderr?.length),
    validFrames: helper.validFrames,
    rejectedLines: helper.rejectedLines,
  });
}

export function windowsSessionProtection(op, args) {
  if (process.platform !== "win32" || !startupSystemRoot || !path.isAbsolute(startupSystemRoot)) throw unavailable();
  if (!["create", "inspect", "protect", "bind", "find", "establish"].includes(op)) throw unavailable();
  if (!args || typeof args.base !== "string" || !path.isAbsolute(args.base) || args.base.length > 2048)
    throw unavailable();
  if (["inspect", "protect", "bind"].includes(op) && !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(args.id)) {
    throw unavailable();
  }
  if (["bind", "find", "establish"].includes(op) && !/^[0-9a-f]{64}$/.test(args.key)) throw unavailable();
  const request = JSON.stringify({ ...args, op });
  if (Buffer.byteLength(request, "utf8") > 4096) throw unavailable();
  const system32 = path.join(startupSystemRoot, "System32");
  const powershellHome = path.join(system32, "WindowsPowerShell", "v1.0");
  const compilationId = crypto.randomBytes(16).toString("hex");
  const started = performance.now();
  const deadline = started + operationTimeoutMs;
  const executable = path.join(powershellHome, "powershell.exe");
  const arguments_ = ["-NoLogo", "-NoProfile", "-NonInteractive", "-File", script];
  const options = {
    encoding: "utf8",
    windowsHide: true,
    cwd: system32,
    maxBuffer: 1024,
    env: {
      SystemRoot: startupSystemRoot,
      WINDIR: startupSystemRoot,
      PATH: system32,
      PSModulePath: path.join(powershellHome, "Modules"),
      LOOM_PRIVATE_COMPILER_BASE: path.join(startupHome, ".loom", "session-protection-compiler"),
      LOOM_PRIVATE_COMPILATION_ID: compilationId,
      LOOM_PRIVATE_DEADLINE_UTC: String(Date.now() + operationTimeoutMs - cleanupReserveMs),
    },
  };
  const result = spawnSync(executable, arguments_, {
    ...options,
    input: request,
    timeout: operationTimeoutMs - cleanupReserveMs,
  });
  if (result.error || result.signal) {
    // Cleanup has its own exact invocation receipt, no compiler and no retry
    // of the failed session operation. It shares the original overall budget.
    const remaining = Math.floor(deadline - performance.now());
    let cleanupConfirmed = false;
    if (remaining > 0) {
      const cleanup = spawnSync(executable, [...arguments_, "-CleanupCompilation"], {
        ...options,
        env: { ...options.env, LOOM_PRIVATE_DEADLINE_UTC: String(Date.now() + remaining) },
        input: "",
        timeout: remaining,
      });
      const cleanupDiagnostic = helperDiagnostic(cleanup.stderr);
      if (
        !cleanup.error &&
        !cleanup.signal &&
        cleanup.status === 0 &&
        !cleanupDiagnostic.unexpected &&
        !cleanupDiagnostic.failed
      ) {
        try {
          const response = JSON.parse(cleanup.stdout.replace(/^\uFEFF/, ""));
          cleanupConfirmed =
            response?.ok === true && response.value === null && Object.keys(response).sort().join(",") === "ok,value";
        } catch {
          cleanupConfirmed = false;
        }
      }
    }
    const error = failedOperation(result, started);
    error.cleanupPending = !cleanupConfirmed;
    throw error;
  }
  const helper = helperDiagnostic(result.stderr);
  if (result.error || result.signal || result.status !== 0 || helper.unexpected || helper.failed)
    throw failedOperation(result, started);
  let response;
  try {
    response = JSON.parse(result.stdout.replace(/^\uFEFF/, ""));
  } catch {
    throw failedOperation(result, started, "protocol");
  }
  if (!response || Object.keys(response).sort().join(",") !== "ok,value" || response.ok !== true)
    throw failedOperation(result, started, "protocol");
  const value = response.value;
  if (
    ["create", "establish"].includes(op) &&
    (typeof value !== "string" || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(value))
  )
    throw failedOperation(result, started, "protocol");
  if (op === "inspect" && typeof value !== "boolean") throw failedOperation(result, started, "protocol");
  if (["protect", "bind"].includes(op) && value !== null) throw failedOperation(result, started, "protocol");
  if (
    op === "find" &&
    value !== null &&
    (typeof value !== "string" || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(value))
  )
    throw failedOperation(result, started, "protocol");
  return value;
}
