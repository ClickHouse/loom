# Unit-test snapshots

Each file here is a pinned public surface. It is written by its owning test,
and only when that test runs in its update mode. Never edit a snapshot by
hand, and never regenerate one to make a failing test pass. First decide
whether the surface change was intended. If it was, regenerate and explain
every changed contract in the commit message.

`tests/unit/test_snapshot_owners.py` checks this table: every file in this
directory has a row, every row names a file that exists, and the owning test
reads the file and honours the update switch.

| Snapshot                   | Owning test                         | What it pins                                                                                                                 | Normalization                                                                                                | Update command                                                                                         |
| -------------------------- | ----------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------ |
| `cli_tree.json`            | `test_cli_surface_snapshot.py`      | Every `loom` parser and argument: option strings, dest, action, default, choices, nargs, type, help, metavar, subcommands    | Callables as `module:qualname`; `--version` masked; `LOOM_*` unset and settings at field defaults            | `LOOM_UPDATE_CLI_SNAPSHOT=1 .venv/bin/python -m pytest tests/unit/test_cli_surface_snapshot.py`        |
| `cli_help.json`            | `test_cli_surface_snapshot.py`      | The words of every `--help` page, per blank-line-separated section                                                           | Whitespace collapsed; the pre-3.13 `-o X, --opt X` spelling folded to `-o, --opt X`; `NO_COLOR`, 100 columns | as above                                                                                               |
| `worker_registry.json`     | `test_worker_registry_snapshot.py`  | The `WorkerSpec` list `enabled_workers()` builds under the `defaults`, `all_on` and `production` profiles                    | Callables fingerprinted by name, free variables and cell contents; keys sorted                               | `LOOM_UPDATE_WORKER_SNAPSHOT=1 .venv/bin/python -m pytest tests/unit/test_worker_registry_snapshot.py` |
| `sdk_public_api.json`      | `test_sdk_surface_contract.py`      | `loom.__all__`, the constructor keywords of `Agent` / `SyncAgent` / `Datapack`, every client's public methods and parameters | Signatures rendered by `inspect`                                                                             | `LOOM_UPDATE_SNAPSHOT=1 .venv/bin/python -m pytest tests/unit/test_sdk_surface_contract.py`            |
| `live_settings.json`       | `test_live_settings.py`             | The settings that take a runtime override (marked `LIVE`)                                                                    | Sorted names                                                                                                 | `LOOM_UPDATE_SNAPSHOT=1 .venv/bin/python -m pytest tests/unit/test_live_settings.py`                   |
| `settings_defaults.json`   | `test_settings_defaults.py`         | Each non-secret setting's default as first pinned; `server/settings/_history.py` replays the changes since                   | JSON values; update adds and drops names, never rewrites a pinned default                                    | `LOOM_UPDATE_SNAPSHOT=1 .venv/bin/python -m pytest tests/unit/test_settings_defaults.py`               |
| `sdk_search_payloads.json` | `test_sdk_search_payload_golden.py` | The `memory.search` request body for each kwarg of `search()` on its own, plus the paired knobs                              | Fixed datetimes; compared as parsed JSON                                                                     | `LOOM_UPDATE_SNAPSHOT=1 .venv/bin/python -m pytest tests/unit/test_sdk_search_payload_golden.py`       |
| `mentor_planes.json`       | `test_mentor_planes.py`             | The plane names `mentor.brief` reports, each of which the dashboard maps to a page or to none                                | Sorted names                                                                                                 | `LOOM_UPDATE_MENTOR_SNAPSHOT=1 .venv/bin/python -m pytest tests/unit/test_mentor_planes.py`            |

Readers other than the owner: `test_code_compiler_tiers.py` reads the
`all_on` profile of `worker_registry.json` to check that each compiler-tier
card names workers the registry builds, and the dashboard's
`frontend/src/views/assistant/cards/planes.test.ts` reads `mentor_planes.json`
to check that each plane maps to a page or to none.

Run each update command on its own file. `LOOM_UPDATE_SNAPSHOT=1` is shared
by the two SDK snapshots, `live_settings.json` and `settings_defaults.json`, so
setting it for a whole-suite run rewrites all four, the reviewed LIVE set among them.
