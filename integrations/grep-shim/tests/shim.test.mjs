// The shim run as an agent runs it, differentially against the native tool:
// with no Loom reachable its stdout, stderr and exit status must equal the
// native tool's byte for byte; with Loom answering, stripping the per-line
// suffixes must give the native bytes back. Cases for a tool that is not
// installed are skipped.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { execFileSync, spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { MARK } from "../lib/annotate.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BIN = path.join(HERE, "..", "bin");

/** The first executable `name` on PATH outside the shim's own bin/. */
function nativeTool(name) {
  const own = fs.realpathSync(BIN);
  const exe = process.platform === "win32" ? `${name}.exe` : name;
  for (const dir of (process.env.PATH || "").split(path.delimiter)) {
    if (!dir) continue;
    const cand = path.join(dir, exe);
    try {
      fs.accessSync(cand, fs.constants.X_OK);
      if (fs.realpathSync(dir) === own || fs.realpathSync(cand).includes(`${path.sep}grep-shim${path.sep}`)) continue;
      return cand;
    } catch {
      /* next */
    }
  }
  return "";
}

const NATIVE = { grep: nativeTool("grep"), rg: nativeTool("rg") };

/** A checkout with text, a non-UTF-8 file and a binary file. */
function checkout() {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-grep-shim-")));
  fs.mkdirSync(path.join(dir, "src"));
  fs.writeFileSync(path.join(dir, "src", "a.cpp"), "int foo() {\n  return foo_impl();\n}\n// foo again\n");
  fs.writeFileSync(path.join(dir, "src", "b.cpp"), "void bar() { foo(); }\n");
  fs.writeFileSync(path.join(dir, "src", "latin1.cpp"), Buffer.from([0x2f, 0x2f, 0x20, 0x66, 0x6f, 0x6f, 0xe9, 0x0a]));
  fs.writeFileSync(path.join(dir, "src", "bin.dat"), Buffer.from([0x00, 0x66, 0x6f, 0x6f, 0x00, 0xff, 0x0a]));
  execFileSync("git", ["init", "-q"], { cwd: dir });
  return dir;
}

/** Run `file` with `args` in `cwd`; stdout as bytes. */
function runRaw(file, args, { cwd, env = {}, input, argv0 } = {}) {
  return new Promise((resolve) => {
    const started = Date.now();
    const child = spawn(file, args, {
      cwd,
      argv0,
      env: { PATH: process.env.PATH, HOME: cwd, LANG: "C", LC_ALL: "C", ...env },
      stdio: ["pipe", "pipe", "pipe"],
    });
    const out = [];
    const err = [];
    child.stdout.on("data", (c) => out.push(c));
    child.stderr.on("data", (c) => err.push(c));
    child.on("close", (code, signal) =>
      resolve({ code, signal, out: Buffer.concat(out), err: Buffer.concat(err).toString(), ms: Date.now() - started }),
    );
    child.stdin.on("error", () => {}); // a tool that never reads stdin closes it
    child.stdin.end(input ?? "");
  });
}

function shim(tool, args, opts = {}) {
  const env = { LOOM_GREP_REAL: NATIVE.grep, LOOM_RG_REAL: NATIVE.rg, ...(opts.env || {}) };
  return runRaw(process.execPath, [path.join(BIN, tool), ...args], { ...opts, env });
}

