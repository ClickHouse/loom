# Contributing to Loom

Loom is Apache-2.0 licensed. By submitting a contribution you agree to license it under the same terms.

The [development guide](./docs/development/repository-guide.md) goes deeper: [repository layout](./docs/development/repository-guide.md), [local development](./docs/development/local-development.md), [testing](./docs/development/testing.md), [code generation](./docs/development/code-generation.md), [migrations](./docs/development/migrations.md), [release process](./docs/development/release-process.md) and [documentation style](./docs/development/documentation-style.md).

## Before you start

- **Discuss material changes first.** Open an issue before significant work on an architectural or wire-surface change.
- **Search existing issues.** The bug may already be tracked.
- **Keep changes focused.** One concern per pull request. A refactor, a bug fix and a feature do not share a commit.

## Choose a first contribution

Start with [unassigned good first issues](https://github.com/ClickHouse/loom/issues?q=is%3Aissue%20is%3Aopen%20label%3A%22good%20first%20issue%22%20no%3Aassignee%20-label%3Ablocked%20-label%3Ahold)
or [unassigned help wanted issues](https://github.com/ClickHouse/loom/issues?q=is%3Aissue%20is%3Aopen%20label%3A%22help%20wanted%22%20no%3Aassignee%20-label%3Ablocked%20-label%3Ahold).
Read the issue's latest comments and linked pull requests before starting: someone can
be working on it without an assignee. Check [open pull requests](https://github.com/ClickHouse/loom/pulls)
for the same change. Help wanted includes larger design work; the label alone does not
make a task suitable for a beginner.

Choose one reproducible problem and a small acceptance check. If the issue has no active
claim or pull request, describe the part you intend to work on in its thread. A question,
a documentation correction and a failing example are useful contributions too; use the
[issue forms](#filing-an-issue) when you need to report a problem.

| Start from                             | Read                                                                                               | First check                                                                                    |
| -------------------------------------- | -------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| A broken instruction or link           | [Documentation style](./docs/development/documentation-style.md)                                   | Run the link check below, replacing the page parameter with the page you edited.               |
| A Python bug with a small reproduction | [Repository map](./docs/development/repository-guide.md), [testing](./docs/development/testing.md) | Run the affected test file or one test with `python -m pytest path/to/test.py::test_name -q`.  |
| A lab or course project                | [Teaching guide](./docs/guides/teaching.md), [course starter](./examples/course-starter/README.md) | Run the starter's grader against your own local stack; it fails until the TODOs are completed. |

After the Python environment in [Development setup](#development-setup), this checks one
page without starting a database, server or model provider:

```bash
python -m pytest 'tests/unit/test_doc_links.py::test_relative_links_resolve[CONTRIBUTING.md]' -q
```

A successful run reports `1 passed`. If it fails, fix the reported target or heading.
The link check verifies local targets and headings, not external sites or the accuracy of
instructions. Check the changed commands and explanations too. Unit tests use isolated
configuration and test doubles; integration tests need a private stack
([testing](./docs/development/testing.md)). Keep tokens, personal data and customer data
out of reproductions. Security reports follow [SECURITY.md](./SECURITY.md).

Finish with the [branch and checks workflow](#branch-change-check) and the
[commit sign-off](#commits-and-sign-offs-dco). In the pull request, say what reproduces the
problem, what changes and which focused checks you ran. CI runs the wider checks; read
each failed job rather than treating an unrun check as passing. After a merge, use the
same issue searches to choose a related test, example or small fix for your next contribution.
The repository's [contributor history](https://github.com/ClickHouse/loom/graphs/contributors)
and merged pull requests record contributions.

## Filing an issue

Every issue goes through a form in `.github/ISSUE_TEMPLATE/`; pick the one that fits:

| Form                             | For                                                                                                                |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| Question                         | How to do something with Loom.                                                                                     |
| Feature request                  | A workflow Loom should support, starting from the use case.                                                        |
| Bug report                       | An op, SDK call, CLI command, worker, migration or plugin hook does the wrong thing.                               |
| Recall or answer quality         | Search, ask or a context pack returned a wrong result, missed a memory, or answered when it should have abstained. |
| Performance issue                | Something is slower, or reads more from ClickHouse, than expected.                                                 |
| Upgrade or compatibility issue   | An upgrade broke a client, a migration failed, or a field or setting changed behaviour.                            |
| Installation or deployment issue | Loom does not install, build, boot or connect to ClickHouse.                                                       |
| Documentation issue              | A page is wrong, missing or unclear.                                                                               |

The forms ask which component is affected, whether the problem reproduces on the latest release or `main`, related issues and pull requests (`Caused by:` names the pull request behind a regression), and how the problem was found: AI tools are welcome, and saying whether a person reproduced the problem tells a maintainer how much to trust the reproduction. Each form asks you to confirm that tokens, keys, passwords and personal or customer data are removed.

`loom report` prints the Environment field the forms ask for: Loom, Python and platform versions and the `loom doctor` diagnostic. `--open` also opens the bug form with the versions filled in:

```bash
loom report --open
loom report | pbcopy                        # macOS
loom report | xclip -selection clipboard    # Linux
```

The diagnostic holds ClickHouse reachability and worker and embedder configuration. It records only whether `LOOM_JWT_SECRET` and `LOOM_CH_PASSWORD` are set, never their values. Security issues go through [SECURITY.md](./SECURITY.md), never a public issue.

## Development setup

The [source contribution lab](.devcontainer/source/README.md) provides a local
development container and bounded public-fork checks for a first reproduction.

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
```

Documentation checks and unit tests need only this environment. No database, running
server or provider account is needed for the focused check above. Continue below when
your change needs a running server:

```bash
# Start ClickHouse locally, with the config and lemmatizer dictionary Loom needs
# (a bare clickhouse-server container fails every memory_embeddings insert)
bash dev/ci_start_clickhouse.sh

# Apply migrations + start the control plane
make migrate
make server
# → http://localhost:7777
```

This runs the same server as the Docker image, with `--reload` and without a supervisor ([deployment](./docs/operations/deployment.md)). The dashboard returns 503 until you run `npm --prefix frontend run build`. Admin ops stay reachable at `/v1/admin/*` either way.

For Loom and ClickHouse together, use the dev compose stack:

```bash
cp .env.example .env    # once; `make dev` does this for you
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up --build    # Loom + CH only
```

`--env-file .env` makes compose read `.env` (see the note in `Makefile`). Without it you get the compose file's own fallbacks (auth on, OpenAI provider with no key), not the stack the [quickstart](./docs/getting-started/quickstart.md) describes.

## Branch, change, check

1. Branch from `main`. CI runs on pull requests into `main` or a `release/X.Y` line and on pushes to them. A pull request stacked on another branch gets no checks until it targets one of them.
2. Make the change with its tests ([testing](./docs/development/testing.md)). If it moves a source that something is generated from, regenerate it ([code generation](./docs/development/code-generation.md)). If it changes the schema, follow [migrations](./docs/development/migrations.md).
3. Run focused checks for the changed behavior first. CI runs the following wider gates; use [testing](./docs/development/testing.md) to choose the relevant local checks:

```bash
make format        # ruff, Biome, Prettier, Taplo, shfmt (CI: make format-check)
make lint          # ruff, the function-length and secret gates, generated files current, Biome's dashboard accessibility rules
make typecheck     # mypy --strict on the SDK + the server's mypy ratchet
make test-unit     # pytest tests/unit and pytest bench (no ClickHouse needed)

# Frontend
npm --prefix frontend ci && npm --prefix frontend run test && npm --prefix frontend run build

# Integration suite (needs a running server and ClickHouse)
LOOM_BASE_URL=http://localhost:7777 \
LOOM_API_KEY=$(cat ~/.loom/token 2>/dev/null) \
  pytest tests/integration -q
```

Unit tests run in random order on every core in CI (`pytest -n auto -p randomly`), so a test must pass after any other and beside any other ([`tests/README.md`](./tests/README.md#rules-every-test-keeps)). Tests skip cleanly when their env config is absent: use `os.environ.get(...) or pytest.skip(...)` in new fixtures. Two ratchets only shrink: `scripts/gates/function_length_gate.py` (no file gains an oversized function) and `scripts/gates/mypy_gate.py` (no new type errors). A failing gate is fixed at the source (regenerate the file, write the test), not by loosening the gate. [`tests/README.md`](./tests/README.md) covers the suite's layers, tiers, isolation guards and skip policy.

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

- **Settings** are all declared in `server/settings/catalog.py`, one section per area; a field is `LOOM_<FIELD>` in the environment. Give a new field a comment saying what it controls and what its values mean, read it as `settings.<field>` (never `getattr` with a default), and run `python scripts/codegen/settings_reference.py` to regenerate [the configuration reference](./docs/reference/configuration.md).
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
4. Regenerate the SDK op tables and the Python per-op client: `python scripts/codegen/sdk_py_gen.py`, `python scripts/codegen/sdk_py_client_gen.py`, `python scripts/codegen/sdk_ts_gen.py` and `python scripts/codegen/sdk_rb_gen.py` (each has a `--check`). Add a method to the hand-written client in `sdk/python/loom/<group>.py` when the op belongs in the high-level API.
5. Add unit tests, and an integration test under `tests/integration/` for a new read or write path.

The HTTP and MCP transports discover registered operations; you don't touch transport code.

An operation or field may be renamed or removed. Say so in the PR description and tell the consumers that call it. A client that still sends a removed field is served, with the field named in `ignored_args` ([HTTP API](./docs/reference/http-api.md#unknown-and-removed-fields)). Retiring a flag deletes the setting, its branch and any per-request field that carried it, in one change.

## Translations

The translation inventory includes every tracked `README.md` and `docs/**/*.md`, including generated references. `i18n/<lang>/` holds the available machine translations. Edit English or its generator; never edit translations by hand. Inventory discovery includes nested additions and renames and excludes untracked or deleted files.

English is canonical and the default. Simplified Chinese (`zh-CN`), Japanese (`ja`), Korean (`ko`), Brazilian Portuguese (`pt-BR`) and Spanish (`es`) are the five active translation locales for default generation and localized website rollout. An additional language requires evidence that its relevant developer audience expects local-language technical documentation. Keep English for an audience that expects English technical documentation. Country, native language and developer population do not establish a translation need. This evidence requirement governs future additions.

With an allowance and the restricted runner configured, translation follows an English merge to main. It is asynchronous: the English PR can merge while coverage is pending. The workflow queues a bounded batch after relevant main pushes and every six hours. Changed existing translations precede missing pages. Relative links use an available translated target and anchor, otherwise the English page. A verified replacement that would break surviving links waits in `i18n/.pending/<lang>/`; later batches reuse it. Obsolete pages are removed only after surviving inbound links are repaired, and only with a matching generator stamp, language and source path. Unknown files are refused. A source deletion that breaks a published English fallback must include the generated link repair: run `LOOM_I18N_MAX_USD=0 python -m scripts.i18n.translate update` to repair links without provider calls. Repair uses a published translation only when its requested anchor exists; otherwise validation blocks the deletion until the incoming page is repaired.

`python -m scripts.i18n.translate check` reports every missing, stale and obsolete page across all five translation locales. `check --skip-stale` permits pending coverage but still checks present stamps, banners, links, anchors and translated labels, plus the content invariants of current output. Ordinary PR tests follow this asynchronous contract. A changed prompt requires an explicit `update --all`; a generated refresh receipt tracks that request across bounded scheduled batches and pending-branch reuse. Completed source/output proofs prevent repeated calls; the receipt stays until main contains those results. A concurrent main translation is preserved when it differs from the refresh's recorded base output. Model and banner changes invalidate stamps automatically.

The updater imports only validated generated blobs from `i18n/update`, preserving fresh main output. Owned output outside a selected batch can remain as hidden checkpoint data after its English source changes or disappears. Such archives never become publication routes; activation still requires current source, model and content validation. A conflicting existing checkpoint refuses replacement so both versions remain recoverable. It executes no code from that branch. It dispatches hosted CI for the exact bot commit because PR events created with `GITHUB_TOKEN` do not start CI. A hosted finalizer checks for successful CI on a 15-minute schedule. Finalization checks and translation batches wait in the same queue and run one at a time. It checks the exact commit, generated diff, current main parent, current language approval and repository review requirements before merging. Required human reviews remain required. After merge it explicitly dispatches the docs-site workflow; publication also requires the site's deployment configuration. A newer main commit defers merging until the producer composes another batch.

The script calls Claude Sonnet 5.5 on Amazon Bedrock through `AnthropicBedrock`. Local runs also spend money and require separate authorization. For an authorized run:

```bash
python -m venv .venv-i18n && . .venv-i18n/bin/activate
python -m pip install --require-hashes --no-deps --only-binary=:all: -r scripts/i18n/requirements.txt
aws sso login
export AWS_REGION=<region>
python -m scripts.i18n.translate update
```

One batch shares an approximate USD 5 usage cap across every language. Before any workflow provider call, the updater books a conservative allowance covering every selected request, verifier retry, API retry and maximum output in the append-only `i18n/budget` branch. These reservations can greatly exceed the expected bill and are never refunded automatically, including after failures. `LOOM_I18N_TOTAL_USD` is the cumulative reservation ceiling, defaults to zero, and does not reset between runs or languages. An exhausted allowance defers work. A missing or invalid ledger refuses paid work. `LOOM_I18N_LANGUAGES` optionally restricts a batch to a space-separated subset of the five active translation locales. Unset or empty selects all five, as does the local update command by default. Other locale codes are refused. Manual language requests must stay within the configured subset and govern pending import, generation and publication together. Published output and checkpoints outside a selected batch stay intact. Automated maintenance changes only selected locales; present-link validation still covers retained files. A first translated README waits if publishing it would require changing an excluded locale or repairing a stale or conflicted README. Other selected pages continue. When a pending batch changes the English README’s generated language bar, importing it requires all affected locales. A partial run stops before generation or publication and retains the pending batch. An English deletion that breaks an unselected file requires a coordinated repair before it can pass validation. Raising the ceiling authorizes additional spending; a schedule cannot raise it.

The workflow stores no AWS credential in GitHub. Only main push, schedule and manual events may reach the role-bearing runner. Hosted CI and completion events cannot reach it. To activate authorized translation:

1. Create environment `i18n` with deployment branches limited to `main`.
2. Configure a runner group for this repository with public-repository access and selected-workflow access limited to `ClickHouse/loom/.github/workflows/i18n.yml@refs/heads/main`. Its image needs a supported OS, `git`, `bash` and `gh`.
3. Give its instance role the policy below and accept the model's Bedrock access once. The role has no Marketplace subscription permissions.
4. Initialize the ledger once with an authorized repository writer: `python -m scripts.i18n.automation init-budget`. Preserve this branch; never recreate it to reset usage.
5. Leave `LOOM_I18N_LANGUAGES` unset for all five active locales, or set an explicit subset for a batch. Set `LOOM_I18N_TOTAL_USD` to an explicitly approved cumulative allowance and `LOOM_I18N_RUNNER` to the registered runner labels as a JSON list. Empty runner labels skip the updater; a zero allowance permits only free maintenance.
6. Allow Actions to create pull requests and retain branch protections and review requirements. Set the site's deployment configuration as described in the docs-site workflow. Hosted validation and merge need repository contents, pull-request and Actions write permissions; the instance role still grants only the model invocation.

The job calls the model's global inference profile from the runner's `AWS_REGION`, else its `AWS_DEFAULT_REGION`, else `us-east-1`. Bedrock authorizes a call through a global profile on three resources: the profile, the model in the calling region, and the model with no region. The policy names each; `<region>` is the calling region:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "bedrock:InvokeModel",
      "Resource": "arn:aws:bedrock:<region>:<account-id>:inference-profile/global.anthropic.claude-sonnet-5-5"
    },
    {
      "Effect": "Allow",
      "Action": "bedrock:InvokeModel",
      "Resource": [
        "arn:aws:bedrock:<region>::foundation-model/anthropic.claude-sonnet-5-5",
        "arn:aws:bedrock:::foundation-model/anthropic.claude-sonnet-5-5"
      ],
      "Condition": {
        "StringEquals": {
          "bedrock:InferenceProfileArn": "arn:aws:bedrock:<region>:<account-id>:inference-profile/global.anthropic.claude-sonnet-5-5"
        }
      }
    }
  ]
}
```

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
- **Body:** fill the template (`.github/PULL_REQUEST_TEMPLATE.md`). It opens with the linked issues (`Closes:` / `Related:`, full URLs) and a **changelog category** with a one- or two-sentence **changelog entry** a user can read without the diff; the release pull request collects those entries into its release notes. Then **Why**, **Scope and risk**, **Compatibility, rollout and rollback** (ops and fields added, changed or removed, operator steps, what a revert leaves behind), **Tests** (what you ran and what it showed) and how AI tools were used. Its checklist matches what CI and repository policy enforce.
- **Measurements** that justify a change go in the description with the command that reproduces them, not in code comments or docs.
- CI must be green before merge. Squash-merge is the default: one logical change per pull request.

## Working with a coding agent

Agents are welcome, and held to the same bar as a person: whoever submits a pull request, issue or comment answers for it. [`AGENTS.md`](./AGENTS.md) is the instruction file Claude Code, Codex and Cursor read, and `.claude/skills/` holds the procedures they follow: setting up a worktree, debugging, the gates before a push, shipping and reviewing a pull request, answering review comments, filing an issue, and writing in plain technical English. A person can follow the same files by hand.

## Asking questions

- **Questions and design discussion:** GitHub Issues, with the question or feature request form.
- **Bugs:** GitHub Issues, with the form that fits (see [Filing an issue](#filing-an-issue)).
- **Security:** see [SECURITY.md](./SECURITY.md). Do not file public issues for vulnerabilities.

## Code of conduct

All interactions are governed by the [Contributor Covenant](./CODE_OF_CONDUCT.md).
