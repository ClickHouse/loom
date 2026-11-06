# Release process

Loom follows SemVer. The server, the Python SDK, the TypeScript SDK, the web app and the root skills (`loom-code`, `loom`) share one version `X.Y.Z`. The two coding-agent plugins share their own version, bumped on a behaviour change to any file the plugin ships (`hooks/`, `lib/`, `bin/`, `skills/`, `.mcp.json`, the Codex `plugin-defaults.json`): installed clients pick up a hook change only through a new plugin version.

## Where the version lives

Bump each of these in the release commit:

| Component            | Files                                                                                                                                                                                                                                                                                |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Server               | `server/__init__.py` (`__version__`), `pyproject.toml` and `server.json`                                                                                                                                                                                                             |
| Python SDK           | `sdk/python/pyproject.toml` and `sdk/python/loom/_version.py`                                                                                                                                                                                                                        |
| TypeScript SDK       | `sdk/typescript/package.json`, `sdk/typescript/package-lock.json`, `sdk/typescript/src/version.ts`                                                                                                                                                                                   |
| Web app              | `frontend/package.json` and `frontend/package-lock.json`                                                                                                                                                                                                                             |
| Root skills          | `.claude-plugin/plugin.json` and the `metadata.version` of `skills/loom-code/SKILL.md` and `skills/loom/SKILL.md`                                                                                                                                                                    |
| Helm chart           | `deploy/helm/loom/Chart.yaml` (`appVersion`): the default tag of the server image and of the extractor image, and the chart's `app.kubernetes.io/version` label                                                                                                                      |
| Coding-agent plugins | `integrations/claude-code-plugin/.claude-plugin/plugin.json`, `integrations/codex-plugin/.codex-plugin/plugin.json` (their own version; bump on a behaviour change to any file the plugin ships: `hooks/`, `lib/`, `bin/`, `skills/`, `.mcp.json`, the Codex `plugin-defaults.json`) |

The server reads its version from `server/__init__.py` only; the OpenAPI document, MCP `serverInfo` and `loom report` report it. Four checks keep the numbers consistent:

| Check                                    | Asserts                                                                                      |
| ---------------------------------------- | -------------------------------------------------------------------------------------------- |
| `tests/unit/test_release_version.py`     | `server/__init__.py` equals `pyproject.toml` and `server.json`, and every surface reports it |
| `npm run check:version` (TypeScript SDK) | its four version sources agree                                                               |
| `tests/unit/test_codex_plugin.py`        | both plugin manifests carry one version                                                      |
| `tests/unit/test_loom_code_skill.py`     | both root skills and `.claude-plugin/plugin.json` carry the server's version                 |

Two plugin pull requests that bump to the same number merge without a conflict, and the second ships nothing to clients the first already updated. Check the plugin version on `main` before bumping.

## Release notes

The run that creates the GitHub Release generates its notes from the commits since the previous tag; a
re-run or the later registry dispatch keeps them as they are. What commits cannot say goes in a "Release
notes" section of the release pull request's description, so it is reviewed before the tag: the
changelog entries of the pull requests merged since the previous tag, grouped by their changelog
category (pull requests in a category that needs no entry are left out), then operator steps (new
settings, backfills, table rebuilds) and client-visible wire changes, including every removed request
field and retired flag. No registry of removed fields exists, so the release notes are where a
client learns of one. Generate the wire diff instead of writing it:

```bash
python scripts/codegen/wire_changelog.py v<previous> HEAD
```

