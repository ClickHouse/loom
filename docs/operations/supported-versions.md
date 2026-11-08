# Supported versions

Loom ships one version for the server, both images, the web app, both SDKs, the root skills and the
Helm chart's `appVersion` ([release process](../development/release-process.md)). This page says
which of those versions get fixes, which SDK works with which server, and how a change that removes
something is announced.

## Release lines

| Line | What it gets                                                                                      |
| ---- | ------------------------------------------------------------------------------------------------- |
| N    | the newest minor release (`X.Y`): every fix, as a patch release (`X.Y.Z+1`) when it cannot wait   |
| N−1  | the minor release before it: security fixes and fixes for data loss or a wrong answer, as patches |
| N−2  | none: upgrade to N or N−1                                                                         |

A minor release comes from `main` about every two weeks; a patch comes from the line's
`release/X.Y` branch, with the fix merged to `main` first. An upgrade follows
[upgrades](./upgrades.md); two adjacent versions serve side by side while one replaces the other
([rolling upgrades](../architecture/rolling-upgrades.md)).

## SDKs and servers

The SDKs (`clickhouse-loom` on PyPI, `@clickhouse/loom` on npm) are published with every release, at
the server's version. An SDK at `X.Y` works with a server at `X.Y` or `X.(Y−1)`:

- a field the SDK sends that the older server does not know is served and named in the answer's
  `ignored_args` ([unknown and removed fields](../reference/http-api.md#unknown-and-removed-fields));
- an operation the older server does not serve answers `404`, which the SDKs raise as `NotFound`
  (Python `loom.errors.NotFound`, TypeScript `NotFound`): a call never succeeds silently against a
  server that cannot do it.

An older SDK keeps working against a newer server for as long as the server serves what it calls; a
removal follows the rule below.

## Coding-agent plugins

The Claude Code and Codex plugins carry their own version, and their manifests name the oldest
server their hooks support (`minServerVersion`). A session against an older server starts with one
`loom:` line saying so, and the plugin goes on working where it can
([Claude Code](../features/integrations/claude-code.md)).

## Removing something

A removal is announced in the release notes of the minor release before the one that removes it,
under "What changed for clients". Until the removal, the server serves the old field or flag; after
it, a removed request field is still accepted and named in `ignored_args`, so a client sending it
learns of the change without failing.

## Release artifacts

Every release image is built on the architecture it runs on, scanned for critical vulnerabilities
with a fix, and published with its provenance and an SBOM, and signed with a keyless signature from
the release workflow, on each image's multi-arch index; [container
images](./deployment.md#container-images) gives the `cosign verify` command.
