#!/usr/bin/env node
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

function hostPlatform() {
  let arch = process.arch;
  if (process.platform === "darwin" && arch === "x64") {
    try {
      if (
        execFileSync("/usr/sbin/sysctl", ["-n", "sysctl.proc_translated"], {
          encoding: "utf8",
          timeout: 1000,
          stdio: ["ignore", "pipe", "ignore"],
        }).trim() === "1"
      )
        arch = "arm64";
    } catch (error) {
      if (error.status !== 1) throw error;
    }
  }
  const musl = process.platform === "linux" && !process.report.getReport().header.glibcVersionRuntime;
  return { platform: process.platform, arch, musl };
}

function nativeInfo(tool, { platform, arch, musl = false }) {
  if (!["x64", "arm64"].includes(arch)) throw new Error(`Unsupported native architecture: ${arch}`);
  const abi = platform === "linux" && musl ? "-musl" : "";
  const exe = platform === "win32" ? ".exe" : "";
  if (tool === "bun") {
    const os = { win32: "windows", android: "linux" }[platform] || platform;
    const cpu = arch === "arm64" ? "aarch64" : arch;
    const suffix = platform === "android" ? "-android" : abi;
    const baseline = arch === "x64" && platform !== "android" && platform !== "freebsd" ? "-baseline" : "";
    return {
      wrapper: "bun",
      native: `@oven/bun-${os}-${cpu}${suffix}${baseline}`,
      file: `bin/bun${exe}`,
      targets: ["bin/bun.exe", "bin/bunx.exe"],
    };
  }
  if (tool === "pnpm") {
    return {
      wrapper: "pnpm",
      native: `@pnpm/exe.${platform}-${arch}${abi}`,
      file: `pnpm${exe}`,
      targets: ["pnpm", "pn", "pnpx", "pnx"].map((name) => name + exe),
    };
  }
  if (tool === "claude") {
    return {
      wrapper: "@anthropic-ai/claude-code",
      native: `@anthropic-ai/claude-code-${platform}-${arch}${abi}`,
      file: `claude${exe}`,
      targets: ["bin/claude.exe"],
    };
  }
  throw new Error(`Unsupported native tool: ${tool}`);
}

function json(file) {
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

function inside(root, file) {
  const relative = path.relative(root, file);
  if (!relative || relative === ".." || relative.startsWith(".." + path.sep) || path.isAbsolute(relative))
    throw new Error("Native package path leaves its locked project");
}

function bindingPlan(project, tool, host) {
  const info = nativeInfo(tool, host);
  const manifest = json(path.join(project, "package.json"));
  const lock = json(path.join(project, "package-lock.json"));
  const wrapperDir = fs.realpathSync(path.join(project, "node_modules", info.wrapper));
  inside(project, wrapperDir);
  const wrapper = json(path.join(wrapperDir, "package.json"));
  const expected = manifest.dependencies?.[info.wrapper];
  if (!expected || wrapper.name !== info.wrapper || wrapper.version !== expected)
    throw new Error(`Native wrapper version mismatch: ${info.wrapper}`);
  const declared = wrapper.optionalDependencies?.[info.native] || manifest.optionalDependencies?.[info.native];
  const entry = lock.packages?.[`node_modules/${info.native}`];
  if (
    declared !== expected ||
    entry?.version !== expected ||
    !/^sha512-[A-Za-z0-9+/]{86}==$/.test(entry.integrity || "") ||
    !entry.resolved?.startsWith("https://registry.npmjs.org/")
  )
    throw new Error(`Native package is absent from the locked graph: ${info.native}`);
  const nativeDir = fs.realpathSync(path.join(project, "node_modules", info.native));
  inside(project, nativeDir);
  const native = json(path.join(nativeDir, "package.json"));
  if (native.name !== info.native || native.version !== expected)
    throw new Error(`Native package version mismatch: ${info.native}`);
  const source = fs.realpathSync(path.join(nativeDir, info.file));
  inside(nativeDir, source);
  if (!fs.statSync(source).isFile() || fs.statSync(source).size === 0)
    throw new Error(`Native binary is missing: ${info.native}`);
  return { ...info, source, wrapperDir, host };
}

function placeBinary(source, destination) {
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  const temporary = destination + ".loom-native";
  fs.rmSync(temporary, { force: true });
  try {
    try {
      fs.linkSync(source, temporary);
    } catch (error) {
      if (!["EXDEV", "EPERM"].includes(error.code)) throw error;
      fs.copyFileSync(source, temporary, fs.constants.COPYFILE_FICLONE);
    }
    fs.chmodSync(temporary, 0o755);
    fs.renameSync(temporary, destination);
  } finally {
    fs.rmSync(temporary, { force: true });
  }
}

function bindNative(project, tools, host = hostPlatform()) {
  project = fs.realpathSync(project);
  const plans = tools.map((tool) => bindingPlan(project, tool, host));
  for (const plan of plans) {
    for (const target of plan.targets) {
      const destination = path.join(plan.wrapperDir, target);
      fs.mkdirSync(path.dirname(destination), { recursive: true });
      const parent = fs.realpathSync(path.dirname(destination));
      if (parent !== plan.wrapperDir) inside(plan.wrapperDir, parent);
      placeBinary(plan.source, destination);
    }
    if (plan.wrapper === "pnpm" && host.platform === "win32") {
      const file = path.join(plan.wrapperDir, "package.json");
      const manifest = json(file);
      manifest.bin = Object.fromEntries(["pnpm", "pn", "pnpx", "pnx"].map((name) => [name, name + ".exe"]));
      const temporary = file + ".loom-native";
      fs.writeFileSync(temporary, JSON.stringify(manifest, null, 2) + "\n");
      fs.renameSync(temporary, file);
    }
  }
}

if (require.main === module) {
  try {
    const [project, ...tools] = process.argv.slice(2);
    if (!project || tools.length === 0) throw new Error("Supply a locked project and native tools to bind");
    bindNative(project, tools);
  } catch (error) {
    console.error(`Native tool installation refused: ${error.message}`);
    process.exitCode = 1;
  }
}

module.exports = { bindNative, nativeInfo };
