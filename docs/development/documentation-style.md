# Documentation style guide

How to place a new page in the right section, write it in the house style, and pass the documentation checks on the first push.

## Where a page goes

A page does one kind of writing. When it starts doing a second, split it and link.

| Kind              | Answers                                      | Lives in                           | Voice                                                                                                                               |
| ----------------- | -------------------------------------------- | ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| Use case          | "Does Loom do X for me?"                     | `docs/use-cases/`                  | The need answered in the first sentence; quotes a published result from `docs/benchmarks/results.md`, with a link, where one exists |
| Tutorial          | "Show me, start to finish."                  | `docs/getting-started/`            | Numbered steps that always work on the stock stack                                                                                  |
| How-to guide      | "How do I get X done?"                       | `docs/guides/`, `docs/operations/` | Goal-first steps; assumes the concepts                                                                                              |
| Concept / feature | "What is it, why does it work so?"           | `docs/concepts/`, `docs/features/` | Explanation, trade-offs, links to reference                                                                                         |
| Architecture      | "How is it built inside?"                    | `docs/architecture/`               | Components, data flow, invariants, code paths                                                                                       |
| Reference         | "What exactly are the options?"              | `docs/reference/`                  | Exhaustive, terse, preferably generated                                                                                             |
| Benchmark         | "How well does it work, and how do we know?" | `docs/benchmarks/`                 | Methodology first, numbers only with a reproduction command                                                                         |

- `docs/concepts/` explains the model a user holds in their head (identity, memory, ranking).
- `docs/features/` explains one capability from the caller's side: what it does, the operations, an example, the settings, the limits.
- `docs/architecture/` explains the same capability from the inside. A feature page links to it instead of repeating it.
- `docs/development/` is for contributors only.

## Page template

Every page starts with a title and a plain opening paragraph: what the page covers and what the reader can do after it. No labels such as **Audience:**.

```markdown
# Retrieval and ranking

`memory.search` combines several retrieval legs into one ranked answer, or abstains when
nothing is close enough. This page explains each leg, how the rank is computed, and
which settings move it.
```

The documentation site uses the first sentence of that paragraph as the page's search description and generates `llms.txt` from it. Then, where they apply: the user-visible behaviour, the interfaces (operations, CLI, settings), how it works, limits and failure modes, and "Related" links. Architecture pages add **Source entry points** (paths and symbols) and **Invariants**.

## Prerequisites

When a feature needs an operator switch or an external system, say so in the page's first sentence: "Needs `LOOM_X=true`." A use-case page is the exception: its first sentence answers the need, and the switch follows. Add no maturity or status banners: link the generated [status page](../reference/status.md), which says whether an op is stable and a switch is on by default, and the [operations reference](../reference/operations.md), which carries what the MCP listing depends on.

## Writing rules

- **The code is the source of truth.** Every setting, op, field, make target, path and CLI flag on a page exists in the tree. Check each one.
- **Short.** Short sentences, short paragraphs, tables for options. No metaphors, no marketing.
- **Present tense, current behaviour.** Write what Loom does. History, dates and the reasons for a change go in the pull request.
- **Name techniques, not products.** Repository text names no adjacent product, no person, no internal host or namespace, and cites no paper or design-decision record. `tests/unit/test_public_copy.py` enforces the lists. Compare architectures ("a separate vector store beside the relational store"), never brands.
- **No unmeasured adjectives.** "Fast", "scalable", "accurate" need a number and the command that reproduces it, or a mechanism.
- **Say why.** A design choice comes with the problem it solves and what it costs.
- **Link, don't repeat.** One page owns each explanation and each table; others link to its heading.
- **No stale counts or versions.** Don't write how many ops, settings or tables exist, or a package version. Link to the generated reference.
- **Examples run.** Code samples use real SDK, CLI and wire names and work on the stock [quickstart](../getting-started/quickstart.md) stack, unless they name the provider or setting they need.

## Terminology

Use these spellings everywhere; the [glossary](../glossary.md) defines them.

