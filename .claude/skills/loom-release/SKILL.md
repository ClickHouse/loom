---
name: loom-release
description: Cuts and checks a Loom release — the version bump and notes (prepare.py), the release PR, a release candidate and its soak, the final tag and the registry dispatch, then the release checked from outside (one-line installer on amd64 and arm64, SDK installs, anonymous pulls, signatures), rollback and patch releases. Use when asked to release, tag, publish or verify a release.
argument-hint: "[X.Y.Z]"
metadata:
  internal: true
---

# Releasing Loom

A release publishes what nobody can take back: images anyone pulls, a PyPI version that can never be reused, an npm version, an MCP Registry listing, and the GitHub Release the one-line installer reads. The procedure and the workflow are in [release process](../../../docs/development/release-process.md); this skill orders the steps and adds the checks that prove each one. Supported lines and deprecations: [supported versions](../../../docs/operations/supported-versions.md).

Every publishing step (pushing a tag, the registry dispatch, editing a GitHub Release, a yank, moving `:latest`) is the owner's to start: print the command, get the go-ahead, then run it. Everything else here you run yourself.

## 0. Is it time

- A minor release every two weeks from `main`; a patch when a fix must reach a supported line (N or N−1) before the next minor. Nothing is released from a private repository: the `preflight` job refuses it.
- Nothing open blocks it: `gh pr list -R ClickHouse/loom --state open --search "Release in:title"`, and `main` is green.
- The image scan is clean: a scheduled `docker-smoke` run on `main` from the past week passed (release process, "Cutting the release", step 1).
- A dry run of `main` is green; it publishes nothing:

  ```bash
  gh workflow run release.yml -R ClickHouse/loom --ref main -f dry_run=true
  gh run list -R ClickHouse/loom --workflow release.yml --branch main -L 1
  ```

  Its `preflight` warnings name every owner prerequisite still missing ("Before the first release"); a release with any of them fails in its first minute, before a build.

## 1. The version

