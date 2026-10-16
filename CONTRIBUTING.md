# Contributing to Loom

Loom is Apache-2.0 licensed. By submitting a contribution you agree to license it under the same terms.

The [development guide](./docs/development/repository-guide.md) goes deeper: [repository layout](./docs/development/repository-guide.md), [local development](./docs/development/local-development.md), [testing](./docs/development/testing.md), [code generation](./docs/development/code-generation.md), [migrations](./docs/development/migrations.md), [release process](./docs/development/release-process.md) and [documentation style](./docs/development/documentation-style.md).

## Before you start

- **Discuss material changes first.** Open an issue before significant work on an architectural or wire-surface change.
- **Search existing issues.** The bug may already be tracked.
- **Keep changes focused.** One concern per pull request. A refactor, a bug fix and a feature do not share a commit.

## Filing a bug

`loom report` prints an issue-ready Markdown body with the `loom doctor` diagnostic attached. `--open` also opens the new-issue page:

```bash
loom report --open
loom report | pbcopy                        # macOS
loom report | xclip -selection clipboard    # Linux
```

The diagnostic holds Python and Loom versions, platform, ClickHouse reachability, and worker and embedder configuration. It records only whether `LOOM_JWT_SECRET` and `LOOM_CH_PASSWORD` are set, never their values.

The bug form (`.github/ISSUE_TEMPLATE/bug.yml`) asks for:

- what you ran (op, SDK call, `loom` or `make` command, plugin hook) and what came back, with its `error.code`;
- the expected behaviour;
- the smallest reproduction. For state-dependent failures, add the latest applied migration: `SELECT max(filename) FROM loom._migrations`;
- the environment (paste `loom report`) and the relevant logs;
- confirmation that tokens, keys, passwords and personal or customer data are removed.

Blank issues stay enabled, because `loom report --open` opens one with the report's header filled in. Security issues go through [SECURITY.md](./SECURITY.md), never a public issue.

## Development setup

```bash
git clone https://github.com/ClickHouse/loom.git
cd loom

# Build deps first: `lemmagen3` (the `[lemma]` extra, pulled in by `[dev]`) has
# no wheel and compiles against Python.h. A plain `pip install -e .` (the
# `loom up` path in the quickstart) needs neither.
#   Debian/Ubuntu:  sudo apt-get install -y build-essential python3-dev
#   macOS:          xcode-select --install   (Python.h ships with python.org / Homebrew Python)

# Python env (Python 3.12+)
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"

# Start ClickHouse locally, with the config and lemmatizer dictionary Loom needs
# (a bare clickhouse-server container fails every memory_embeddings insert)
bash dev/ci_start_clickhouse.sh

# Apply migrations + start the control plane
make migrate
make server
# → http://localhost:7777
```

This runs the same server as the Docker image, with `--reload` and without a supervisor ([deployment](./docs/operations/deployment.md)). The `/me` dashboard returns 503 until you run `npm --prefix frontend run build`. Admin ops stay reachable at `/v1/admin/*` either way.

For Loom and ClickHouse together, use the dev compose stack:

```bash
cp .env.example .env    # once; `make dev` does this for you
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up --build    # Loom + CH only
```

`--env-file .env` makes compose read `.env` (see the note in `Makefile`). Without it you get the compose file's own fallbacks (auth on, OpenAI provider with no key), not the stack the [quickstart](./docs/getting-started/quickstart.md) describes.

## Branch, change, check

1. Branch from `main`. CI runs only on pull requests into `main` and pushes to it. A pull request stacked on another branch gets no checks until it targets `main`.
2. Make the change with its tests ([testing](./docs/development/testing.md)). If it moves a source that something is generated from, regenerate it ([code generation](./docs/development/code-generation.md)). If it changes the schema, follow [migrations](./docs/development/migrations.md).
3. Run what CI runs:

```bash
make format        # ruff, Biome, Prettier, Taplo, shfmt (CI: make format-check)
make lint          # ruff, the function-length and secret gates, generated files current
make typecheck     # mypy --strict on the SDK + the server's mypy ratchet
make test-unit     # pytest tests/unit and pytest bench (no ClickHouse needed)

# Frontend
npm --prefix frontend ci && npm --prefix frontend run test && npm --prefix frontend run build

# Integration suite (needs a running server and ClickHouse)
LOOM_BASE_URL=http://localhost:7777 \
LOOM_API_KEY=$(cat ~/.loom/token 2>/dev/null) \
  pytest tests/integration -q
```

Unit tests run in random order in CI (`pytest -p randomly`), so a test must not depend on the one before it. Tests skip cleanly when their env config is absent: use `os.environ.get(...) or pytest.skip(...)` in new fixtures. Two ratchets only shrink: `scripts/gates/function_length_gate.py` (no file gains an oversized function) and `scripts/gates/mypy_gate.py` (no new type errors). A failing gate is fixed at the source (regenerate the file, write the test), not by loosening the gate. [`tests/README.md`](./tests/README.md) covers the suite's layers, tiers, isolation guards and skip policy.

## Formatting