/** A code.enclosing stand-in: every location resolves to `<file>::fn<line>`. */
async function withServer(mode, fn) {
  const seen = [];
  const server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (c) => (raw += c));
    req.on("end", () => {
      seen.push({ url: req.url, body: raw ? JSON.parse(raw) : {} });
      if (mode === "hang") return;
      if (mode === 401) {
        res.statusCode = 401;
        return res.end("{}");
      }
      res.setHeader("content-type", "application/json");
      if (mode === "garbage") return res.end("{not json");
      const { locations = [] } = JSON.parse(raw || "{}");
      const results = locations.map((loc) => {
        const i = loc.lastIndexOf(":");
        const p = loc.slice(0, i);
        return {
          path: p,
          line: Number(loc.slice(i + 1)),
          qualified_name: `${path.basename(p)}::fn${loc.slice(i + 1)}`,
        };
      });
      res.end(JSON.stringify({ results, as_of: { commit: "abc1234567" } }));
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  try {
    return await fn(`http://127.0.0.1:${server.address().port}`, seen);
  } finally {
    server.closeAllConnections();
    server.close();
  }
}

const unannotated = (buf) =>
  buf
    .toString("latin1")
    .split("\n")
    .map((l) => {
      const i = l.indexOf(Buffer.from(MARK).toString("latin1"));
      return i < 0 ? l : l.slice(0, i);
    })
    .join("\n");

// Invocations covering the output shapes the promise names: annotated
// shapes, every pass-through shape, stdin, no match, a missing file, binary
// and non-UTF-8 input.
const GREP_CASES = [
  ["-rn", "foo", "src"],
  ["-n", "foo", "src/a.cpp"],
  ["-n", "foo", "src/a.cpp", "src/b.cpp"],
  ["-rnH", "-A1", "foo", "src"],
  ["-rnh", "foo", "src"],
  ["-rn", "--color=always", "foo", "src"],
  ["-rl", "foo", "src"],
  ["-rc", "foo", "src"],
  ["-rno", "foo", "src"],
  ["-rnZ", "foo", "src"],
  ["-rq", "foo", "src"],
  ["-rn", "zzz_nothing", "src"],
  ["-n", "foo", "src/missing.cpp"],
  ["-n", "foo", "src/bin.dat"],
  ["-na", "foo", "src/bin.dat"],
  ["-n", "foo", "src/latin1.cpp"],
  ["-rn", "foo"],
  ["-n", "foo"],
];

test("grep, Loom unreachable: every shape is byte-identical to native grep, exit status included", {
  skip: !NATIVE.grep && "no native grep on PATH",
}, async () => {
  const dir = checkout();
  for (const args of GREP_CASES) {
    const input = "foo on stdin\nbar\n";
    // Started as `grep`, as the shim starts it: Git for Windows' grep names
    // itself in errors by the path it was started with.
    const want = await runRaw(NATIVE.grep, args, { cwd: dir, input, argv0: "grep" });
    for (const env of [{}, { LOOM_API_KEY: "t", LOOM_BASE_URL: "http://127.0.0.1:9" }, { LOOM_GREP_SHIM: "off" }]) {
      const got = await shim("grep", args, { cwd: dir, input, env });
      const label = `grep ${args.join(" ")} ${JSON.stringify(env)}`;
      assert.equal(got.code, want.code, label);
      assert.equal(got.out.equals(want.out), true, `${label}\n${got.out}\n---\n${want.out}`);
      assert.equal(got.err.replaceAll(NATIVE.grep, "grep"), want.err.replaceAll(NATIVE.grep, "grep"), label);
    }
  }
});

test("grep, Loom answering: annotated shapes gain only suffixes; everything else is untouched", {
  skip: !NATIVE.grep && "no native grep on PATH",
}, async () => {
  const dir = checkout();
  await withServer("ok", async (url, seen) => {
    const env = { LOOM_API_KEY: "t", LOOM_BASE_URL: url, LOOM_GREP_SHIM_QUIET: "1" };
    for (const args of GREP_CASES) {
      const want = await runRaw(NATIVE.grep, args, { cwd: dir, input: "foo\n" });
      const got = await shim("grep", args, { cwd: dir, input: "foo\n", env });
      const label = `grep ${args.join(" ")}`;
      assert.equal(got.code, want.code, label);
      assert.equal(unannotated(got.out), want.out.toString("latin1"), label);
    }
    const r = await shim("grep", ["-rn", "foo", "src"], { cwd: dir, env });
    assert.match(r.out.toString(), new RegExp(`src/a\\.cpp:1:int foo\\(\\) \\{${MARK}a\\.cpp::fn1\n`));
    assert.ok(seen.every((s) => s.url === "/v1/code.enclosing" && s.body.consumer === "grep-shim"));
  });
});

test("grep, Loom answering: an operand reached through a symlink is the checkout's own path", {
  skip: (!NATIVE.grep && "no native grep on PATH") || (process.platform === "win32" && "symlinks need privileges"),
}, async () => {
  const dir = checkout();
  const link = `${dir}-link`;
  fs.symlinkSync(dir, link);
  try {
    await withServer("ok", async (url, seen) => {
      const env = { LOOM_API_KEY: "t", LOOM_BASE_URL: url, LOOM_GREP_SHIM_QUIET: "1" };
      const r = await shim("grep", ["-n", "foo", path.join(link, "src", "a.cpp")], { cwd: link, env });
      assert.match(r.out.toString(), new RegExp(`${MARK}a\\.cpp::fn1`));
      assert.ok(
        seen.some((s) => (s.body.locations || []).includes("src/a.cpp:1")),
        JSON.stringify(seen),
      );
    });
  } finally {
    fs.rmSync(link, { force: true });
  }
});

test("grep: output that is not valid UTF-8 is passed through as the same bytes", {
  skip: !NATIVE.grep && "no native grep on PATH",
}, async () => {
  const dir = checkout();
  await withServer("ok", async (url) => {
    const env = { LOOM_API_KEY: "t", LOOM_BASE_URL: url, LOOM_GREP_SHIM_QUIET: "1" };
    for (const args of [
      ["-rn", "foo", "src"],
      ["-n", "foo", "src/latin1.cpp"],
      ["-na", "foo", "src/bin.dat"],
    ]) {
      const want = await runRaw(NATIVE.grep, args, { cwd: dir });
      const got = await shim("grep", args, { cwd: dir, env });
      assert.equal(unannotated(got.out), want.out.toString("latin1"), args.join(" "));
    }
  });
});

test("grep: a 401, unparseable JSON or a server past the timeout all give native output", {
  skip: !NATIVE.grep && "no native grep on PATH",
}, async () => {
  const dir = checkout();
  const want = await runRaw(NATIVE.grep, ["-rn", "foo", "src"], { cwd: dir });
  for (const mode of [401, "garbage", "hang"]) {
    await withServer(mode, async (url, seen) => {
      const got = await shim("grep", ["-rn", "foo", "src"], {
        cwd: dir,
        env: { LOOM_API_KEY: "t", LOOM_BASE_URL: url, LOOM_GREP_SHIM_TIMEOUT_MS: "300" },
      });
      assert.equal(got.code, 0, String(mode));
      assert.equal(got.out.equals(want.out), true, String(mode));
      assert.equal(seen.length > 0, true, String(mode));
      assert.ok(got.ms < 3000, `${mode} took ${got.ms} ms`);
    });
  }
});

test("grep: past the buffer cap the output streams unannotated and whole", {
  skip: !NATIVE.grep && "no native grep on PATH",
}, async () => {
  const dir = checkout();
  fs.writeFileSync(path.join(dir, "src", "big.txt"), "foo line\n".repeat(50000));
  await withServer("ok", async (url) => {
    const env = { LOOM_API_KEY: "t", LOOM_BASE_URL: url, LOOM_GREP_SHIM_MAX_BYTES: "4096" };
    const want = await runRaw(NATIVE.grep, ["-rn", "foo", "src"], { cwd: dir });
    const got = await shim("grep", ["-rn", "foo", "src"], { cwd: dir, env });
    assert.equal(got.out.equals(want.out), true);
    assert.equal(got.code, 0);
  });
});

test("grep: a reader that closes the pipe early ends the shim quietly", {
  skip: (!NATIVE.grep && "no native grep on PATH") || (process.platform === "win32" && "no FIFOs"),
}, async () => {
  const dir = checkout();
  fs.writeFileSync(path.join(dir, "src", "big.txt"), "foo line\n".repeat(200000));
  // A real pipe, as in `grep ... | head`: a child's "pipe" stdio is a socket
  // pair, where a write after the reader closed with data unread can fail
  // with ECONNRESET instead of EPIPE, and native grep then reports it.
  const fifo = path.join(dir, "out.fifo");
  execFileSync("mkfifo", [fifo]);
  // The read end first, without blocking, so opening the write end returns.
  const reader = new net.Socket({ fd: fs.openSync(fifo, fs.constants.O_RDONLY | fs.constants.O_NONBLOCK) });
  const out = fs.openSync(fifo, "w");
  const r = await new Promise((resolve) => {
    const child = spawn(process.execPath, [path.join(BIN, "grep"), "-n", "foo", "src/big.txt"], {
      cwd: dir,
      env: { PATH: process.env.PATH, HOME: dir, LOOM_GREP_REAL: NATIVE.grep, LOOM_GREP_SHIM_MAX_BYTES: "4096" },
      stdio: ["ignore", out, "pipe"],
    });
    fs.closeSync(out);
    let err = "";
    child.stderr.on("data", (c) => (err += c));
    reader.once("data", () => reader.destroy());
    child.on("close", (code, signal) => resolve({ code, signal, err }));
  });
  assert.equal(r.err, "", "no stack trace on a closed pipe");
  assert.equal(r.signal === "SIGPIPE" || r.code === 141, true, `ended ${r.signal || r.code}`);
});

// Windows has no FIFOs; a child's "pipe" stdio there is a named pipe, which
// gives the writer EPIPE as a FIFO does. Git's grep.exe then dies of SIGPIPE
// and exits 13 << 8 (3328), which the shim reports as a shell would: 141.
test("grep: a reader that closes a Windows pipe early ends the shim quietly", {
  skip: (!NATIVE.grep && "no native grep on PATH") || (process.platform !== "win32" && "the FIFO case above"),
}, async () => {
  const dir = checkout();
  fs.writeFileSync(path.join(dir, "src", "big.txt"), "foo line\n".repeat(200000));
  const r = await new Promise((resolve) => {
    const child = spawn(process.execPath, [path.join(BIN, "grep"), "-n", "foo", "src/big.txt"], {
      cwd: dir,
      env: { PATH: process.env.PATH, HOME: dir, LOOM_GREP_REAL: NATIVE.grep, LOOM_GREP_SHIM_MAX_BYTES: "4096" },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let err = "";
    child.stderr.on("data", (c) => (err += c));
    child.stdout.once("data", () => child.stdout.destroy());
    child.on("close", (code, signal) => resolve({ code, signal, err }));
  });
  assert.equal(r.err, "", "no stack trace on a closed pipe");
  assert.equal(r.signal === "SIGPIPE" || r.code === 141, true, `ended ${r.signal || r.code}`);
});

test("grep, annotating: a reader that closes a socket stdout early ends the shim quietly", {
  skip: !NATIVE.grep && "no native grep on PATH",
}, async () => {
  const dir = checkout();
  fs.writeFileSync(path.join(dir, "src", "big.txt"), "foo line\n".repeat(200000));
  await withServer("ok", async (url) => {
    // A child's "pipe" stdio is a socket pair, as an agent's often is: a
    // reader that closes with data unread can show the shim ECONNRESET.
    const r = await new Promise((resolve) => {
      const child = spawn(process.execPath, [path.join(BIN, "grep"), "-n", "foo", "src/big.txt"], {
        cwd: dir,
        env: {
          PATH: process.env.PATH,
          HOME: dir,
          LOOM_GREP_REAL: NATIVE.grep,
          LOOM_API_KEY: "t",
          LOOM_BASE_URL: url,
          LOOM_GREP_SHIM_MAX_BYTES: "4096",
        },
        stdio: ["ignore", "pipe", "pipe"],
      });
      let err = "";
      child.stderr.on("data", (c) => (err += c));
      child.stdout.once("data", () => child.stdout.destroy());
      child.on("close", (code, signal) => resolve({ code, signal, err }));
    });
    assert.equal(r.err, "", "no stack trace and no write error");
    assert.equal(r.signal === "SIGPIPE" || r.code === 141, true, `ended ${r.signal || r.code}`);
  });
});

test("grep: a native grep killed by a signal is reported the same way", {
  skip: (!NATIVE.grep && "no native grep on PATH") || (process.platform === "win32" && "a sh fake and POSIX signals"),
}, async () => {
  const dir = checkout();
  const fake = path.join(dir, "fake-grep");
  fs.writeFileSync(fake, "#!/bin/sh\nkill -TERM $$\n");
  fs.chmodSync(fake, 0o755);
  for (const args of [
    ["-rn", "foo", "src"],
    ["-l", "foo", "src"],
  ]) {
    const got = await runRaw(process.execPath, [path.join(BIN, "grep"), ...args], {
      cwd: dir,
      env: { LOOM_GREP_REAL: fake },
    });
    assert.equal(got.signal, "SIGTERM", args.join(" "));
  }
});

test("grep: a second shim on PATH is never taken for the native tool", {
  skip: !NATIVE.grep && "no native grep on PATH",
}, async () => {
  const dir = checkout();
  const other = path.join(dir, "other-shim");
  fs.mkdirSync(other);
  fs.cpSync(path.join(HERE, ".."), path.join(other, "grep-shim"), { recursive: true, verbatimSymlinks: true });
  const want = await runRaw(NATIVE.grep, ["-rn", "foo", "src"], { cwd: dir });
  const PATHS = [path.join(other, "grep-shim", "bin"), BIN, path.dirname(NATIVE.grep), process.env.PATH].join(":");
  const got = await runRaw(process.execPath, [path.join(BIN, "grep"), "-rn", "foo", "src"], {
    cwd: dir,
    env: { PATH: PATHS },
  });
  assert.equal(got.code, 0);
  assert.equal(got.out.equals(want.out), true);
});

const RG_CASES = [
  ["-n", "foo", "src"],
  ["--column", "foo", "src"],
  ["-n", "foo", "src/a.cpp"],
  ["foo", "src/a.cpp"],
  ["-N", "foo", "src"],
  ["--vimgrep", "foo", "src/a.cpp"],
  ["-p", "foo", "src"],
  ["--heading", "-n", "foo", "src"],
  ["-E", "latin1", "foo", "src/latin1.cpp"],
  ["-l", "foo", "src"],
  ["-c", "foo", "src"],
  ["zzz_nothing", "src"],
  ["-r", "X", "foo", "src/a.cpp"],
  ["foo", "src/missing.cpp"],
];

test("rg, Loom unreachable: every shape is byte-identical to native rg", {
  skip: !NATIVE.rg && "no native rg on PATH",
}, async () => {
  const dir = checkout();
  for (const args of RG_CASES) {
    const want = await runRaw(NATIVE.rg, ["--sort=path", ...args], { cwd: dir });
    const got = await shim("rg", ["--sort=path", ...args], { cwd: dir });
    assert.equal(got.code, want.code, args.join(" "));
    assert.equal(got.out.equals(want.out), true, args.join(" "));
  }
});

test("rg, Loom answering: stripping the suffixes gives native rg's bytes", {
  skip: !NATIVE.rg && "no native rg on PATH",
}, async () => {
  const dir = checkout();
  await withServer("ok", async (url) => {
    const env = { LOOM_API_KEY: "t", LOOM_BASE_URL: url, LOOM_GREP_SHIM_QUIET: "1" };
    for (const args of RG_CASES) {
      const want = await runRaw(NATIVE.rg, ["--sort=path", ...args], { cwd: dir });
      const got = await shim("rg", ["--sort=path", ...args], { cwd: dir, env });
      assert.equal(got.code, want.code, args.join(" "));
      assert.equal(unannotated(got.out), want.out.toString("latin1"), args.join(" "));
    }
    const one = await shim("rg", ["-n", "foo", "src/a.cpp"], { cwd: dir, env });
    assert.match(one.out.toString(), new RegExp(`^1:int foo\\(\\) \\{${MARK}a\\.cpp::fn1\n`));
  });
});
