// bin/loom-node finds Node 18+ for the hooks and the MCP header helper when
// the agent's PATH has none: each install layout in a temp HOME, a too-old
// Node, none at all, paths with spaces, and every wiring going through it.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const codexRoot = path.join(root, "..", "codex-plugin");
const launcher = path.join(root, "bin", "loom-node");
const WINDOWS = process.platform === "win32";

// Every POSIX shell on this machine: dash is Debian's sh, bash is macOS's.
// On Windows, Git Bash's sh, by path: the tests empty PATH.
const SHELLS = WINDOWS
  ? [path.join(process.env.ProgramFiles || "C:\\Program Files", "Git", "bin", "sh.exe")]
  : ["/bin/sh", "/bin/dash", "/bin/bash", "/bin/busybox"].filter((s) => fs.existsSync(s));

// Windows needs SystemRoot to start a process.
const BASE_ENV = WINDOWS ? { SystemRoot: process.env.SystemRoot } : {};

// A path as Git Bash prints it, or as a colon-separated list needs it: /c/x/y.
const shPath = (p) => (WINDOWS ? p.replace(/\\/g, "/").replace(/^([A-Za-z]):/, (_, d) => `/${d.toLowerCase()}`) : p);

function fixture() {
  const dir = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir(), "loom node ")));
  const home = path.join(dir, "home");
  fs.mkdirSync(home);
  const script = path.join(dir, "argv.mjs");
  fs.writeFileSync(script, "process.stdout.write(JSON.stringify(process.argv.slice(2)));\n");
  // A fake Node at <rel>/node: answers --version, else runs the script
  // under this test's Node. On Windows, a hard link to this Node as node.exe.
  const node = (rel, version = "v22.1.0") => {
    const d = path.join(home, rel);
    fs.mkdirSync(d, { recursive: true });
    if (WINDOWS) {
      fs.linkSync(process.execPath, path.join(d, "node.exe"));
      return path.join(d, "node.exe");
    }
    const p = path.join(d, "node");
    fs.writeFileSync(
      p,
      `#!/bin/sh\nif [ "$1" = --version ]; then echo ${version}; exit 0; fi\nexec "${process.execPath}" "$@"\n`,
      { mode: 0o755 },
    );
    return p;
  };
  const run = (env, args = ["--check"], shell = SHELLS[0]) => {
    const argv = shell.endsWith("busybox") ? ["sh", launcher, ...args] : [launcher, ...args];
    return spawnSync(shell, argv, {
      encoding: "utf8",
      // Only what the test builds: no node on PATH, no fixed directories.
      env: { ...BASE_ENV, HOME: home, PATH: path.join(dir, "empty"), LOOM_NODE_SYSTEM_DIRS: "", ...env },
    });
  };
  // Windows may still hold a just-run node.exe open for a moment.
  const done = () => fs.rmSync(dir, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
  return { dir, home, script, node, run, done };
}

function found(f, r) {
  assert.equal(r.status, 0, r.stderr);
  const m = r.stdout.match(/^Loom hook runtime ready: (.+) \((v[\d.]+)\)\n$/);
  assert.ok(m, r.stdout);
  const home = `${shPath(f.home)}/`.toLowerCase();
  const got = shPath(m[1]);
  assert.ok(got.toLowerCase().startsWith(home), `${got} is not under ${home}`);
  // Git Bash opens node.exe as `node`: compare without the suffix.
  return got.slice(home.length).replace(/\.exe$/, "");
}

const exe = (rel) => `${rel}/node`;

