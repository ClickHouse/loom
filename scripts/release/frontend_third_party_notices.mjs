#!/usr/bin/env node

/** Build the license notice that travels with the compiled dashboard. */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));

function argument(name, fallback = "") {
  const index = process.argv.indexOf(name);
  return index === -1 ? fallback : process.argv[index + 1] || fallback;
}

function repositoryUrl(value) {
  if (typeof value === "string") return value;
  if (value && typeof value.url === "string") return value.url;
  return "";
}

function licenseText(packageDir) {
  const names = fs.readdirSync(packageDir).sort();
  const license = names.find((name) => /^(licen[cs]e|copying|notice)(\..*)?$/i.test(name));
  if (license) return fs.readFileSync(path.join(packageDir, license), "utf8").trim();

  const readme = names.find((name) => /^readme(\.md)?$/i.test(name));
  if (!readme) return "";
  const text = fs.readFileSync(path.join(packageDir, readme), "utf8");
  const heading = /^(#{1,6})\s+licen[cs]e\s*$/im.exec(text);
  if (!heading) return "";
  const rest = text.slice(heading.index + heading[0].length).replace(/^\s+/, "");
  const next = new RegExp(`^#{1,${heading[1].length}}\\s+`, "m").exec(rest);
  return (next ? rest.slice(0, next.index) : rest).trim();
}

export function buildNotices(root) {
  const lock = JSON.parse(fs.readFileSync(path.join(root, "package-lock.json"), "utf8"));
  const entries = [];
  for (const [relative, locked] of Object.entries(lock.packages || {})) {
    if (!relative.startsWith("node_modules/") || locked.dev || locked.optional) continue;
    const packageDir = path.join(root, relative);
    if (!fs.existsSync(packageDir)) continue;
    const metadata = JSON.parse(fs.readFileSync(path.join(packageDir, "package.json"), "utf8"));
    const declared = metadata.license || locked.license || "UNKNOWN";
    const notice = licenseText(packageDir);
    entries.push({
      name: metadata.name || relative.replace(/^node_modules\//, ""),
      version: metadata.version || locked.version || "unknown",
      declared,
      repository: repositoryUrl(metadata.repository),
      notice,
    });
  }
  for (const entry of entries) {
    if (entry.notice) continue;
    const donor = entries.find(
      (candidate) =>
        candidate.notice &&
        candidate.repository &&
        candidate.repository === entry.repository &&
        candidate.declared === entry.declared,
    );
    if (donor) {
      entry.notice = donor.notice;
      continue;
    }
    const fallback = path.join(SCRIPT_DIR, "license_fallbacks", `${entry.name.replaceAll("/", "__")}.txt`);
    if (!fs.existsSync(fallback)) {
      throw new Error(`${entry.name} has no distributable license text`);
    }
    entry.notice = fs.readFileSync(fallback, "utf8").trim();
  }
  entries.sort((a, b) => `${a.name}@${a.version}`.localeCompare(`${b.name}@${b.version}`));

  const lines = [
    "Loom dashboard third-party notices",
    "==================================",
    "",
    "This file is generated from the production npm dependency tree used to build",
    "the dashboard. Each package remains subject to the license stated below.",
    "",
  ];
  for (const entry of entries) {
    lines.push(`${entry.name}@${entry.version}`, "-".repeat(`${entry.name}@${entry.version}`.length));
    lines.push(`Declared license: ${entry.declared}`);
    if (entry.repository) lines.push(`Source: ${entry.repository}`);
    lines.push("", entry.notice, "");
  }
  return `${lines.join("\n").trim()}\n`;
}

const root = path.resolve(argument("--root", "frontend"));
const output = path.resolve(argument("--output", path.join(root, "THIRD_PARTY_NOTICES.txt")));
const rendered = buildNotices(root);
if (process.argv.includes("--check")) {
  const current = fs.existsSync(output) ? fs.readFileSync(output, "utf8") : "";
  if (current !== rendered) {
    process.stderr.write(`${output} is missing or stale\n`);
    process.exitCode = 1;
  }
} else {
  fs.writeFileSync(output, rendered, "utf8");
}
