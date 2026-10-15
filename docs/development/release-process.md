# Release process

Loom follows SemVer. The server, the Python SDK, the TypeScript SDK, the web app and the `loom-code` skill share one version `X.Y.Z`. The two coding-agent plugins share their own version, bumped whenever a hook or skill changes: installed clients pick up a hook change only through a new plugin version.

## Where the version lives

Bump each of these in the release commit:

| Component            | Files                                                                                                                                                                                            |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Server               | `server/__init__.py` (`__version__`) and `pyproject.toml`                                                                                                                                        |
| Python SDK           | `sdk/python/pyproject.toml` and `sdk/python/loom/_version.py`                                                                                                                                    |
| TypeScript SDK       | `sdk/typescript/package.json`, `sdk/typescript/package-lock.json`, `sdk/typescript/src/version.ts`                                                                                               |
| Web app              | `frontend/package.json` and `frontend/package-lock.json`                                                                                                                                         |
| `loom-code` skill    | `.claude-plugin/plugin.json` and the `metadata.version` of `skills/loom-code/SKILL.md`                                                                                                           |
| Coding-agent plugins | `integrations/claude-code-plugin/.claude-plugin/plugin.json`, `integrations/codex-plugin/.codex-plugin/plugin.json` (their own version; bump when a hook, the shared library or a skill changes) |

The server reads its version from `server/__init__.py` only; the OpenAPI document, MCP `serverInfo` and `loom report` report it. Four checks keep the numbers consistent:

| Check                                    | Asserts                                                                                                            |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `tests/unit/test_release_version.py`     | `server/__init__.py` equals `pyproject.toml` and the top entry of the root changelog, and every surface reports it |
| `tests/unit/test_sdk_changelogs.py`      | each SDK's version equals its changelog's top entry; entries are newest first                                      |
| `npm run check:version` (TypeScript SDK) | its four version sources agree                                                                                     |
| `tests/unit/test_codex_plugin.py`        | both plugin manifests carry one version                                                                            |

Two plugin pull requests that bump to the same number merge without a conflict, and the second ships nothing to clients the first already updated. Check the plugin version on `main` before bumping.

## Changelogs

Three changelogs, each in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format:

- `CHANGELOG.md` at the root: the server, the web app and the plugins;
- `sdk/python/CHANGELOG.md`;
- `sdk/typescript/CHANGELOG.md`.

Changes accumulate under a top entry `## [X.Y.Z] - Unreleased`. On release day, replace `Unreleased` with the date in all three files:

```markdown
## [X.Y.Z] - YYYY-MM-DD
```

The release workflow refuses a tag unless all three changelogs contain `## [X.Y.Z] - YYYY-MM-DD` for its version. The root entry lists operator steps (new settings, backfills, table rebuilds) and client-visible wire changes, including every removed request field and retired flag. No registry of removed fields exists, so the release notes are where a client learns of one. Generate the wire diff instead of writing it:

```bash
python scripts/codegen/wire_changelog.py v<previous> HEAD
```

