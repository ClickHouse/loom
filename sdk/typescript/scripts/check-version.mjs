// The version is single-sourced: package.json, package-lock.json
// and src/version.ts must agree. Exits 1 with the three values otherwise.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
const lock = JSON.parse(readFileSync(join(root, "package-lock.json"), "utf8"));
const src = readFileSync(join(root, "src/version.ts"), "utf8");
const m = src.match(/export const SDK_VERSION = "([^"]+)";/);
const changelog = readFileSync(join(root, "CHANGELOG.md"), "utf8");
const top = changelog.match(/^## \[(\d+\.\d+\.\d+)\] - (?:Unreleased|\d{4}-\d{2}-\d{2})$/m);
const versions = {
  "package.json": pkg.version,
  "package-lock.json": lock.version,
  "src/version.ts": m?.[1],
  "CHANGELOG.md top entry": top?.[1],
};
const lockRoot = lock.packages?.[""]?.version;
if (lockRoot !== undefined) versions['package-lock.json packages[""]'] = lockRoot;
const distinct = new Set(Object.values(versions));
if (distinct.size !== 1) {
  console.error("version mismatch:", versions);
  process.exit(1);
}
console.log(`version ${pkg.version} consistent across ${Object.keys(versions).length} sources`);
