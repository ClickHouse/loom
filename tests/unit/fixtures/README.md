# Unit-test fixtures

Structured inputs and golden outputs that a unit test reads by exact bytes.
Each file has one consuming test. A golden file is regenerated only by its
consumer's own capture command, and only for an intended change: explain
every changed entry in the commit message. Never edit a fixture to make a
failing test pass.

`tests/unit/test_unit_fixtures.py` checks this table: every file here has a
row, every row names a file that exists, the file parses as the listed JSON
shape, its consumer reads it, and it contains no home paths, e-mail addresses
or credential-shaped strings.

| Fixture                          | Consumer                            | Role                  | Shape  | Provenance and update                                                                                                                                                                                                                                                                 |
| -------------------------------- | ----------------------------------- | --------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ask_default_prompt_golden.json` | `test_ask_default_prompt_golden.py` | golden output         | list   | The `memory.ask` synthesis prompt on the default path, one entry per question shape (`name`, `system`, `user`). Rendered by the product code. Regenerate with `python -m tests.unit.test_ask_default_prompt_golden --capture` only when the default prompt changes on purpose.        |
| `loom_search_mcp_answers.json`   | `test_loom_search_mcp_render.py`    | golden output         | object | The MCP `loom_search` answer (text block and structured result) and the fan-out call it made, per scenario, for a fixed payload. Rendered by the product code. Regenerate with `python -m tests.unit.test_loom_search_mcp_render --write` only when the rendering changes on purpose. |
| `prompt_golden.json`             | `test_prompt_blocks.py`             | golden output         | list   | The strong synthesis prompt, per scenario (`name`, `system`, `user`), from before block accounting existed: accounting must not move a byte. Regenerate with `python -m tests.unit.test_prompt_blocks --capture` only when the prompt changes on purpose.                             |
| `bisect_fusion_cases.json`       | `test_bisect_fusion.py`             | held-out input cases  | list   | Three frozen regression reports from the public ClickHouse issue tracker, each with the candidate rows, paths, vector order and known culprit PR. Hand-curated, with no generator. Adding a case is a new held-out case, never a relabel of an existing one.                          |
| `durable_facts.json`             | `test_durable_facts.py`             | labelled input corpus | object | Synthetic facts shaped like a coding session's SessionEnd extraction, labelled `real` / junk-by-class. Hand-written, with no generator.                                                                                                                                               |

## The knowledge extraction corpus

`knowledge/` holds source files for `test_document_extraction.py`: SQL in two dialects and one that does not parse,
valid and invalid TOML, Markdown and HTML. They are inputs, not golden outputs: every line span, key path and
statement kind the test asserts was read off the file by hand, so a parser change that moves a locator fails the test.
The formatters (Prettier, Taplo) skip the directory, since they would rewrite the bytes the locators point into. The
table above covers the JSON files at the top level only.