([code generation](./code-generation.md#wire-changelog)).

## Cutting the release

1. On a branch from `main`, open a pull request that sets the version everywhere above and
   dates the three changelog entries. CI must pass, including `make schema-check` and the
   strict migration dry run.
2. After it merges, tag the merge commit and push the tag:

   ```bash
   git tag vX.Y.Z <merge commit>
   git push origin vX.Y.Z
   ```

3. The tag push runs `.github/workflows/release.yml` (below), publishes the server image,
   and only then creates the GitHub Release. Check the image and release files.
4. To publish to the package registries, run the same workflow manually on the tag:

   ```bash
   gh workflow run release.yml --ref vX.Y.Z
   ```

5. Open the next `## [X.Y.Z] - Unreleased` entry in each changelog when the next change
   lands.

A tag containing `-` (`vX.Y.Z-rc.1`) makes a GitHub pre-release and does not move the `latest` image tag.

## What the release workflow does

`.github/workflows/release.yml` builds once, verifies the exact files, and publishes those files.

**On a tag push (`v*`)**:

1. `version` takes the version from the tag (`vX.Y.Z` → `X.Y.Z`) and refuses a ref that
   is not a `v*` tag or not SemVer; it fails unless each of the three changelogs has
   `## [X.Y.Z] - YYYY-MM-DD` and `server/__init__.py` reads `__version__ = "X.Y.Z"`;
2. `clickhouse` names the ClickHouse release the Compose bundle runs: the release
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
   A ClickHouse downgrade (see step 7) is refused here first, so such a run stops before
   it builds and pushes images;
3. `build` writes the version into the Python and TypeScript SDK sources and builds the
   Python SDK wheel and sdist, the npm tarball (after the TypeScript SDK's `check:dist`),
   and zips of the two plugins (`loom-claude-code-plugin-vX.Y.Z.zip`,
   `loom-codex-plugin-vX.Y.Z.zip`); it also builds the source-free Compose bundle and its
   SHA-256 file, naming that ClickHouse release;
4. `verify` checks those exact files: their names and embedded versions, the plugin
   manifests, no tests in the Claude Code zip, the executable bits git records, and
   that the bundle names the ClickHouse release the `clickhouse` job checked, and
   smoke-tests the wheel in a clean venv with `scripts/gates/sdk_wheel_smoke.py`;
5. `docker` publishes `ghcr.io/clickhouse/loom` for linux/amd64 and linux/arm64, with
   provenance and an SBOM;
6. `docker-smoke` installs the bundle with that image, on the ClickHouse release it
   names, on amd64 and arm64;
7. `github-release` creates the GitHub Release only after that image exists, or updates it
   on a re-run, with notes generated from the commits since the previous tag, and attaches
   the verified files. A bundle the release already carries is kept, and so is its checksum
   (one the release lost is written again from it): replacing an asset deletes it first,
   and a failed upload would leave an installed version without its bundle. A new bundle
   never names an older ClickHouse release than the bundles of the 30 newest releases
   (prereleases included) do, since an install upgrading to it would start an older
   server on data a newer one wrote; the job fails instead, unless the dispatch adds
   `-f allow_clickhouse_downgrade=true`. This job runs for one tag at a time, so two
   tags released together cannot both pass the check against the same older bundle.
   Only one more such job waits: a third tag's publish cancels the waiting one, which must
   then be re-run by hand (Re-run failed jobs). A refusal here comes after `docker` pushed
   the tag's image tags (and `:latest` for a final release): re-run the tag with
   `allow_clickhouse_downgrade`, or move `:latest` back to the previous release by hand.

The server image is published before the GitHub Release, so an installer shown on the
release page never points at a missing image.

**On manual dispatch** (`workflow_dispatch` on a tag ref), the same jobs run (`build`
also runs the TypeScript SDK's `npm run check`: lint, types, version, README examples,
tests), and then two SDK publish jobs:

| Job    | Publishes                                       | How                                                                                                                     |
| ------ | ----------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `pypi` | `clickhouse-loom` (the Python SDK only) to PyPI | the verified wheel and sdist; trusted publishing (OIDC); files PyPI already has are skipped                             |
| `npm`  | `@clickhouse/loom` to npm with provenance       | the verified tarball; a published version is skipped; a prerelease goes to the `next` dist-tag; `NPM_TOKEN` as fallback |

The Docker image is already published by the tag run. Manual dispatch still rebuilds the
same immutable version safely, while publishing the SDK registries.

A re-run after a partial publish is safe: each publish job skips what already exists.

**Why publish the verified files.** A job that rebuilt its own artifact could ship unchecked bytes. Publishing what `verify` passed means the GitHub Release and the registries carry the same bytes.

**Why the server is not on PyPI.** The server is deployed against ClickHouse, not imported as a library; it ships as the image. The root `pyproject.toml` is marked `Private :: Do Not Upload`, and PyPI carries only the SDK. The registry jobs need PyPI and npm trusted publishers registered for this repository (the `pypi` and `npm` GitHub environments).

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

## After the release

- Deploy it ([upgrades](../operations/upgrades.md)). The root changelog's operator steps are the deploy checklist.
- Client plugins update through each client's plugin manager, not through a server deploy.

## Related

- [Unknown and removed fields](../reference/http-api.md#unknown-and-removed-fields): how a
  client sees a field a release removed
- [Contributing](../../CONTRIBUTING.md)