// [label, layout to build, env, the one expected]
const LAYOUTS = [
  ["fnm default alias", [".local/share/fnm/aliases/default/bin"], {}, ".local/share/fnm/aliases/default/bin"],
  [
    "fnm under XDG_DATA_HOME",
    ["xdg/fnm/aliases/default/bin"],
    (f) => ({ XDG_DATA_HOME: path.join(f.home, "xdg") }),
    "xdg/fnm/aliases/default/bin",
  ],
  [
    "fnm on macOS, a path with a space",
    ["Library/Application Support/fnm/aliases/default/bin"],
    {},
    "Library/Application Support/fnm/aliases/default/bin",
  ],
  [
    "fnm newest install without an alias",
    [
      ".fnm/node-versions/v20.9.0/installation/bin",
      ".fnm/node-versions/v22.11.0/installation/bin",
      ".fnm/node-versions/v22.2.0/installation/bin",
    ],
    {},
    ".fnm/node-versions/v22.11.0/installation/bin",
  ],
  [
    "nvm newest",
    [".nvm/versions/node/v18.20.0/bin", ".nvm/versions/node/v22.1.0/bin", ".nvm/versions/node/v9.11.0/bin"],
    {},
    ".nvm/versions/node/v22.1.0/bin",
  ],
  [
    "NVM_DIR",
    ["n v m/versions/node/v20.0.0/bin"],
    (f) => ({ NVM_DIR: path.join(f.home, "n v m") }),
    "n v m/versions/node/v20.0.0/bin",
  ],
  ["volta", [".volta/bin"], {}, ".volta/bin"],
  ["asdf", [".asdf/installs/nodejs/20.11.1/bin"], {}, ".asdf/installs/nodejs/20.11.1/bin"],
  [
    "asdf default in ~/.tool-versions",
    [".asdf/installs/nodejs/20.11.1/bin", ".asdf/installs/nodejs/22.2.0/bin", ".tool-versions-file"],
    {},
    ".asdf/installs/nodejs/20.11.1/bin",
  ],
  ["mise", [".local/share/mise/installs/node/22.3.0/bin"], {}, ".local/share/mise/installs/node/22.3.0/bin"],
  ["nodenv", [".nodenv/versions/21.7.3/bin"], {}, ".nodenv/versions/21.7.3/bin"],
  [
    "nodenv default in its version file",
    [".nodenv/versions/20.0.0/bin", ".nodenv/versions/21.7.3/bin", ".nodenv-version-file"],
    {},
    ".nodenv/versions/20.0.0/bin",
  ],
  [
    "n",
    ["n/n/versions/node/22.0.0/bin"],
    (f) => ({ N_PREFIX: path.join(f.home, "n") }),
    "n/n/versions/node/22.0.0/bin",
  ],
  [
    "n's active version over a newer cached one",
    ["n/n/versions/node/22.0.0/bin", "n/bin"],
    (f) => ({ N_PREFIX: path.join(f.home, "n") }),
    "n/bin",
  ],
  ["nodebrew", [".nodebrew/current/bin"], {}, ".nodebrew/current/bin"],
  [
    "a fixed directory with a space",
    ["Program Files/nodejs"],
    (f) => ({ LOOM_NODE_SYSTEM_DIRS: `${shPath(f.home)}/nowhere:${shPath(f.home)}/Program Files/nodejs` }),
    "Program Files/nodejs",
  ],
  // Windows layouts, found under Git Bash through these variables, which
  // hold backslash paths there.
  [
    "nvm-windows active version",
    ["AppData/Roaming/nvm/v20.1.0", "nodejs"],
    (f) => ({ APPDATA: path.join(f.home, "AppData", "Roaming"), NVM_SYMLINK: path.join(f.home, "nodejs") }),
    "nodejs",
  ],
  [
    "nvm-windows newest",
    ["AppData/Roaming/nvm/v20.1.0", "AppData/Roaming/nvm/v22.4.0"],
    (f) => ({
      APPDATA: path.join(f.home, "AppData", "Roaming"),
      NVM_HOME: path.join(f.home, "AppData", "Roaming", "nvm"),
    }),
    "AppData/Roaming/nvm/v22.4.0",
  ],
  [
    "fnm on Windows",
    ["AppData/Roaming/fnm/aliases/default"],
    (f) => ({ APPDATA: path.join(f.home, "AppData", "Roaming") }),
    "AppData/Roaming/fnm/aliases/default",
  ],
  [
    "Volta on Windows",
    ["AppData/Local/Volta/bin"],
    (f) => ({ LOCALAPPDATA: path.join(f.home, "AppData", "Local") }),
    "AppData/Local/Volta/bin",
  ],
  ["Scoop", ["scoop/apps/nodejs-lts/current"], (f) => ({ LOCALAPPDATA: f.home }), "scoop/apps/nodejs-lts/current"],
  [
    "the Windows installer",
    ["PF/nodejs"],
    (f) => ({ APPDATA: f.home, PROGRAMFILES: path.join(f.home, "PF") }),
    "PF/nodejs",
  ],
];

