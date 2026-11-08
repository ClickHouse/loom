---
name: loom-release
description: Cuts and checks a Loom release — the version bump and notes (prepare.py), the release PR, a release candidate and its soak, the final tag and the registry dispatch, then the release checked from outside (one-line installer on amd64 and arm64, SDK installs, anonymous pulls, signatures), rollback and patch releases. Use when asked to release, tag, publish or verify a release.
argument-hint: "[X.Y.Z]"
metadata:
  internal: true
---

# Releasing Loom

A release publishes what nobody can take back: images anyone pulls, a PyPI version that can never be reused, an npm version, an MCP Registry listing, and the GitHub Release the one-line installer reads. The procedure and the workflow are in [release process](../../../docs/development/release-process.md); this skill orders the steps and adds the checks that prove each one. Supported lines and deprecations: [supported versions](../../../docs/operations/supported-versions.md).

Every step others see is the owner's to start, as AGENTS.md has it: opening or merging a pull request (the release pull request, and a fix's against `release/X.Y`) and posting on it, pushing a tag or `release/X.Y`, the registry dispatch, editing a GitHub Release, a PyPI yank, `npm deprecate` and `npm dist-tag`, moving `:latest`. Print the command, get the go-ahead, then run it. Everything else here (the dry run, `prepare.py`, the checks) you run yourself.

## 0. Is it time

- A minor release every two weeks from `main`; a patch when a fix must reach a supported line before the next minor (which fixes: §7). Nothing is released from a private repository: the `preflight` job refuses it.
- Nothing open blocks it: no open `Release X.Y.Z` pull request (`gh pr list -R ClickHouse/loom --state open --search '"Release X.Y.Z" in:title'`), and `main` is green.
- The image scan is clean: a scheduled `docker-smoke` run on `main` from the past week passed (release process, "Cutting the release", step 1).
- A dry run of `main` is green; it publishes nothing:

  ```bash
  gh workflow run release.yml -R ClickHouse/loom --ref main -f dry_run=true
  gh run list -R ClickHouse/loom --workflow release.yml --branch main -L 1
  ```

  Its `preflight` warnings name every owner prerequisite still missing ("Before the first release"); a release with any of them fails in its first minute, before a build.
- For a patch, run the scan and the dry run on the line instead: `gh workflow run docker-smoke.yml -R ClickHouse/loom --ref release/X.Y` and the dry run with `--ref release/X.Y`, and read both with `--branch release/X.Y` (the scan with `--event workflow_dispatch`).

## 1. The version