([code generation](./code-generation.md#wire-changelog)).

## Cutting the release

1. Check that the latest scheduled run of `.github/workflows/docker-smoke.yml` on `main` passed
   (`gh run list -R ClickHouse/loom --workflow docker-smoke.yml --event schedule --branch main -L 1`,
   or `gh workflow run docker-smoke.yml -R ClickHouse/loom --ref main` for a fresh one). It scans both
   images as the release does, and a scheduled run's failure notifies only the last editor of its
   schedule. A red scan names the package: move the pin that brings it in (a `Dockerfile` `ARG` or
   base-image digest, or a lock file) on `main` first.
2. On a branch from `main`, open a pull request that sets the version everywhere above, with the
   [release notes](#release-notes) section in its description. CI must pass, including
   `make schema-check` and the strict migration dry run.
3. After the release pull request merges, tag its merge commit and push the tag:

   ```bash
   git tag vX.Y.Z <merge commit>
   git push origin vX.Y.Z
   ```

4. The tag push runs `.github/workflows/release.yml` (below), publishes the server and
   extractor images, and only then creates the GitHub Release. Check the images and release files.
5. To publish the SDKs and the MCP Registry listing, run the same workflow manually on the tag:

   ```bash
   gh workflow run release.yml --ref vX.Y.Z
   ```

6. Paste the pull request's release notes section into the GitHub Release, above the generated notes.

A tag containing `-` (`vX.Y.Z-rc.1`) makes a GitHub pre-release. Only the highest final version
becomes the repository's Latest release (`releases/latest` serves the installer) and moves
`:latest`: a prerelease or a patch to an older line leaves both where they are. The versions are
read from the repository's tags, so delete a final tag that will not ship, or Latest stays below it.

## Rehearsing a release

A dry run builds, verifies and smoke-tests everything a release would, on any branch, and publishes
nothing: no image push, GitHub Release or registry upload.

```bash
gh workflow run release.yml --ref <branch> -f dry_run=true
```

It runs at the version `server/__init__.py` names. Its images go to run artifacts instead of ghcr.io,
without the provenance and SBOM attestations that only a registry keeps, and `docker-smoke` loads them
from there. The `preflight` job reports each missing [prerequisite](#before-the-first-release) as a
warning instead of failing. What only a published release exercises, a dry run does not: `docker-tag`
(joining each image's two digests, the Latest decision, `:latest`), the GitHub Release, the
registry publishes, and the published-images check (`scripts/release/published_images.sh`) with the
skipped-build path it selects.

## Before the first release

A release publishes public packages, and a registry version can never be reused, so the workflow
refuses a release from a private repository. The `preflight` job checks what only an owner can grant,
in seconds, before any build, and names the fix for each one it finds missing:

| Prerequisite                                                                     | Checked by                                           |
| -------------------------------------------------------------------------------- | ---------------------------------------------------- |
| The repository is public                                                         | `preflight`                                          |
| `ghcr.io/clickhouse/loom` and `loom-extractor` give this repository Write access | `preflight` (an upload session it opens and cancels) |
| Both packages are public, since an install pulls without signing in              | `preflight` (an anonymous pull token)                |
| A `pypi` environment and a PyPI trusted publisher for `release.yml`              | the `pypi` job                                       |
| An `npm` environment, and npm trusted publishing or an `NPM_TOKEN` secret        | the `npm` job                                        |
| An `mcp-registry` environment with the same protection rules as `pypi` and `npm` | the `mcp-registry` job                               |

Only a push creates a ghcr.io package, and it creates it private. For an image no release has pushed
yet, `preflight` warns instead of failing, and that run's `docker-smoke` jobs fail to pull it: set
the package's visibility to public in its settings, then re-run the failed jobs.

## What the release workflow does

`.github/workflows/release.yml` builds once, verifies the exact files, and publishes those files.

**On a tag push (`v*`)**:

1. `version` takes the version from the tag (`vX.Y.Z` → `X.Y.Z`) and refuses a ref that
   is not a `v*` tag (unless the run is a [dry run](#rehearsing-a-release)) or not SemVer; it
   fails unless `server/__init__.py` reads `__version__ = "X.Y.Z"` and `server.json` carries
   the same version;
2. `preflight` fails the run on a missing [prerequisite](#before-the-first-release);
3. `clickhouse` names the ClickHouse release the Compose bundle runs: the release
   `clickhouse/clickhouse-server:latest` names now (`dev/clickhouse_release.sh`), or, on a
   re-run, the one this version's published bundle already names, so rerunning the
   installer for a version never moves ClickHouse (`scripts/release/published_bundle.sh`;
   a release without a bundle resolves anew, as `github-release` never replaces one, so
   nobody can have installed it). It migrates a fresh server of that
   release and runs `make schema-check` before `build` may pin it. Each tag resolves
   its own, so a final release can name a newer ClickHouse than its release candidates.
   When `latest` names a bad release, dispatch the tag with an exact one instead
   (`gh workflow run release.yml --ref vX.Y.Z -f clickhouse_image=clickhouse/clickhouse-server:A.B.C.D`);
   a bundle already published keeps its own, and the run warns that it ignored the input.
   The dispatch passes only if that release dumps the tag's `schemas/schema.sql`;
   otherwise regenerate it on that release (the
   [schema dump](./migrations.md#schema-dump-and-check) commands with
   `LOOM_CLICKHOUSE_IMAGE=clickhouse/clickhouse-server:A.B.C.D`), commit it, and re-tag.
   A ClickHouse downgrade (see step 8) is refused here first, so such a run stops before
   it builds and pushes images;
4. `build` writes the version into the Python and TypeScript SDK sources and builds the
   Python SDK wheel and sdist, the npm tarball (after the TypeScript SDK's `check:dist`),
   and zips of the two plugins (`loom-claude-code-plugin-vX.Y.Z.zip`,
   `loom-codex-plugin-vX.Y.Z.zip`); it also builds the source-free Compose bundle and its
   SHA-256 file, naming that ClickHouse release;
5. `verify` checks those exact files: their names and embedded versions, the plugin
   manifests, no tests in the Claude Code zip, the executable bits git records, and
   that the bundle names the ClickHouse release the `clickhouse` job checked, and
   smoke-tests the wheel in a clean venv with `scripts/gates/sdk_wheel_smoke.py`;
6. `docker` builds `ghcr.io/clickhouse/loom` and `ghcr.io/clickhouse/loom-extractor`
   (the document extractor service), each for linux/amd64 and linux/arm64 on that
   architecture's own runner and without the build cache (so from the package archives as
   they are that day), with provenance and an SBOM, pushes each by digest and scans it;
   `docker-tag` joins each image's two digests under its tags, and tags neither image until
   all four have passed (a failure there can leave one tagged, and re-running it tags both);
7. `docker-smoke` installs the bundle with those images, on the ClickHouse release it
   names, on amd64 and arm64;
8. `github-release` creates the GitHub Release only after both images exist, with notes
   generated from the commits since the previous tag, or updates it on a re-run, keeping
   its notes as they are, and attaches the verified files. A bundle the release already
   carries is kept, and so is its checksum (one the release lost is written again from
   it): replacing an asset deletes it first, and a failed upload would leave an installed
   version without its bundle. A new bundle never names an older ClickHouse release than
   the bundles of the 30 newest releases (prereleases included) do, since an install
   upgrading to it would start an older server on data a newer one wrote; the job fails
   instead, unless the dispatch adds `-f allow_clickhouse_downgrade=true`. This job runs
   for one tag at a time, so two tags released together cannot both pass the check against
   the same older bundle. Only one more such job waits: a third tag's publish cancels the
   waiting one, which must then be re-run by hand (Re-run failed jobs). A refusal here
   comes after `docker-tag` pushed the tag's image tags (and `:latest` when it is the highest
   final version). A re-run keeps the run's inputs and fails again, so dispatch the tag instead,
   which also publishes the SDKs and the MCP Registry listing:
   `gh workflow run release.yml --ref vX.Y.Z -f clickhouse_image=<the newer release the error names>`
   (if that release dumps the tag's `schemas/schema.sql`),
   or `gh workflow run release.yml --ref vX.Y.Z -f allow_clickhouse_downgrade=true`.
   Otherwise move `:latest` back to the previous release by hand, on both
   `ghcr.io/clickhouse/loom` and `ghcr.io/clickhouse/loom-extractor`.

The server and extractor images are published before the GitHub Release, so an installer
shown on the release page never points at a missing image.

**On manual dispatch** (`workflow_dispatch` on a tag ref), the same jobs run (`build`
also runs the TypeScript SDK's `npm run check`: lint, types, version, README examples,
tests), building no image when the images are already published (below), and then three publish
jobs:

| Job            | Publishes                                                        | How                                                                                                                                                                                                                                   |
| -------------- | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pypi`         | `clickhouse-loom` (the Python SDK only) to PyPI                  | the verified wheel and sdist; trusted publishing (OIDC); files PyPI already has are skipped                                                                                                                                           |
| `npm`          | `@clickhouse/loom` to npm with provenance                        | the verified tarball; a published version is skipped; a prerelease goes to the `next` dist-tag; `NPM_TOKEN` as fallback                                                                                                               |
| `mcp-registry` | `server.json` to the MCP Registry as `io.github.ClickHouse/loom` | the committed file, whose version the `version` job checks against the tag; GitHub OIDC login with a pinned `mcp-publisher`; a listed version and a prerelease are skipped; `preflight` refuses a private repository before any build |

The images are already published by the tag run: a dispatch on a tag whose images carry the
version builds and tags none (`scripts/release/published_images.sh`), smoke-tests the published
ones, and goes on to the registries. Release images build without the cache, so a rebuild would
move the version's tags onto other bytes.

When only one of the two carries the version (a failed `docker-tag`), use "Re-run failed jobs" on
the tag's run, which tags the same build. "Re-run all jobs" would start with a `preflight` that
refuses the half-tagged version, and every "Re-run failed jobs" after it fails in that `preflight`
again. To recover from there, tag the missing image by hand from that run's digests (from its
`scanned-<artifact>-<arch>` artifacts), adding `-t ghcr.io/clickhouse/<image>:X.Y` for a final
version and `-t ghcr.io/clickhouse/<image>:latest` when
`python3 scripts/release/make_latest.py X.Y.Z` prints `true` (with `GH_TOKEN` and
`GITHUB_REPOSITORY=ClickHouse/loom` set), then dispatch the tag:

```bash
docker buildx imagetools create -t ghcr.io/clickhouse/<image>:X.Y.Z \
  ghcr.io/clickhouse/<image>@<amd64 digest> ghcr.io/clickhouse/<image>@<arm64 digest>
```

Never move a tag whose images are published: `published_images.sh` fails when a published image
was built from another commit than the run's, and a fix needs a new version.

A re-run after a partial publish is safe: each publish job skips what already exists.

**Why publish the verified files.** A job that rebuilt its own artifact could ship unchecked bytes. Publishing what `verify` passed means the GitHub Release and the registries carry the same bytes.

**Why the server is not on PyPI.** The server is deployed against ClickHouse, not imported as a library; it ships as the image. The root `pyproject.toml` is marked `Private :: Do Not Upload`, and PyPI carries only the SDK. The `pypi` and `npm` jobs need trusted publishers registered for this repository (their GitHub environments).

**Why SDK publishing is manual.** A registry publish cannot be undone: PyPI never accepts
a version twice. Publishing SDKs only on dispatch lets the release files be checked first.
The server image is automatic because it is a prerequisite of the GitHub Release's
installer, and creating the signed release tag is its review gate.

## Installing from a GitHub Release

When the registries have not been published, install from the release assets:

```bash
gh release download vX.Y.Z --repo ClickHouse/loom --pattern 'clickhouse_loom-*.whl'
pip install ./clickhouse_loom-*.whl

gh release download vX.Y.Z --repo ClickHouse/loom --pattern 'clickhouse-loom-*.tgz'
npm install ./clickhouse-loom-*.tgz
```

The coding-agent plugins install from the repository's marketplaces (`claude plugin marketplace add ClickHouse/loom`, `codex plugin marketplace add ClickHouse/loom`). The zips are for machines without access to the repository.

## Listings

Agents and people find Loom through these listings. Each reads files in this repository:

| Listing                                        | Reads                                                                                                                                                                                | How it updates                                                                                                                                 |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| MCP Registry, and the directories that copy it | `server.json`: a remote at `https://{loom_host}/mcp` with an optional secret `Authorization` header                                                                                  | the `mcp-registry` job on each dispatched release                                                                                              |
| Claude plugin directory                        | `integrations/claude-code-plugin/.claude-plugin/plugin.json` (`displayName`, `description`, `keywords`, `icon`, `documentationUrl`, `supportUrl`, `privacyPolicyUrl`) and the README | submitted once in the developer portal; later versions are picked up from the tracked branch. The icon is read only on the first submission    |
| Claude Code plugin suggestions                 | the `relevance` block of the `loom` entry in `.claude-plugin/marketplace.json`: the `loom` command, Loom's config files, an SDK dependency in a manifest                             | read from the marketplace on the default branch; shown only where an administrator allowlists the marketplace (`pluginSuggestionMarketplaces`) |
| Codex plugin directory                         | `integrations/codex-plugin/.codex-plugin/plugin.json` (`interface`)                                                                                                                  | submitted in the plugin portal; a plugin with lifecycle hooks is not eligible there, so the public listing needs a build without them          |
| Docs index for coding agents                   | `context7.json`                                                                                                                                                                      | refreshed from the default branch after the repository is added once                                                                           |
| Agent Skills index                             | `skills/loom-code`, `skills/loom` and the Claude Code plugin's skills; the contributor skills are marked internal in their frontmatter and are not offered                           | ranked by `npx skills add ClickHouse/loom` installs; nothing to submit                                                                         |

The `mcp-registry` job runs in the `mcp-registry` environment. Give it the same protection rules as `pypi` and `npm` before the first dispatch: GitHub creates a missing environment with none. Its login needs no secret.

After changing the Claude manifest or the marketplace entry, and before pushing, run `claude plugin validate integrations/claude-code-plugin` for the plugin and `claude plugin validate .` for the marketplace: they run the directory's structural checks.

## After the release

- Deploy it ([upgrades](../operations/upgrades.md)). The release notes' operator steps are the deploy checklist.
- Client plugins update through each client's plugin manager, not through a server deploy.

## Related

- [Unknown and removed fields](../reference/http-api.md#unknown-and-removed-fields): how a
  client sees a field a release removed
- [Contributing](../../CONTRIBUTING.md)