for (const shell of SHELLS) {
  for (const [label, dirs, env, want] of LAYOUTS) {
    test(`${path.basename(shell)}: ${label}`, () => {
      const f = fixture();
      try {
        for (const d of dirs) {
          if (d === ".tool-versions-file")
            // a hand-edited file may lack the final newline
            fs.writeFileSync(path.join(f.home, ".tool-versions"), "python 3.12.1\nnodejs 20.11.1");
          else if (d === ".nodenv-version-file") fs.writeFileSync(path.join(f.home, ".nodenv", "version"), "20.0.0\n");
          else f.node(d);
        }
        const r = f.run(typeof env === "function" ? env(f) : env, ["--check"], shell);
        assert.equal(found(f, r), exe(want));
      } finally {
        f.done();
      }
    });
  }
}

test("node on PATH wins, and runs the script with its arguments, stdin and exit code", () => {
  const f = fixture();
  try {
    const p = f.node("bin dir");
    f.node(".nvm/versions/node/v22.9.0/bin");
    const r = f.run({ PATH: path.dirname(p) });
    assert.equal(found(f, r), exe("bin dir"));
    const ran = f.run({ PATH: path.dirname(p) }, [f.script, "a b", "", "$HOME"]);
    assert.equal(ran.status, 0, ran.stderr);
    assert.deepEqual(JSON.parse(ran.stdout), ["a b", "", "$HOME"]);
    const exit = path.join(f.dir, "exit.mjs");
    fs.writeFileSync(
      exit,
      "let s='';process.stdin.on('data',(c)=>s+=c).on('end',()=>{process.stdout.write(s);process.exit(3)});\n",
    );
    const piped = spawnSync(SHELLS[0], [launcher, exit], {
      input: '{"hook":1}',
      encoding: "utf8",
      env: { ...BASE_ENV, HOME: f.home, PATH: path.dirname(p), LOOM_NODE_SYSTEM_DIRS: "" },
    });
    assert.equal(piped.status, 3);
    assert.equal(piped.stdout, '{"hook":1}');
    assert.equal(piped.stderr, "");
  } finally {
    f.done();
  }
});

