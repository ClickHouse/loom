const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { CompilerState, Extractor, ExtractorConfig } = require("@microsoft/api-extractor");

function entries(dir) {
  const packageJsonFullPath = path.join(dir, "package.json");
  const { exports } = JSON.parse(fs.readFileSync(packageJsonFullPath, "utf8"));
  const inputs = new Map();
  for (const [entry, targets] of Object.entries(exports)) {
    if (entry === "./package.json") continue;
    const modes = ["import", "require"].filter((mode) => targets[mode]);
    if (!modes.length) throw new Error(`No typed import or require entry for ${entry}`);
    for (const mode of modes) {
      const declaration = targets[mode].types;
      if (typeof declaration !== "string" || !/\.d\.[cm]?ts$/.test(declaration)) {
        throw new Error(`Missing type declaration for ${entry} (${mode})`);
      }
      const input = path.resolve(dir, declaration);
      if (!input.startsWith(dir + path.sep)) throw new Error(`Declaration outside package: ${entry}`);
      inputs.set(`${entry} (${mode})`, input);
    }
  }
  if (!inputs.size) throw new Error("No package declarations found");
  return { packageJsonFullPath, inputs };
}

function reports(dir, output, labels) {
  const { packageJsonFullPath, inputs } = entries(dir);
  fs.mkdirSync(output, { recursive: true });
  const compiler = {
    overrideTsconfig: {
      compilerOptions: {
        target: "ES2022",
        module: "NodeNext",
        moduleResolution: "NodeNext",
        strict: true,
        skipLibCheck: true,
      },
      files: [...inputs.values()],
    },
  };
  let compilerState;
  const results = new Map();
  for (const [label, mainEntryPointFilePath] of inputs) {
    const reportFileName = String(labels.indexOf(label));
    const config = ExtractorConfig.prepare({
      configObject: {
        projectFolder: dir,
        mainEntryPointFilePath,
        compiler,
        apiReport: { enabled: true, reportFileName, reportFolder: output, reportTempFolder: output },
        docModel: { enabled: false },
        dtsRollup: { enabled: false },
        tsdocMetadata: { enabled: false },
      },
      configObjectFullPath: path.join(dir, "api-extractor.json"),
      packageJsonFullPath,
    });
    compilerState ??= CompilerState.create(config, { additionalEntryPoints: [...inputs.values()] });
    const result = Extractor.invoke(config, { localBuild: true, compilerState });
    if (!result.succeeded) throw new Error(`Could not generate a report for ${label}`);
    results.set(label, path.join(output, `${reportFileName}.api.md`));
  }
  return results;
}

const [baseline, candidate, output] = process.argv.slice(2).map((name) => path.resolve(name));
const labels = [...new Set([...entries(baseline).inputs.keys(), ...entries(candidate).inputs.keys()])].sort();
const before = reports(baseline, path.join(output, "before"), labels);
const after = reports(candidate, path.join(output, "after"), labels);
let changed = 0;
const sections = [];
for (const label of labels) {
  const oldFile = before.get(label) ?? "/dev/null";
  const newFile = after.get(label) ?? "/dev/null";
  const relative = (file) => (file === "/dev/null" ? file : path.relative(output, file));
  const diff = spawnSync("git", ["diff", "--no-index", "--no-color", "--", relative(oldFile), relative(newFile)], {
    cwd: output,
    encoding: "utf8",
    maxBuffer: 16 * 1024 * 1024,
  });
  if (diff.status !== 0 && diff.status !== 1) {
    throw new Error(diff.stderr || `git diff failed for ${label}`, { cause: diff.error });
  }
  if (diff.status === 1) {
    changed += 1;
    sections.push(`# ${label}\n${diff.stdout}`);
  }
}
const summary = `TypeScript declaration changes in ${changed} of ${labels.length} package entry points.\n`;
fs.writeFileSync(path.join(output, "typescript-api.diff"), summary + sections.join("\n"));
console.log(summary.trim());