`make format` runs one pinned tool per language: ruff (Python), Biome (JS/TS/JSON/CSS), Prettier (Markdown/YAML), Taplo (TOML), shfmt (shell). CI runs `make format-check`. Each tool's config names what it leaves alone: test fixtures, migrations, generated SDK code, the code index's extractor modules (their code is the index's version stamp) and the prompt Markdown under `skills/`. To keep `git blame` on the code rather than the formatting commit:

```bash
git config blame.ignoreRevsFile .git-blame-ignore-revs
```

## Commits and sign-offs (DCO)

- **Messages say why.** The diff says what changed.
- **Sign off every commit** under the [Developer Certificate of Origin](https://developercertificate.org/): `git commit -s -m "your message"`. There is no CLA.
- **Comments explain why**, not what or when: no history, dates, names or ticket numbers in source.

## Material changes

A new storage table, a new wire-op group, an auth-model change, a new external-system dependency, or a change to the identity tuple needs a design note in the pull request description: what changes, why, the alternatives and what each costs, and the migration story. When the design outlives the pull request, update the matching page under `docs/architecture/` so it describes the system as it is. There are no separate design-record files.

## Conventions that aren't obvious

- **Settings** live in `server/settings/`, one mixin per area; a field is `LOOM_<FIELD>` in the environment. Give a new field a comment saying what it controls and what its values mean, read it as `settings.<field>` (never `getattr` with a default), and run `python scripts/codegen/settings_reference.py` to regenerate [the configuration reference](./docs/reference/configuration.md).
- **Migrations** are numbered SQL under `schemas/migrations/`, additive only. A merged migration is never edited or deleted: deployed ledgers verify every file's checksum. After adding one, run `make schema-dump` and commit `schemas/schema.sql`.
- **ReplacingMergeTree rows carry every column.** A new version row of `loom.memory` / `loom.shared` goes through the builders in `server/memory/store/rows.py`. A new column is added to the builders, and to `loom.memory_versions` and its view, in the same change.
- **Every write stamps `actor` and `source_class`** from the token, never from the request body. Every write that creates or changes memory leaves a hash-only audit row.
- **A table that can hold memory content** needs an erasure saga step and a residue-census row (`server/erasure/store/census.py`).
- **Code-index extractor modules** (`extractor_stamp.EXTRACTOR_MODULES`) carry the index's version stamp: a code change to one re-indexes every repository. Comments and docstrings are not stamped.
- **Broad excepts say why:** `except Exception as e:  # noqa: BLE001 — <what must not fail>`.
- **Background loops tag their queries** with `set_query_tag(...)` so every ClickHouse query is attributed in `system.query_log`.
- **Workers are rows** in `server/workers/registry.py`.

## Wire-op contributions

Adding a new operation ([full walkthrough](./docs/development/adding-an-operation.md)):

1. Define `<Op>Input` / `<Op>Output` Pydantic models in `server/<domain>/api/models_<group>.py`.
2. Write the handler in `server/<domain>/app/<group>.py`; it imports the models at module top.
3. Declare the `Operation` in `server/<domain>/api/ops_<group>.py` and append it to that module's `OPERATIONS`.
4. Regenerate the SDK op tables and the Python per-op client: `python scripts/codegen/sdk_py_gen.py`, `python scripts/codegen/sdk_py_client_gen.py` and `python scripts/codegen/sdk_ts_gen.py` (CI runs each with `--check`). Add a method to the hand-written client in `sdk/python/loom/<group>.py` when the op belongs in the high-level API.
5. Add unit tests, and an integration test under `tests/integration/` for a new read or write path.

The HTTP and MCP transports discover registered operations; you don't touch transport code.

An operation or field may be renamed or removed. Say so in the PR description and tell the consumers that call it. A client that still sends a removed field is served, with the field named in `ignored_args` ([HTTP API](./docs/reference/http-api.md#unknown-and-removed-fields)). Retiring a flag deletes the setting, its branch and any per-request field that carried it, in one change.

## The open-source copy rule

Repository text (code, comments, tests, docs, fixtures) stays product-neutral and timeless. `tests/unit/test_public_copy.py` enforces the lists:

- **No adjacent products.** Name the technique ("a separate vector store beside the relational store"), not a brand. Integrations Loom ships (Langfuse, ClickStack/HyperDX, Claude Code, Codex, Cursor, the LLM providers, ClickHouse) may be named.
- **No people, no internal hosts or namespaces.**
- **No citations** of papers or design-decision records.
- **No history in the text.** No dates, no "we changed", "previously", "new in". Write what the code does.
- **Server code names no public benchmark** as the reason for a choice. The adapters under `bench/` are those benchmarks and keep their names.

Docs follow the [documentation style guide](./docs/development/documentation-style.md). Its gates (`test_doc_links.py`, `test_public_copy.py`, the generated-reference checks) run in `make test-unit`.

## Pull requests

- **Title:** a sentence saying what is now true, as in `git log` (e.g. `MCP reads the bearer from the Authorization header only, never from ?token=`).
- **Body:** fill the template (`.github/PULL_REQUEST_TEMPLATE.md`): **Why** (link the issue), **Scope and risk**, **Compatibility, rollout and rollback** (ops and fields added, changed or removed, operator steps, what a revert leaves behind) and **Tests** (what you ran and what it showed). Its checklist matches what CI and repository policy enforce.
- **Measurements** that justify a change go in the description with the command that reproduces them, not in code comments or docs.
- CI must be green before merge. Squash-merge is the default: one logical change per pull request.

## Asking questions

- **Questions and design discussion:** GitHub Issues.
- **Bugs and feature requests:** GitHub Issues with the bug or feature template.
- **Security:** see [SECURITY.md](./SECURITY.md). Do not file public issues for vulnerabilities.

## Code of conduct

All interactions are governed by the [Contributor Covenant](./CODE_OF_CONDUCT.md).