test("the nvm default alias wins over a newer install", () => {
  const f = fixture();
  try {
    f.node(".nvm/versions/node/v20.11.0/bin");
    f.node(".nvm/versions/node/v20.9.0/bin");
    f.node(".nvm/versions/node/v22.1.0/bin");
    fs.mkdirSync(path.join(f.home, ".nvm", "alias"));
    fs.writeFileSync(path.join(f.home, ".nvm", "alias", "default"), "20\n");
    assert.equal(found(f, f.run({})), exe(".nvm/versions/node/v20.11.0/bin"));
    // 20.1 is 20.1.x, not 20.11.0
    f.node(".nvm/versions/node/v20.1.0/bin");
    fs.writeFileSync(path.join(f.home, ".nvm", "alias", "default"), "20.1\n");
    assert.equal(found(f, f.run({})), exe(".nvm/versions/node/v20.1.0/bin"));
    // lts/iron's file names the version; lts/* names lts/iron (no `*` file on Windows)
    fs.mkdirSync(path.join(f.home, ".nvm", "alias", "lts"));
    fs.writeFileSync(path.join(f.home, ".nvm", "alias", "lts", "iron"), "v20.11.0"); // no final newline
    fs.writeFileSync(path.join(f.home, ".nvm", "alias", "default"), "lts/iron\n");
    assert.equal(found(f, f.run({})), exe(".nvm/versions/node/v20.11.0/bin"));
    if (!WINDOWS) {
      fs.writeFileSync(path.join(f.home, ".nvm", "alias", "lts", "*"), "lts/iron\n");
      fs.writeFileSync(path.join(f.home, ".nvm", "alias", "default"), "lts/*\n");
      assert.equal(found(f, f.run({})), exe(".nvm/versions/node/v20.11.0/bin"));
    }
    fs.writeFileSync(path.join(f.home, ".nvm", "alias", "default"), "node\n");
    assert.equal(found(f, f.run({})), exe(".nvm/versions/node/v22.1.0/bin"));
  } finally {
    f.done();
  }
});

test("LOOM_NODE wins, and a LOOM_NODE that is not Node fails rather than falling through", () => {
  const f = fixture();
  try {
    f.node(".volta/bin");
    const chosen = f.node("my node");
    assert.equal(found(f, f.run({ LOOM_NODE: chosen })), exe("my node"));
    const r = f.run({ LOOM_NODE: path.join(f.home, "missing") });
    assert.equal(r.status, 1);
    assert.equal(r.stdout, "");
    assert.match(r.stderr, /^loom: LOOM_NODE=.*missing is not Node\.js 18\+\.\n$/);
    if (!WINDOWS) {
      const old = f.node("old", "v16.20.2");
      const tooOld = f.run({ LOOM_NODE: old });
      assert.equal(tooOld.stderr, `loom: LOOM_NODE=${old} is not Node.js 18+ (it is v16.20.2).\n`);
    }
  } finally {
    f.done();
  }
});

test("no Node anywhere: one line naming LOOM_NODE, nothing on stdout, exit 1", { skip: WINDOWS }, () => {
  const f = fixture();
  try {
    const r = f.run({}, [f.script]);
    assert.equal(r.status, 1);
    assert.equal(r.stdout, "");
    assert.equal(
      r.stderr,
      "loom: Node.js 18+ not found on PATH or in the usual install locations. Install it, or set LOOM_NODE=/path/to/node.\n",
    );
  } finally {
    f.done();
  }
});

test("a too-old Node on PATH is skipped for a newer one, and the first old one is named", { skip: WINDOWS }, () => {
  const f = fixture();
  try {
    const old = f.node("old", "v16.20.2");
    f.node(".volta/bin");
    assert.equal(found(f, f.run({ PATH: path.dirname(old) })), ".volta/bin/node");
    fs.rmSync(path.join(f.home, ".volta"), { recursive: true });
    f.node(".nvm/versions/node/v14.21.3/bin", "v14.21.3");
    const r = f.run({ PATH: path.dirname(old) });
    assert.equal(r.status, 1);
    assert.equal(
      r.stderr,
      `loom: Node.js 18+ is required; the first Node found, ${old} (v16.20.2), is older. Install a newer one, or set LOOM_NODE=/path/to/node.\n`,
    );
  } finally {
    f.done();
  }
});

function commands(file) {
  const j = JSON.parse(fs.readFileSync(file, "utf8"));
  if (j.events) return Object.values(j.events).map((e) => [e.command]);
  return Object.values(j.hooks).flatMap((groups) =>
    groups.flatMap((g) => g.hooks.map((h) => [h.command, h.commandWindows])),
  );
}