| Write                              | Not                                          |
| ---------------------------------- | -------------------------------------------- |
| operation, op (`memory.set`)       | endpoint, API call, tool (for the op)        |
| MCP tool (`loom_memory_set`)       | function                                     |
| namespace                          | project, collection, index                   |
| identity tuple                     | scope key                                    |
| plane (memory plane, code plane)   | module, service (for the product area)       |
| memory, fact (a `loom.memory` row) | note, record (a tracker record is different) |
| knowledge document                 | file, upload                                 |
| worker                             | job, cron (unless it is a cron)              |
| abstain                            | empty result                                 |
| tombstone                          | hard delete                                  |
| the dashboard                      | UI, console                                  |
| ClickHouse Cloud / ClickHouse OSS  | CH (in prose)                                |

## Formatting

- Markdown, formatted by Prettier (`make format`; CI runs `make format-check`). Python code blocks are formatted by `ruff format`, which CI also checks.
- Sentence-case headings. One `#` title per page.
- Code, op names, settings, paths and table names in backticks. Write settings as their environment variable (`LOOM_CONFLICT_THRESHOLD`); architecture pages may use the field name (`settings.conflict_threshold`).
- Source references are `path/to/file.py` plus the symbol: `server/workers/registry.py` (`_WORKERS`). No line numbers.
- Diagrams are Mermaid, only where a flow is clearer drawn than written, under ~20 nodes.
- Tables for options and comparisons; prose for reasoning.
- Relative links between docs. `tests/unit/test_doc_links.py` checks every relative link and anchor under `docs/` and the root Markdown files.

## Generated pages

These pages are generated from the code or documentation source. Edit the source, then run the generator. `make lint` runs each with `--check`, so a stale page fails CI.

| Page                                                        | Source                                                                                 | Generator                                      |
| ----------------------------------------------------------- | -------------------------------------------------------------------------------------- | ---------------------------------------------- |
| [reference/configuration.md](../reference/configuration.md) | comments above fields in `server/settings/`                                            | `python scripts/codegen/settings_reference.py` |
| [reference/operations.md](../reference/operations.md)       | `server.operations.registry.ALL_OPERATIONS`                                            | `python scripts/codegen/docs_reference.py`     |
| `llms.txt` (documentation site)                             | the docs' page titles and opening paragraphs                                           | Blume site build                               |
| `reference/operations/<group>.md` (one per group)           | the op input/output models and the comments above their fields                         | `python scripts/codegen/docs_reference.py`     |
| [reference/workers.md](../reference/workers.md)             | `server/workers/registry.py` (`_WORKERS`)                                              | `python scripts/codegen/docs_reference.py`     |
| [reference/cli.md](../reference/cli.md)                     | the `loom` parser snapshot `tests/unit/snapshots/cli_tree.json`                        | `python scripts/codegen/docs_reference.py`     |
| [reference/errors.md](../reference/errors.md)               | `server/core/errors.py`                                                                | `python scripts/codegen/docs_reference.py`     |
| [reference/status.md](../reference/status.md)               | op `maturity` and the boolean settings of `server/settings/catalog.py` that load false | `python scripts/codegen/docs_reference.py`     |
| [reference/schema.md](../reference/schema.md)               | `schemas/schema.sql`                                                                   | `python scripts/codegen/docs_reference.py`     |

On the documentation site, Blume serves each page's Markdown copy beside the page and
publishes `llms.txt` and `llms-full.txt`. `llms-install.md` remains in the repository.

The site publishes generated pages from `i18n/<language>/docs/` under `/<language>/`. English is the canonical default. Each page keeps its docs-relative path under `/<language>/`, so the homepage and use-case index have distinct routes. The English homepage links to each available translated homepage, or to its translated use-case index when the homepage is unavailable. Each translated page links to its English source and to the available equivalent translations. A notice marks translations whose source blob differs from the current English page. Translation generation stays in `scripts/i18n/translate.py`; the site build does not call a translation provider.

When an English source is removed, its stamped translations keep their routes until the producer removes the generated files. These pages show a removed-source notice instead of a link to the absent English page. Explicit English fallback links keep their repository destination because the corresponding translated page may not contain the requested anchor.

The SDK op tables (`sdk/python/loom/ops.py`, `sdk/typescript/src/generated/ops.ts`) come from `scripts/codegen/sdk_py_gen.py` and `sdk_ts_gen.py` ([code generation](code-generation.md)).

## Before you push

```bash
make format                                   # Prettier formats the Markdown, ruff its Python blocks
uv run pytest tests/unit/test_doc_links.py tests/unit/test_public_copy.py -q
python scripts/codegen/docs_reference.py --check
npm --prefix docs run build                    # builds the deployed documentation site
```
