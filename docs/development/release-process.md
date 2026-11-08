# Release process

Loom follows SemVer. The server, the Python SDK, the TypeScript SDK, the web app and the root skills (`loom-code`, `loom`) share one version `X.Y.Z`. The two coding-agent plugins share their own version, bumped on a behaviour change to any file the plugin ships (`hooks/`, `lib/`, `bin/`, `skills/`, `.mcp.json`, the Codex `plugin-defaults.json`): installed clients pick up a hook change only through a new plugin version. Both manifests also declare `minServerVersion`, the oldest server the plugin's hooks support: when the server's `GET /v1/health` reports an older `version`, SessionStart prints one `loom:` line asking for a server upgrade, and says nothing when the version cannot be read. It rises with a release ([cutting the release](#cutting-the-release), step 2).

## Where the version lives

`.venv/bin/python scripts/release/prepare.py bump X.Y.Z` bumps each of these in the release commit, except the plugins, and writes the [release notes](#release-notes):

| Component            | Files                                                                                                                                                                                                                                                                                                                                                               |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Server               | `server/__init__.py` (`__version__`), `pyproject.toml`, `uv.lock` and `server.json`                                                                                                                                                                                                                                                                                 |
| Python SDK           | `sdk/python/pyproject.toml` and `sdk/python/loom/_version.py`                                                                                                                                                                                                                                                                                                       |
| TypeScript SDK       | `sdk/typescript/package.json`, `sdk/typescript/package-lock.json`, `sdk/typescript/src/version.ts`                                                                                                                                                                                                                                                                  |
| Web app              | `frontend/package.json` and `frontend/package-lock.json`                                                                                                                                                                                                                                                                                                            |
| Root skills          | `.claude-plugin/plugin.json` and the `metadata.version` of `skills/loom-code/SKILL.md` and `skills/loom/SKILL.md`                                                                                                                                                                                                                                                   |
| Helm chart           | `deploy/helm/loom/Chart.yaml`: `appVersion` (the default tag of the server and extractor images, and the chart's `app.kubernetes.io/version` label) and the chart's own `version`: a minor for a major or minor release, a patch for a patch release                                                                                                                |
| Coding-agent plugins | `integrations/claude-code-plugin/.claude-plugin/plugin.json`, `integrations/codex-plugin/.codex-plugin/plugin.json` (their own version, and `minServerVersion`, which rises with a release (step 2 below); bump the version on a behaviour change to any file the plugin ships: `hooks/`, `lib/`, `bin/`, `skills/`, `.mcp.json`, the Codex `plugin-defaults.json`) |

The server reads its version from `server/__init__.py` only; the OpenAPI document, MCP `serverInfo`, `loom report` and `GET /v1/health` (checked by `tests/unit/test_health_problems.py`) report it. These checks keep the numbers consistent:

| Check                                          | Asserts                                                                                                                               |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `tests/unit/test_release_version.py`           | every surface reports the server's version, and `server.json` keeps the registry listing's shape                                      |
| `npm run check:version` (TypeScript SDK)       | its four version sources agree                                                                                                        |
| `tests/unit/test_codex_plugin.py`              | both plugin manifests carry one version and one `minServerVersion`, no newer than the server's version                                |
| `tests/unit/test_release_prepare.py`           | every file `prepare.py` bumps names the server's version                                                                              |
| `prepare.py check`, `check-tag`, `check-chart` | the release's `version` job ([step 1](#what-the-release-workflow-does)): one version everywhere, the candidate order, the chart moved |

Two plugin pull requests that bump to the same number merge without a conflict, and the second ships nothing to clients the first already updated. Check the plugin version on `main` before bumping.

## Release notes

The run that creates the GitHub Release generates its notes from the commits since the previous final
release's tag (`prepare.py previous`), so a final on its candidate's commit lists them too;
a re-run or the later registry dispatch keeps them as they are. What commits cannot say goes in a
"Release notes" section of the release pull request's description, so it is reviewed before the tag:
the pull requests' changelog entries, grouped by their changelog category (pull requests in a
category that needs no entry are left out), then operator steps (new settings, backfills, table
rebuilds) and client-visible wire changes, including every removed request field and retired flag.
No registry of removed fields exists, so the release notes are where a client learns of one.
`prepare.py` writes the section to `release-notes.md` (not committed), from the squash merges
since the highest final tag below the release:

- each pull request's changelog entry under its category, in the template's order, or its title when
  the entry is empty; one whose description names no single category is listed under "Unclassified"
  for the author to place;
- each "Operator steps" item, as written, under its pull request's number, from every pull request,
  including one in a category that needs no entry; an item that says there are none (`none`, `N/A`,
  `No operator steps.`) or is the template's own text is left out, and the author prunes the rest
  that name no step;
- the wire diff ([code generation](./code-generation.md#wire-changelog)).

A fix that a patch release of an older line already shipped is listed again, and so is a pull
request reverted in the same range, beside its revert; the author drops them.

It fetches the upstream repository's tags itself, reads the pull requests with `gh` and the wire
diff through the server's registry, so it needs a signed-in `gh` and the project's environment
(`uv sync`). `.venv/bin/python scripts/release/prepare.py notes X.Y.Z` prints the section again
without bumping anything: run it after rebasing the release pull request just before it merges,
and again at the highest-numbered candidate's commit before the final tag, so the notes name every
pull request the tag ships.

## Cutting the release

1. Check that a scheduled run of `.github/workflows/docker-smoke.yml` on `main` from the past week
   passed:

   ```bash
   gh run list -R ClickHouse/loom --workflow docker-smoke.yml --event schedule --branch main -L 1 --json conclusion,createdAt,headSha
   ```

   Or start a fresh one with `gh workflow run docker-smoke.yml -R ClickHouse/loom --ref main` and read
   it with `--event workflow_dispatch` in place of `--event schedule`. It scans both images as the
   release does, and a scheduled run's failure notifies only the last editor of its schedule. A red
   scan names the package: move the pin that brings it in (a `Dockerfile` `ARG` or base-image digest,
   or a lock file) on `main` first. The base image's own packages move only with a new base-image
   digest; a package an `apt-get install` layer adds moves with any uncached build.

2. On a branch from `main`, run `.venv/bin/python scripts/release/prepare.py bump X.Y.Z`, and open a
   pull request with its changes and `release-notes.md` as the [release notes](#release-notes)
   section of its description; edit the notes where an entry needs it. CI must pass, including
   `make schema-check` and the strict migration dry run.
   When a merged pull request's changelog entry says it needs the next server release, the same
   pull request raises both plugins' `minServerVersion` to this version and bumps both plugin
   versions: clients get a manifest only through a new plugin version.
3. After the release pull request merges, tag its merge commit and push the tag. To soak a
   release candidate first, tag `vX.Y.Z-rc.1` there instead, and the final later on the
   highest-numbered candidate's commit (below):

   ```bash
   git tag vX.Y.Z <merge commit>
   git push origin vX.Y.Z
   ```

4. The tag push runs `.github/workflows/release.yml` (below), publishes the server and
   extractor images, and only then creates the GitHub Release, then checks the published installer
   from outside (`release-check`). Read that run before step 5.
5. To publish the SDKs and the MCP Registry listing, run the same workflow manually on the tag:

   ```bash
   gh workflow run release.yml --ref vX.Y.Z
   ```

   Once that run's `pypi` and `npm` jobs pass, check the SDKs as a user installs them, with the
   upgrade from the previous release:
   `gh workflow run release-check.yml -f version=X.Y.Z -f previous_version=<previous X.Y.Z>`.
   `previous_version` must be a published release earlier than `version` that carries
   `install.sh` and `loom-stack.tar.gz`; leave it out when there is none.

6. Paste the release notes made at the final tag's commit ([release notes](#release-notes)) into
   the GitHub Release, above the generated notes.

A release candidate is a `vX.Y.Z-rc.N` tag, and makes a GitHub pre-release; the release refuses any
other prerelease tag. The first candidate goes on the release pull request's merge commit, whose
files name `X.Y.Z`, so the candidate's server already reports `X.Y.Z`; a later one, after a fix,
goes on a later commit of `main`. The final `vX.Y.Z` tag goes on the highest-numbered candidate's
commit after the soak. The final's bundle keeps the ClickHouse release of the highest-numbered
candidate on that commit with a published bundle; its images are built again from the same
commit, uncached, so an OS package may be newer than in the soaked image. A run of a
candidate tag after its final is refused. One on the final's commit is told to run the final. One
numbered below the highest-numbered candidate on the final's commit, such as an older candidate
dispatched to publish its SDKs, has nothing to run, and its tag stays. Any other is a stray: it
also fails the final's later runs until it is deleted. The chart's `appVersion` names `X.Y.Z` too,
so a soak on Kubernetes sets `image.tag` and `extractor.image.tag` to `X.Y.Z-rc.N`.

Only the highest final version becomes the repository's Latest release (`releases/latest` serves the
installer) and moves `:latest`: a prerelease or a patch to an older line leaves both where they are.
The versions are read from the repository's tags, so delete a final tag that will not ship, or
Latest stays below it.

## Rehearsing a release

A dry run builds, verifies and smoke-tests everything a release would, on any branch, and publishes
nothing: no image push, GitHub Release or registry upload.

```bash
gh workflow run release.yml --ref <branch> -f dry_run=true
```

It runs at the version `server/__init__.py` names. Its images go to run artifacts instead of
ghcr.io, without the provenance and SBOM attestations that only a registry keeps, and
`docker-smoke` loads them from there. The `preflight` job reports each missing
[prerequisite](#before-the-first-release) as a warning instead of failing. What only a published
release exercises, a dry run does not: `docker-tag` (joining each image's two digests, the Latest
decision, `:latest`), `sign`, the GitHub Release, the registry publishes, `release-check`, the
published-images check (`scripts/release/published_images.sh`) with the skipped-build path it
selects, the candidate-order check (`prepare.py check-tag`, which a dry run of a branch skips with
a notice), and the chart check when the version is already tagged on another commit.

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
   is not a `v*` tag (unless the run is a [dry run](#rehearsing-a-release)) or not `X.Y.Z` or
   `X.Y.Z-rc.N`. It fails when any of these is false (for `vX.Y.Z-rc.N`, against `X.Y.Z`):
   - `prepare.py check`: `server/__init__.py` reads `__version__ = "X.Y.Z"`, and every other
     version file, `server.json` included, names that version;
   - `prepare.py check-tag`: a candidate `vX.Y.Z-rc.N` before `vX.Y.Z` is tagged, and a final
     `vX.Y.Z` on the commit of its highest-numbered candidate;
   - `prepare.py check-chart`: the chart's version is above the previous release's;
2. `preflight` fails the run on a missing [prerequisite](#before-the-first-release);
3. `clickhouse` names the ClickHouse release the Compose bundle runs
   (`scripts/release/release_clickhouse.sh`), migrates a fresh server of that release and runs
   `make schema-check` before `build` may pin it, and refuses a ClickHouse downgrade (see step 8),
   so such a run stops before it builds and pushes images
   ([when it stops](#when-the-clickhouse-job-stops)). The release is the first of:
   - the one this version's published bundle already names, so rerunning the installer for a
     version never moves ClickHouse (a release without a bundle resolves anew, as `github-release`
     never replaces one, so nobody can have installed it);
   - a dispatch's `clickhouse_image`, which for a final is used in place of any release its
     candidates on the same commit published;
   - for a final, the one the highest-numbered candidate on the same commit with a published
     bundle was soaked with;
   - the release `clickhouse/clickhouse-server:latest` names now (`dev/clickhouse_release.sh`);
4. `build` writes the version into the Python and TypeScript SDK sources and builds the
   Python SDK wheel and sdist, the npm tarball (after the TypeScript SDK's `check:dist`),
   and zips of the two plugins (`loom-claude-code-plugin-vX.Y.Z.zip`,
   `loom-codex-plugin-vX.Y.Z.zip`); it also builds the source-free Compose bundle and its
   SHA-256 file, naming that ClickHouse release, and the release's `install.sh`, which installs
   this version;
5. `verify` checks those exact files: their names and embedded versions, the plugin
   manifests, no tests in the Claude Code zip, the executable bits git records, and
   that the bundle names the ClickHouse release the `clickhouse` job checked and `install.sh`
   installs this version, and smoke-tests the wheel in a clean venv
   (`scripts/gates/sdk_wheel_smoke.py`);
6. `docker` builds `ghcr.io/clickhouse/loom` and `ghcr.io/clickhouse/loom-extractor`
   (the document extractor service), each for linux/amd64 and linux/arm64 on that
   architecture's own runner and without the build cache (so from the package archives as
   they are that day), with provenance and an SBOM, pushes each by digest and scans it;
   `docker-tag` joins each image's two digests under its tags, and tags neither image until
   all four have passed (a failure there can leave one tagged, and re-running it tags both);
   `sign` then signs, by digest, the index `docker-tag` made for each image, with a keyless
   signature (cosign, with the run's GitHub identity, so the certificate names `release.yml`
   and the tag); a run on a tag whose images are out signs nothing. Every run then verifies
   both images' signatures as users do, so the GitHub Release, PyPI, npm and the MCP Registry
   wait for a signature that checks ([a failed tag or signature](#a-failed-tag-or-signature));
7. `docker-smoke` installs the bundle with those images, on the ClickHouse release it
   names, on amd64 and arm64, then runs the release's own `install.sh` and bundle as the
   one-liner in clean Debian, Ubuntu and Alpine containers (install, round trip, re-run, a
   refused downgrade under each one's own `sh` and `awk`, and an upgrade over the previous
   final release's installer, or, in a dry run of a commit after its version's release, over
   that release's own; `scripts/gates/installer_rehearsal.py`);
8. `github-release` creates the GitHub Release only after both images exist, with notes
   generated from the commits since the previous final release (`prepare.py previous`, an
   output of `version`; a candidate's notes start there too), or updates it on a re-run, keeping
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
   which also publishes the SDKs and the MCP Registry listing. Dispatch it either with the newer
   release the error names, when that release dumps the tag's `schemas/schema.sql`:
   `gh workflow run release.yml --ref vX.Y.Z -f clickhouse_image=clickhouse/clickhouse-server:A.B.C.D`;
   or accepting the downgrade:
   `gh workflow run release.yml --ref vX.Y.Z -f allow_clickhouse_downgrade=true`.
   When neither is taken, move `:latest` back to the previous release on both images, as in
   [a failed tag or signature](#a-failed-tag-or-signature);
9. `release-check` (on a tag push) runs `.github/workflows/release-check.yml` against what
   was just published, signed in to nothing: the one-liner from github.com in the same clean
   containers, pulling both images anonymously, and verifies both images' signatures, which
   must come from `release.yml` on that tag, as a user does
   ([container images](../operations/deployment.md#container-images)). A release published
   before the release workflow signed carries none, so a check of it fails its `signatures` job.

The server and extractor images are published before the GitHub Release, so an installer
shown on the release page never points at a missing image.

**On manual dispatch** (`workflow_dispatch` on a tag ref), the same jobs run except
`release-check`, building no image when the images are already published (below). `build` also
runs the TypeScript SDK's `npm run check` (lint, types, version, README examples, tests). Then
three publish jobs run:

| Job            | Publishes                                                        | How                                                                                                                                                                                                                                   |
| -------------- | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pypi`         | `clickhouse-loom` (the Python SDK only) to PyPI                  | the verified wheel and sdist; trusted publishing (OIDC); files PyPI already has are skipped                                                                                                                                           |
| `npm`          | `@clickhouse/loom` to npm with provenance                        | the verified tarball; a published version is skipped; a prerelease goes to the `next` dist-tag; `NPM_TOKEN` as fallback                                                                                                               |
| `mcp-registry` | `server.json` to the MCP Registry as `io.github.ClickHouse/loom` | the committed file, whose version the `version` job checks against the tag; GitHub OIDC login with a pinned `mcp-publisher`; a listed version and a prerelease are skipped; `preflight` refuses a private repository before any build |

The images are already published by the tag run: a dispatch on a tag whose images carry the
version builds and tags none (`scripts/release/published_images.sh`), smoke-tests the published
ones, and goes on to the registries. Release images build without the cache, so a rebuild would
move the version's tags onto other bytes.

Never move a tag whose images are published: `published_images.sh` fails when a published image
was built from another commit than the run's, and a fix needs a new version.

A re-run after a partial publish is safe: each publish job skips what already exists.

**Why publish the verified files.** A job that rebuilt its own artifact could ship unchecked bytes. Publishing what `verify` passed means the GitHub Release and the registries carry the same bytes.

**Why the server is not on PyPI.** The server is deployed against ClickHouse, not imported as a library; it ships as the image. The root `pyproject.toml` is marked `Private :: Do Not Upload`, and PyPI carries only the SDK. The `pypi` and `npm` jobs need trusted publishers registered for this repository (their GitHub environments).

**Why SDK publishing is manual.** A registry publish cannot be undone: PyPI never accepts
a version twice. Publishing SDKs only on dispatch lets the release files be checked first.
The server image is automatic because it is a prerequisite of the GitHub Release's
installer, and creating the signed release tag is its review gate.

### A failed tag or signature

A failed `docker-tag` (one image tagged, not the other) or a failed `sign` (Sigstore down, say):

- Use "Re-run failed jobs" on the run that tagged the images (the tag push, or the dispatch that
  built and tagged them), within 30 days of it, as long as its artifacts are kept. That attempt
  keeps its `preflight` result and `docker-tag`'s digests, so it tags the same build and signs
  what it tagged. Another run on the tag (a dispatch, a second push) is a run of its own: re-run
  that run's failed jobs, or dispatch the tag again, once the tagging run has signed.
- Never re-run a run that failed before tagging once another run has tagged: its `preflight` saw
  no images, so it rebuilds and would move the published tags.
- Once "Re-run all jobs" has replaced the tagging run's attempt, or the 30 days have passed, no
  run can tag or sign that build: a run on a half-tagged version fails its `preflight`, and a run
  on a published tag verifies signatures but cannot make them. Release a new version, and if
  `:latest` moved, point it back at the previous release for both images:

  ```bash
  docker buildx imagetools create -t ghcr.io/clickhouse/<image>:latest ghcr.io/clickhouse/<image>:<previous X.Y.Z>
  ```

  The previous release's index keeps its signature, which belongs to its digest.

### When the clickhouse job stops

A run that stopped in `clickhouse` published nothing.

- **`latest` names a bad release**: dispatch the tag with an exact one,
  `gh workflow run release.yml --ref vX.Y.Z -f clickhouse_image=clickhouse/clickhouse-server:A.B.C.D`.
  A bundle already published keeps its own, and the run warns that it ignored the input.
- **The release does not dump the tag's `schemas/schema.sql`**: regenerate it on that release (the
  [schema dump](./migrations.md#schema-dump-and-check) commands with
  `LOOM_CLICKHOUSE_IMAGE=clickhouse/clickhouse-server:A.B.C.D`) and commit it. For a candidate, tag
  the next `-rc.N` on the fix's commit. For a final (one with no candidate on its commit that
  published a bundle, as only that one resolves `latest`), delete its tag
  (`git push origin :refs/tags/vX.Y.Z`, and `git tag -d vX.Y.Z` in every clone that fetched it, as a
  fetch never removes a tag), then tag a new candidate on the fix's commit. Either way dispatch the
  new candidate with the same `-f clickhouse_image=...`, as its tag push resolves `latest` again;
  soak it, and tag the final there, which keeps that release.
- **A downgrade during the soak**: a final carrying its candidate's release is checked too, so a
  patch to an older line published during the soak with a newer release stops it. Two ways out:
  - delete the final's tag as above (it published nothing), tag a new candidate on that commit,
    soak it, and tag the final there again. The candidate's tag push resolves `latest`; when that
    is older than the release the error names, dispatch the candidate with that release:
    `gh workflow run release.yml --ref vX.Y.Z-rc.N -f clickhouse_image=clickhouse/clickhouse-server:A.B.C.D`;
  - or dispatch the final with `-f allow_clickhouse_downgrade=true` and accept the downgrade for
    that patch's installs.

Each dispatch above also runs the `pypi` and `npm` jobs, so a dispatched candidate publishes its
SDKs once their environments are approved; decline those approvals to publish only the images and
the GitHub Release.

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