test("every hook and both header helpers start Node through the launcher", () => {
  const wiring = [
    [path.join(root, "hooks", "hooks.json"), 'sh "${CLAUDE_PLUGIN_ROOT}/bin/loom-node" "${CLAUDE_PLUGIN_ROOT}/hooks/'],
    [path.join(root, "hooks", "hooks.generic.json"), "sh ${LOOM_PLUGIN_DIR}/bin/loom-node ${LOOM_PLUGIN_DIR}/hooks/"],
    [path.join(codexRoot, "hooks", "hooks.json"), 'sh "${PLUGIN_ROOT}/bin/loom-node" "${PLUGIN_ROOT}/hooks/'],
  ];
  for (const [file, prefix] of wiring) {
    const all = commands(file);
    assert.ok(all.length >= 10, file);
    for (const [command, windows] of all) {
      assert.ok(command.startsWith(prefix), `${file}: ${command}`);
      // Codex runs a hook through cmd.exe on Windows, which has no sh.
      if (file.startsWith(codexRoot))
        assert.equal(windows, command.replace('sh "${PLUGIN_ROOT}/bin/loom-node" ', "node "));
    }
  }
  const claude = JSON.parse(fs.readFileSync(path.join(root, ".mcp.json"), "utf8")).mcpServers.loom;
  assert.equal(
    claude.headersHelper,
    'sh "$CLAUDE_PLUGIN_ROOT/bin/loom-node" "$CLAUDE_PLUGIN_ROOT/bin/loom-mcp-headers.mjs"',
  );
  const codex = JSON.parse(fs.readFileSync(path.join(codexRoot, ".mcp.json"), "utf8")).mcpServers.loom;
  assert.match(
    codex.http_headers_helper,
    /\/bin\/loom-node 2>\/dev\/null \| sort -V \| tail -1\)"; sh "\$l" "\$\{l%\/\*\}\/loom-mcp-headers\.mjs"$/,
  );
});

test("the Codex header helper runs the newest loom@loom launcher and the helper beside it", {
  skip: WINDOWS,
}, () => {
  const f = fixture();
  try {
    f.node(".nvm/versions/node/v22.1.0/bin");
    // Only the tools the helper's command line needs, and no node.
    const tools = path.join(f.dir, "tools");
    fs.mkdirSync(tools);
    for (const t of ["sh", "ls", "sort", "tail"]) {
      const real = spawnSync("/bin/sh", ["-c", `command -v ${t}`], { encoding: "utf8" }).stdout.trim();
      fs.symlinkSync(real, path.join(tools, t));
    }
    const cached = (marketplace, v, withLauncher = true) => {
      const bin = path.join(f.home, ".codex", "plugins", "cache", marketplace, "loom", v, "bin");
      fs.mkdirSync(bin, { recursive: true });
      if (withLauncher) fs.copyFileSync(launcher, path.join(bin, "loom-node"));
      fs.writeFileSync(path.join(bin, "loom-mcp-headers.mjs"), `process.stdout.write(${JSON.stringify(v)});\n`);
    };
    cached("loom", "1.2.9");
    cached("loom", "1.2.10");
    // Not picked: a newer copy without a launcher (an interrupted install), and
    // another marketplace's copy.
    cached("loom", "1.2.11-pre", false);
    cached("zz", "1.3.0");
    const helper = JSON.parse(fs.readFileSync(path.join(codexRoot, ".mcp.json"), "utf8")).mcpServers.loom
      .http_headers_helper;
    // Codex's cleared environment: HOME, and a Node only under nvm.
    const r = spawnSync("/bin/sh", ["-c", helper], {
      encoding: "utf8",
      env: { HOME: f.home, PATH: tools, LOOM_NODE_SYSTEM_DIRS: "" },
    });
    assert.equal(r.status, 0, r.stderr);
    assert.equal(r.stdout, "1.2.10");
  } finally {
    f.done();
  }
});

