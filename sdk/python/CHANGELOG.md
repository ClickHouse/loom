# Changelog — clickhouse-loom

All notable changes to the Python SDK. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/). The top entry's version equals `pyproject.toml` and
`loom/_version.py` (a unit test checks).

## [Unreleased]

### Added

- `loom.login()`, `loom.logout()`, `loom.whoami()` and `python -m loom login|logout|whoami`:
  sign in with the OAuth device grant on any browser (password or SSO). The session lives in
  `~/.loom/credentials` (mode 0600, one entry per `profile` / `LOOM_PROFILE`); an `Agent`
  given no `api_key` and no `LOOM_API_KEY` uses it and refreshes it as it goes.
- `CodedWidthMismatch` and `OverrideNotShared` (`BackendUnavailable` subclasses) for the
  server's `CODED_WIDTH_MISMATCH` and `OVERRIDE_NOT_SHARED`.

### Changed

- `CODED_WIDTH_MISMATCH` and `OVERRIDE_NOT_SHARED` are not retried: an operator, not a
  retry, fixes them.

## [1.0.0] - 2026-10-16

First public release.