- One version for the server, both images, the web app, both SDKs, the root skills and the chart's `appVersion`; `prepare.py` moves all of them and the chart's own `version`.
- The coding-agent plugins keep their own version, bumped in the pull request that changes them. Raise their `minServerVersion` as the [release process](../../../docs/development/release-process.md#cutting-the-release) step 2 says.
- A minor for features, a patch for fixes only (on a release branch, §7), a major for a break beyond the N−1 rule. A field or op to be removed is announced one minor ahead, by hand under the notes' "What changed for clients" (the generated wire diff lists only what has changed), and served until the removal; after it, a removed field is named in `ignored_args` and a removed op answers `NotFound` ([unknown and removed fields](../../../docs/reference/http-api.md#unknown-and-removed-fields)).

## 2. The release pull request

In a worktree from `origin/main` (`loom-dev-setup` §1), with the tags fetched (`git fetch --tags`) and `gh` signed in:

```bash
.venv/bin/python scripts/release/prepare.py bump X.Y.Z
```

It refuses a prerelease, an older or an already tagged version before the notes, writes `release-notes.md`, then bumps every file. Read the notes: place every entry under "Unclassified", prune the operator-steps items that name no step and check the rest against their pull requests, and rewrite an entry a user could not act on. Open the pull request (`loom-ship-pr`) titled `Release X.Y.Z`, changelog category "Not for changelog", with the notes as its "Release notes" section. Just before it merges, rebase it and run `prepare.py notes X.Y.Z` again, so the notes name every pull request the tag ships.

## 3. Release candidate and soak

```bash
git fetch origin main release/X.Y   # release/X.Y for a patch
git tag vX.Y.Z-rc.1 <the release pull request's merge commit>
git push origin vX.Y.Z-rc.1          # the owner's go-ahead first
# The tag's run may take a few seconds to appear: re-run this line until it prints an id.
run=$(gh run list -R ClickHouse/loom --workflow release.yml --branch vX.Y.Z-rc.1 -L 1 --json databaseId --jq '.[0].databaseId // empty')
gh run watch -R ClickHouse/loom "$run" --exit-status
```

A prerelease takes neither Latest nor `:latest` and is not listed in the MCP Registry. Deploy it to a deployment you run (`loom-deploy`; on Kubernetes set `image.tag` and `extractor.image.tag` to `X.Y.Z-rc.N`) and use it for at least two days. A regression is fixed on `main`, whose files already name `X.Y.Z`; tag `rc.2` on the fix's merge commit (for a patch, on its merge commit on `release/X.Y`, as in §7) and soak again. Never move a tag: once its images are published, the workflow refuses a tag that points elsewhere. Once the final is tagged, a candidate's run is refused (`prepare.py check-tag`), and the message says whether to run the final, do nothing, or delete a stray tag.

## 4. The final release

On the same commit as the last release candidate, after `prepare.py notes X.Y.Z` there:

```bash
git tag vX.Y.Z <that commit>
git push origin vX.Y.Z                                        # the owner's go-ahead first
# watch it as in §3; after it passes, the registry dispatch (it reuses the tag run's images):
gh workflow run release.yml -R ClickHouse/loom --ref vX.Y.Z   # the owner's go-ahead first
git push origin "vX.Y.Z^{commit}:refs/heads/release/X.Y"      # X.Y.0 only: the line's patch branch; the owner's go-ahead first
```

The final's bundle keeps the ClickHouse release its candidate soaked with. GitHub generates the Release's notes from the previous final (`prepare.py previous`); paste the release pull request's notes above them (release process, step 6). The tag run ends with `release-check` on the published installer. A failed job: `loom-investigate-ci`; the workflow's own recovery paths (a package first pushed private, a refused ClickHouse downgrade, and [a failed tag or signature](../../../docs/development/release-process.md#a-failed-tag-or-signature)) are in the release process.

## 5. Check it from outside

After the registry dispatch, check the release as a user meets it:

```bash
gh workflow run release-check.yml -R ClickHouse/loom -f version=X.Y.Z -f previous_version=<the release before>
```

Signed in to nothing, it runs the one-line installer (`curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh | sh`, checking that `latest` serves this version, or the version's own URL when it is not Latest) in clean Debian, Ubuntu and Alpine containers on amd64 and arm64: install, a `memory.set`/`memory.search` round trip, a re-run, and an upgrade from the previous release. It verifies both images' signatures (`scripts/release/verify_signatures.sh`) and, unless `-f sdks=false`, installs `clickhouse-loom==X.Y.Z` from PyPI and `@clickhouse/loom@X.Y.Z` from npm, verifying the npm package's registry signatures and attestations (`npm audit signatures`). Then by hand, the MCP Registry listing:

```bash
curl -fsS "https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.ClickHouse/loom" | jq '.servers[].server.version'   # lists X.Y.Z
npm view @clickhouse/loom dist-tags   # latest names the highest final
```

The plugins, in a throwaway `HOME`: `claude plugin marketplace add ClickHouse/loom && claude plugin install loom@loom`, and `codex plugin marketplace add ClickHouse/loom` then install `loom` from it; each lists the plugin version on `main`, and its SessionStart against this server prints no `loom:` line about the server's version. Post what you checked, with the run links, on the release pull request (the owner's go-ahead first).

## 6. Something is wrong

- **The installer or an image is bad**: move back only the tags the bad release took, checking the previous release's signature before moving each ([a failed tag or signature](../../../docs/development/release-process.md#a-failed-tag-or-signature) has the commands), then ship a patch.
  - A final took `:X.Y`: point it at the previous patch of that line. An `X.Y.0` has none, so `:X.Y` stays until `X.Y.1`.
  - When the bad release is also Latest (`GITHUB_REPOSITORY=ClickHouse/loom python3 scripts/release/make_latest.py X.Y.Z` prints `true`, with `gh` signed in), point Latest at the release before it (`gh release edit v<previous> -R ClickHouse/loom --latest`) and both images' `:latest` there. After that, never dispatch or re-run the bad tag: its `github-release` job makes it Latest again until a higher final is tagged.
  - A candidate took neither.

  Never delete a release's assets or tag: an installed version reruns its installer from them. A host already on the bad release keeps it until the patch: the older installer refuses to run over a newer install; going back is [downgrading an installer install](../../../docs/operations/upgrades.md#downgrading-an-installer-install), ClickHouse pinned first.
- **An SDK is bad**: yank it on PyPI (the project's settings: the release, Yank, with the reason), which keeps pinned installs working; `npm deprecate @clickhouse/loom@X.Y.Z "<reason>; use <fixed version>"` and move the dist-tag it took (`latest`, or `release-X.Y` for a patch to an older line) back to the previous version that held it (`npm dist-tag add @clickhouse/loom@<previous> <tag>`), or remove it when none did (`npm dist-tag rm @clickhouse/loom release-X.Y`). A version number is never reused: the fix is a new patch.
- **The MCP Registry listing**: publish the patch; the registry lists the newest version.

## 7. A patch release

For a supported line only: any fix for N, and for N−1 only what [supported versions](../../../docs/operations/supported-versions.md#release-lines) lists. The fix merges to `main` first, then reaches the line in its own pull request against `release/X.Y`, where CI runs as on `main`:

```bash
git switch -c fix/X.Y-<what> origin/release/X.Y
git cherry-pick -x -s <the merge commit on main>
```

Once it merges, the release pull request follows as in §2, from `origin/release/X.Y` and against it, with `prepare.py bump X.Y.<Z+1>`: in one pull request with the fix, the notes would leave the fix out, as they skip the commit that bumps the version. Tag the release pull request's merge commit as in §3 and §4. Latest, `:latest` and npm's `latest` go only to the highest final version, so a patch to N−1 leaves them on N; its npm package goes to the `release-X.Y` dist-tag.