- One version for the server, both images, the web app, both SDKs, the root skills and the chart's `appVersion`; `prepare.py` moves all of them and the chart's own `version`.
- The coding-agent plugins keep their own version, bumped in the pull request that changes them. When a hook merged since the previous release needs an op or field this release first serves, raise both manifests' `minServerVersion` to this version in the release pull request.
- A minor for features, a patch for fixes only (on a release branch, §7), a major for a break beyond the N−1 rule. A removed field or op is announced one minor ahead in the notes and served as `ignored_args` meanwhile ([unknown and removed fields](../../../docs/reference/http-api.md#unknown-and-removed-fields)).

## 2. The release pull request

In a worktree from `origin/main` (`loom-dev-setup` §1), with the tags fetched (`git fetch --tags`) and `gh` signed in:

```bash
.venv/bin/python scripts/release/prepare.py bump X.Y.Z
```

It refuses a prerelease, an older or an already tagged version before the notes, writes `release-notes.md`, then bumps every file. Read the notes: place every entry under "Unclassified", prune the operator-steps items that name no step and check the rest against their pull requests, and rewrite an entry a user could not act on. Open the pull request (`loom-ship-pr`) titled `Release X.Y.Z`, changelog category "Not for changelog", with the notes as its "Release notes" section. Just before it merges, rebase it and run `prepare.py notes X.Y.Z` again, so the notes name every pull request the tag ships.

## 3. Release candidate and soak

```bash
git fetch origin main
git tag vX.Y.Z-rc.1 <the release pull request's merge commit>
git push origin vX.Y.Z-rc.1          # the owner's go-ahead first
run=$(gh run list -R ClickHouse/loom --workflow release.yml --branch vX.Y.Z-rc.1 -L 1 --json databaseId --jq '.[0].databaseId')
gh run watch -R ClickHouse/loom "$run" --exit-status
```

A prerelease takes neither Latest nor `:latest` and is not listed in the MCP Registry. Deploy it to a deployment you run (`loom-deploy`; on Kubernetes set `image.tag` and `extractor.image.tag` to `X.Y.Z-rc.N`) and use it for at least two days. A regression is fixed on `main`, whose files already name `X.Y.Z`; tag `rc.2` on the fix's merge commit and soak again. Never move a tag: once its images are published, the workflow refuses a tag that points elsewhere. Once the final is tagged, a candidate's run is refused (`prepare.py check-tag`), and the message says whether to run the final, do nothing, or delete a stray tag.

## 4. The final release

On the same commit as the last release candidate, after `prepare.py notes X.Y.Z` there:

```bash
git tag vX.Y.Z <that commit>
git push origin vX.Y.Z                                        # the owner's go-ahead first
# watch it as in §3; then the registries (it builds no image, the tag run's stand):
gh workflow run release.yml -R ClickHouse/loom --ref vX.Y.Z   # the owner's go-ahead first
git push origin "vX.Y.Z^{commit}:refs/heads/release/X.Y"      # the line's patch branch
```

The final's bundle keeps the ClickHouse release its candidate soaked with. GitHub generates the Release's notes from the previous final (`prepare.py previous`); paste the release pull request's notes above them (release process, step 6). The tag run ends with `release-check` on the published installer. A failed job: `loom-investigate-ci`; the workflow's own recovery paths (a package first pushed private, a refused ClickHouse downgrade, and [a failed tag or signature](../../../docs/development/release-process.md#a-failed-tag-or-signature)) are in the release process.

## 5. Check it from outside

Signed in to nothing, as a user meets it, after the registry dispatch:

```bash
gh workflow run release-check.yml -R ClickHouse/loom -f version=X.Y.Z -f previous_version=<the release before>
```

It runs the one-line installer (`curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh | sh`, or the version's own URL when it is not Latest) in clean Debian, Ubuntu and Alpine containers on amd64 and arm64: install, a `memory.set`/`memory.search` round trip, a re-run, and an upgrade from the previous release. It pulls both images without signing in, and (unless `-f sdks=false`) installs `clickhouse-loom==X.Y.Z` from PyPI and `@clickhouse/loom@X.Y.Z` from npm, checking their provenance. Then by hand:

```bash
curl -fsSI https://github.com/ClickHouse/loom/releases/latest/download/install.sh | grep -i '^location:'   # names vX.Y.Z
bash scripts/release/verify_signatures.sh X.Y.Z ghcr.io/clickhouse/loom ghcr.io/clickhouse/loom-extractor   # cosign 3
curl -fsS "https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.ClickHouse/loom" | jq '.servers[].server.version'
```

The plugins, in a throwaway `HOME`: `claude plugin marketplace add ClickHouse/loom && claude plugin install loom@loom`, and `codex plugin marketplace add ClickHouse/loom`. Post what you checked, with the run links, on the release pull request.

## 6. Something is wrong

- **The installer or an image is bad**: point Latest back with `gh release edit v<previous> -R ClickHouse/loom --latest`, move both images' `:latest` (and, for a final, `:X.Y`) back by digest after checking the previous release's signature ([a failed tag or signature](../../../docs/development/release-process.md#a-failed-tag-or-signature) has the commands), then ship a patch.

  Never delete a release's assets or tag: an installed version reruns its installer from them. A host already on the bad release keeps it until the patch: the older installer refuses to run over a newer install unless `LOOM_VERSION=<previous>` names it.
- **An SDK is bad**: yank it on PyPI (the project's settings: the release, Yank, with the reason), which keeps pinned installs working; `npm deprecate @clickhouse/loom@X.Y.Z "<reason>; use <fixed version>"` and, if `latest` names it, `npm dist-tag add @clickhouse/loom@<previous> latest`. A version number is never reused: the fix is a new patch.
- **The MCP Registry listing**: publish the patch; the registry lists the newest version.

## 7. A patch release

For a supported line only (N or N−1). The fix merges to `main` first, then:

```bash
git switch -c fix/X.Y-<what> origin/release/X.Y
git cherry-pick -x <the merge commit on main>
.venv/bin/python scripts/release/prepare.py bump X.Y.<Z+1>
```

Open the pull request against `release/X.Y`; CI runs on it as on `main`. Tag its merge commit as in §3 and §4. Latest and `:latest` go only to the highest final version, so a patch to N−1 leaves them on N.