test("a manager's default that is too old falls through to its newest install", { skip: WINDOWS }, () => {
  const f = fixture();
  try {
    f.node(".nvm/versions/node/v16.20.2/bin", "v16.20.2");
    f.node(".nvm/versions/node/v20.11.0/bin");
    fs.mkdirSync(path.join(f.home, ".nvm", "alias"));
    fs.writeFileSync(path.join(f.home, ".nvm", "alias", "default"), "16\n");
    assert.equal(found(f, f.run({})), exe(".nvm/versions/node/v20.11.0/bin"));
  } finally {
    f.done();
  }
});

test("managers are tried in order: fnm before nvm before Volta", () => {
  const f = fixture();
  try {
    f.node(".volta/bin");
    f.node(".nvm/versions/node/v22.1.0/bin");
    assert.equal(found(f, f.run({})), exe(".nvm/versions/node/v22.1.0/bin"));
    f.node(".local/share/fnm/aliases/default/bin");
    assert.equal(found(f, f.run({})), exe(".local/share/fnm/aliases/default/bin"));
  } finally {
    f.done();
  }
});

test("with no script it prints its usage instead of starting a REPL on the hook payload", () => {
  const f = fixture();
  try {
    f.node("bin");
    const r = f.run({ PATH: path.join(f.home, "bin") }, []);
    assert.equal(r.status, 1);
    assert.equal(r.stdout, "");
    assert.equal(r.stderr, "usage: loom-node <script> [args...] | --check\n");
  } finally {
    f.done();
  }
});

test("an install directory without a Node does not hide an older one", { skip: WINDOWS }, () => {
  const f = fixture();
  try {
    f.node(".nvm/versions/node/v22.1.0/bin");
    fs.mkdirSync(path.join(f.home, ".nvm", "versions", "node", "v23.0.0"), { recursive: true });
    assert.equal(found(f, f.run({})), exe(".nvm/versions/node/v22.1.0/bin"));
  } finally {
    f.done();
  }
});

test("--check names the real directory, not a per-shell link to it", { skip: WINDOWS }, () => {
  const f = fixture();
  try {
    const real = path.dirname(f.node(".local/share/fnm/node-versions/v22.1.0/installation/bin"));
    const link = path.join(f.home, "fnm_multishells", "123_456");
    fs.mkdirSync(path.dirname(link));
    fs.symlinkSync(path.dirname(real), link);
    assert.equal(
      found(f, f.run({ PATH: path.join(link, "bin") })),
      exe(".local/share/fnm/node-versions/v22.1.0/installation/bin"),
    );
  } finally {
    f.done();
  }
});

test("versions are tried from the highest down: one --version for the newest", { skip: WINDOWS }, () => {
  const f = fixture();
  try {
    const log = path.join(f.dir, "versions.log");
    for (const v of ["v18.20.4", "v20.11.1", "v22.1.0"]) {
      const bin = path.join(f.home, ".nvm", "versions", "node", v, "bin");
      fs.mkdirSync(bin, { recursive: true });
      fs.writeFileSync(path.join(bin, "node"), `#!/bin/sh\necho ${v} >> "${log}"\necho ${v}\n`, { mode: 0o755 });
    }
    assert.equal(found(f, f.run({})), exe(".nvm/versions/node/v22.1.0/bin"));
    assert.equal(fs.readFileSync(log, "utf8"), "v22.1.0\n");
  } finally {
    f.done();
  }
});

test("--check prints a stable link, such as fnm's default alias, as it is", { skip: WINDOWS }, () => {
  const f = fixture();
  try {
    const installation = path.dirname(path.dirname(f.node(".local/share/fnm/node-versions/v22.1.0/installation/bin")));
    const aliases = path.join(f.home, ".local", "share", "fnm", "aliases");
    fs.mkdirSync(aliases);
    fs.symlinkSync(installation, path.join(aliases, "default"));
    assert.equal(found(f, f.run({})), exe(".local/share/fnm/aliases/default/bin"));
  } finally {
    f.done();
  }
});
