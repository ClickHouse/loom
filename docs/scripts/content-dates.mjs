import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

function git(cwd, args) {
  return execFileSync("git", args, {
    cwd,
    encoding: "utf8",
    env: { ...process.env, GIT_TERMINAL_PROMPT: "0" },
    maxBuffer: 4 * 1024 * 1024,
    stdio: ["ignore", "pipe", "pipe"],
    timeout: 20_000,
  }).trimEnd();
}

function datesAt(repository, revision) {
  const log = git(repository, [
    "log",
    "--first-parent",
    "--diff-merges=first-parent",
    "--no-renames",
    "--format=%x00%ct",
    "--name-only",
    "-z",
    revision,
    "--",
    "docs",
    "i18n",
  ]);
  const dates = new Map();
  let date;
  let nextIsCommit = false;
  for (const field of log.split("\0")) {
    if (field === "") {
      nextIsCommit = true;
    } else if (nextIsCommit) {
      date = new Date(Number(field) * 1_000).toISOString();
      nextIsCommit = false;
    } else {
      const path = field.replace(/^\n/, "");
      if (!dates.has(path)) dates.set(path, date);
    }
  }
  return dates;
}

export function readContentDates(repository) {
  try {
    if (git(repository, ["rev-parse", "--is-shallow-repository"]) === "false") {
      return datesAt(repository, "HEAD");
    }

    const revision = git(repository, ["rev-parse", "HEAD"]);
    const scratch = mkdtempSync(join(tmpdir(), "loom-docs-history-"));
    try {
      // A shallow build needs the trees at its exact revision, with no file blobs.
      git(scratch, ["init", "--bare", "--quiet"]);
      git(scratch, [
        "fetch",
        "--filter=blob:none",
        "--no-tags",
        "--quiet",
        "https://github.com/ClickHouse/loom.git",
        revision,
      ]);
      return datesAt(scratch, "FETCH_HEAD");
    } finally {
      rmSync(scratch, { recursive: true, force: true });
    }
  } catch {
    console.warn("Git history is unavailable. Omitting source modification dates.");
    return new Map();
  }
}
