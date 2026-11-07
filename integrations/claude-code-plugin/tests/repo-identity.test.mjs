// A repository is known by its remote, not its folder name.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { repoIdentity, resolveRelDir, resolveRemote, resolveScope } from "../lib/loom-client.mjs";
import { beginBody } from "../lib/run-hook.mjs";
import { buildObservation } from "../hooks/loom-capture.mjs";

test("repoIdentity folds every URL shape of one repository onto one id", () => {
  const shapes = [
    "git@github.com:Acme/App.git",
    "https://github.com/acme/app",
    "https://github.com/acme/app.git/",
    "ssh://git@github.com/acme/app.git",
    "ssh://git@github.com:22/acme/app",
    "git+ssh://git@GitHub.com/Acme/App",
    "https://token@github.com/acme/app",
  ];
  const ids = new Set(shapes.map((u) => repoIdentity(u).id));
  assert.equal(ids.size, 1, `one id for ${shapes.length} shapes: ${[...ids]}`);
  const r = repoIdentity(shapes[0]);
  assert.match(r.id, /^[0-9a-f]{10}$/);
  assert.equal(r.slug, "acme/app");
  assert.equal(r.canonical, "github.com/acme/app");
  // a different repository is a different id, even with the same name
  assert.notEqual(repoIdentity("git@github.com:other/app.git").id, r.id);
  assert.notEqual(repoIdentity("git@gitlab.com:acme/app.git").id, r.id);
});

test("repoIdentity: nothing usable → null", () => {
  assert.equal(repoIdentity(""), null);
  assert.equal(repoIdentity(undefined), null);
  assert.equal(repoIdentity("file:///srv/app.git"), null);
  assert.equal(repoIdentity("origin"), null);
});

function tmpRepo(remote) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-repo-"));
  execFileSync("git", ["init", "-q", dir]);
  if (remote) execFileSync("git", ["-C", dir, "remote", "add", "origin", remote]);
  fs.mkdirSync(path.join(dir, "src", "deep"), { recursive: true });
  return dir;
}

test("resolveRemote / resolveRelDir read the checkout; LOOM_REPO_REMOTE overrides", () => {
  const dir = tmpRepo("git@github.com:acme/app.git");
  assert.equal(resolveRemote(dir), "git@github.com:acme/app.git");
  assert.equal(resolveRelDir(dir), "");
  assert.equal(resolveRelDir(path.join(dir, "src", "deep")), "src/deep");
  const bare = tmpRepo(null);
  assert.equal(resolveRemote(bare), "");
  const saved = process.env.LOOM_REPO_REMOTE;
  process.env.LOOM_REPO_REMOTE = "https://example.com/x/y";
  try {
    assert.equal(resolveRemote(bare), "https://example.com/x/y");
  } finally {
    if (saved === undefined) delete process.env.LOOM_REPO_REMOTE;
    else process.env.LOOM_REPO_REMOTE = saved;
  }
  assert.equal(resolveRelDir(os.tmpdir()), "");
});

test("resolveScope carries repo + relDir and the repo tag beside the project tag", () => {
  const dir = tmpRepo("https://github.com/acme/app.git");
  const saved = { ...process.env };
  process.env.LOOM_API_KEY = "";
  delete process.env.LOOM_PROJECT_SCOPE;
  delete process.env.LOOM_REPO_REMOTE;
  try {
    const s = resolveScope(path.join(dir, "src"));
    assert.equal(s.project, path.basename(dir));
    assert.equal(s.repo.slug, "acme/app");
    assert.equal(s.relDir, "src");
    assert.deepEqual(s.tags, [`project:${s.project}`, `repo:${s.repo.id}`]);
    process.env.LOOM_PROJECT_SCOPE = "namespace";
    const n = resolveScope(dir);
    assert.equal(n.namespace, s.project);
    assert.deepEqual(n.tags, [`repo:${s.repo.id}`]);
    const bare = tmpRepo(null);
    delete process.env.LOOM_PROJECT_SCOPE;
    const b = resolveScope(bare);
    assert.equal(b.repo, null);
    assert.deepEqual(b.tags, [`project:${b.project}`]);
  } finally {
    for (const k of Object.keys(process.env)) if (!(k in saved)) delete process.env[k];
    Object.assign(process.env, saved);
  }
});

test("observations and runs record the repository by its remote", () => {
  const scope = {
    org: "o",
    namespace: "coding",
    agent: "claude-code",
    project: "app",
    repo: { id: "abcdef0123", slug: "acme/app" },
    relDir: "src/deep",
    tags: ["project:app", "repo:abcdef0123"],
  };
  const obs = buildObservation(
    "PostToolUse",
    { tool_name: "Bash", tool_input: { command: "ls" }, session_id: "s" },
    scope,
  );
  assert.equal(obs.metadata.repo_id, "abcdef0123");
  assert.equal(obs.metadata.repo_slug, "acme/app");
  assert.equal(obs.metadata.rel_dir, "src/deep");
  assert.ok(obs.tags.includes("repo:abcdef0123") && obs.tags.includes("project:app"));
  const run = beginBody(scope, { session_id: "s", cwd: "/x" }, "do the thing");
  assert.equal(run.metadata.repo_id, "abcdef0123");
  // a checkout without a remote adds nothing
  const plain = buildObservation(
    "PostToolUse",
    { tool_name: "Bash", tool_input: {} },
    { ...scope, repo: null, relDir: "" },
  );
  assert.equal("repo_id" in plain.metadata, false);
  assert.equal("rel_dir" in plain.metadata, false);
});
