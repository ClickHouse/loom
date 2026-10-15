# Changelog — clickhouse-loom

All notable changes to the Python SDK. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/). The top entry's version equals `pyproject.toml` and
`loom/_version.py` (a unit test checks).

## [Unreleased]

### Added

- `CodedWidthMismatch` and `OverrideNotShared` (`BackendUnavailable` subclasses) for the
  server's `CODED_WIDTH_MISMATCH` and `OVERRIDE_NOT_SHARED`.
- `memory.restore(if_superseded_by=..., only_if_superseded=...)`: undo a retirement only
  while it still stands, never a delete.

### Changed

- `CODED_WIDTH_MISMATCH` and `OVERRIDE_NOT_SHARED` are not retried: an operator, not a
  retry, fixes them.

## [1.0.0] - 2026-10-16

First public release.
