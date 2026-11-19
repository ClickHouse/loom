<!-- i18n: source=docs/development/adding-language-support.md blob=ee2dee0c86ab7bfb46f22017d1d71ab8005a2276 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-language-support.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-language-support.md), which is authoritative.

<a id="add-language-support-to-the-code-index"></a>

# 为代码索引添加语言支持

代码索引分两个层级读取代码仓库：

- **快速层级**（tree-sitter 语法和手写的扫描器）会在每次提交时对每个文件运行。它生成分块、符号和调用边。
- **编译器层级**（对已更改文件运行的 SCIP 索引器）在存在索引器的情况下，补充编译器级精确的符号和边。

快速层级为某种语言提供了处理通道，就表示支持该语言。[代码索引](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)介绍了这些层级，并列出了每种语言能获得的内容。

<a id="the-one-rule-stay-outside-the-extractor-stamp"></a>

## 唯一的规则：不要触碰 extractor stamp

快速层级的行带有生成它们的代码的版本戳（`server/code/domain/extract/extractor_stamp.py`）：

- 该戳是对 `STAGE_MODULES` 中每个模块的**代码 token**（`logic_tokens`：不含注释、空行或 docstring；import 按其绑定的名称计算，而不是包路径）以及 `STAMPED_GRAMMARS`（`tree-sitter`、`tree-sitter-cpp`、`tree-sitter-python`、`tree-sitter-bash`）中各语法已安装版本计算出的 sha256。
- 当该戳变化时，ingest worker 会分批、限速地为受影响语言族中每个已索引命名空间的每个文件重新推导发生变化的阶段（`chunk`、`symbol`、`edge`、`guard`）（见 `server/code/app/index/ingest_pass.py` 中的 `_maybe_rechunk`）。
- 在 `MODULE_LANGUAGES` 中映射到某一语言族的模块（`lang_python.py`、`ts_*` 模块）只会使该语言族的文件发生变化。共享模块（`treesitter.py`、`chunker.py`）或核心 `tree-sitter` 语法会使所有文件发生变化。
- 在大型部署上，这意味着数小时的 ingest。

请把新语言添加在不会改变任何戳的位置：

| 文件                                                           | 是否在戳中 | 放什么                                                                                                      |
| -------------------------------------------------------------- | ---------- | ----------------------------------------------------------------------------------------------------------- |
| `server/code/domain/extract/lang_<lang>.py`                    | 否         | 语言通道：`parse_<lang>(path, source) -> ParsedFile`，包含符号、include 和调用点。                          |
| `server/code/domain/extract/lang_detect.py`                    | 否         | `LANG_BY_EXTENSION`（以及 shell 规则）：哪些文件属于该语言。                                                |
| `server/code/app/index/ingest_parse.py`                        | 否         | 在 `_PARSERS` 中添加 `_parse_<lang>` 包装函数；如果该通道有 import，则在 `ingest_resolve.py` 中处理 include 解析。 |
| `server/code/domain/extract/lane_units.py`                     | 否         | 用于**没有**语法的通道的共享辅助函数：`span_chunks` 和引用绑定器 `bind_sites`。                             |
| `server/code/domain/extract/treesitter.py`                     | **是**     | 仅用于带戳的核心语言的 `lang_for_path`。在此添加扩展名会使每个命名空间重新分块。                            |
| `chunker.py`、`edges_treesitter.py` 以及其他 `STAGE_MODULES`   | **是**     | 共享的分块和边提取。只有在提取逻辑本身对所有人都发生变化时才修改它们。                                      |

`lang_for_path` 未列出的语言在 `lang_detect.py` 中检测，因此添加这类语言不会改变任何戳。带戳模块中的注释和 docstring 可以自由编辑。这些模块不参与格式化，因为格式化工具可能会改写字符串引号，而引号属于代码 token。

`server/code/domain/extract/` 下的每个模块，要么属于 `STAGE_MODULES` 的某个阶段，要么在 `tests/unit/test_code_extractor_stamp.py` 的 `NON_EXTRACTOR_MODULES` 中附带理由列出。请把新的通道模块放入 `NON_EXTRACTOR_MODULES`，否则该测试会失败。

<a id="1-choose-the-lanes-shape"></a>

## 1. 选择 lane 的形态

| 形态              | 示例                                                                                                       | 你需要提供                                                                                                                                                                  |
| ----------------- | ---------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 语法 lane         | `lang_typescript.py`, `lang_rust.py`, `lang_go.py`, `lang_java.py`                                         | 在 `pyproject.toml` 中添加 tree-sitter grammar 依赖；从语法树中提取符号和调用点；chunk 和边由共享的 chunker 和 edge walker 生成。                                            |
| 无语法树 lane     | `lang_proto.py`, `lang_gotemplate.py`, `lang_hcl.py`, `lang_k8s.py`, `lang_jsonnet.py` (`LANE_UNIT_LANGS`) | 手写的扫描器；`ParsedFile.tree` 保持为 `None`；chunk 来自 `lane_units.span_chunks`，边来自 `reference_sites` + `SITE_RULES`，并由 `lane_units.bind_sites` 绑定。            |
| 仅 include 的 lane | `tla`, `flatbuffers`, `cmake`, `doc` (`_LANE_INCLUDE_LANGS`)                                              | 由简单的 lane resolver 解析符号和 include。                                                                                                                                 |
| 嵌入式语言        | Go 和 Java 中的 SQL (`go_sql_units`, `java_sql_units`)，Dockerfile 和 Makefile 中的 shell 孤岛             | 单元并入宿主文件的解析（`_join_island_symbols`），通常带有一个开关设置，可用于关闭该功能。                                                                                  |
| 仅 chunk          | `.css`，文本文件 (`lang_text.py`)                                                                          | 只需提供类型检测：文件会被切分为 chunk 并可被搜索，但没有符号。                                                                                                             |

语法 lane 的 grammar **不**在 `STAMPED_GRAMMARS` 中，因此升级它不会改变 stamp。该 lane 自行负责重新生成它的行。

<a id="2-write-the-parser"></a>

## 2. 编写解析器

`parse_<lang>(path: str, source: bytes) -> ParsedFile` 返回语言标记、符号（`treesitter.Symbol`：限定名、种类、范围、签名）、includes，以及边阶段所需的内容。请参照最接近的现有 lane：

- **限定名** 遵循该语言的模块路径（Rust：由文件路径得出 crate 路径；`mod.rs` / `lib.rs` 以其所在目录命名）。符号 id 由限定名和规范化后的签名哈希而来，因此名称不稳定会导致每次重新索引时行都发生变动。
- **Includes** 根据命名空间中已有的路径进行解析（`wanted_paths`，然后是 lane 中的 `resolve_imports` / `resolve_uses`，由 `ingest_resolve.py` 调用）。外部包解析为空，而不是猜测一个结果。
- **调用边** 首先绑定同一文件内唯一的名称，然后以较低的置信度绑定仓库内唯一的名称，名称有歧义时则不绑定（对于没有语法树的 lane，由 `lane_units.bind_sites` 完成此项工作）。
- **容器种类**（类、impl 块）属于 lane。若在 chunker 的按语言划分的表中注册容器种类，会修改 `chunker.py` 并改变戳记。

注册 lane：

```python
# server/code/app/index/ingest_parse.py
def _parse_rust(path: str, source: bytes) -> ParsedFile:
    from server.code.domain.extract.lang_rust import parse_rust

    return parse_rust(path, source)


_PARSERS = {
    ...
    "rust": _parse_rust,
    ...
}
```

并将扩展名添加到 `LANG_BY_EXTENSION`（`".rs": "rust"`）。该包装函数以惰性方式导入 lane，因此可选的语法仍然是可选的。新的 tree-sitter 语法还需加入 `bounded_parse._LOADERS`，每次解析都会先经过它。

<a id="3-test-the-lane"></a>

## 3. 测试 lane

- **lane 测试**：`tests/unit/test_code_lang_<lang>.py`，解析小段内联源码，并断言名称、种类、span、include 和边。语法包不存在时跳过：`pytest.importorskip("tree_sitter_rust")`。`test_code_lang_rust.py` 和 `test_code_lang_typescript.py` 可作参考。
- **lanes 语料库。** 在 `tests/fixtures/code_lanes/<lang>/` 下添加 fixture：可以是合成的，也可以是遵循其自身许可证的开源片段，并列在 `tests/fixtures/code_lanes/NOTICE.md` 中。`tests/unit/test_code_lanes_corpus.py` 会从每个 fixture 中推导出语言、符号、facet、include 和边，并与 `tests/fixtures/code_lanes/expected.json` 比较。重新生成并审阅差异：

  ```bash
  python -m tests.code_lanes.corpus --write
  git diff tests/fixtures/code_lanes/expected.json
  ```

  这里发生变化的数字，会改变每个命名空间所保存的内容。请认真审阅。

- **成本防护。** 添加到 `ingest_parse._PARSERS` 的解析器，需要在 `tests/unit/lane_walks.py` 中有一个 `SAMPLES` 条目；`tests/unit/test_code_regex_cost.py` 随后会在生成的各种形态上为该 lane 计时，遍历超出线性复杂度时测试失败。
- **stamp 测试。** `tests/unit/test_code_extractor_stamp.py`，并把新模块加入 `NON_EXTRACTOR_MODULES`。
- **检索。** 如果某个 lane 应当让 `code.search` 或 `code.symbol` 给出良好的结果，请在改动前后分别运行代码智能门禁。`make bench-code-intel-dry` 离线检查问题集；`make bench-code-intel` 针对使用真实 embedder 的服务器运行，绝不要用假的 embedder（[运行基准测试](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)）。

<a id="4-bring-existing-namespaces-up-to-date"></a>

## 4. 更新现有命名空间

不在 stamp 范围内的 lane 不会改变任何已索引的内容。新的提交会使用该 lane；旧文件保留之前的行（通常只有 chunk）。通过有针对性的 backfill 重新生成它们：

```bash
git -C <checkout of the repository> ls-files '*.rs' > rust-paths.txt
loom code backfill --org <org> --namespace <code-namespace> --paths-file rust-paths.txt
```

`--paths-file` 只会在分支顶端重新摄取这些相对于仓库根目录的路径，并且不改变 watermark。请在服务器的 `.env` 和仓库镜像所在的位置运行它，例如 workers 容器（[代码索引运维](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)）。

> **警告：** 如果某项变更必须修改带 stamp 的模块（例如修复 `chunker.py`，或扩展 `treesitter.py`），请在 PR 中说明。每个采用该变更的部署，都会为每个命名空间中受影响语言族的所有文件重新生成发生变动的阶段。`chunker.py` 的变更只影响 `chunk`；`edges_treesitter.py` 的变更会影响 `edge`、`chunk` 和 `guard`；解析器的变更会影响所有阶段。

<a id="5-add-a-compiler-tier-optional"></a>

## 5. 添加编译器层级（可选）

当快速层级的边存在歧义（重载、动态分派、重新导出）且有可用的 SCIP indexer 时，编译器层级才值得添加。`server/code/domain/index/compiler_tiers.py` 中的 `TIERS` 列出了各个层级。每个 `TierSpec` 指明 indexer（`scip-clang`、`scip-typescript`、`scip-go`、`scip-python`）、其 `code_index_state` 水位线键（`scip_commit`、`scip_commit_ts`、`scip_commit_go`、`scip_commit_py`），以及推进该水位线的 worker。新增一个层级需要：

1. 一个 harness，对某个提交范围内变更的文件运行 indexer，并将其输出转换为 Loom 的 JSON delta：`scripts/scip_harness/`（`run_delta.sh`、`run_typescript_delta.sh`、`run_python_delta.sh`、`run_go_monorepo_delta.sh`、`scip_to_delta.py`）。
2. 一个 delta worker（可参考 `server/code/workers/scip_ts_delta.py`、`scip_py_delta.py` 和 `scip_go_delta.py`）：它的 `run_pass` 把自己的 `_run_repo` 交给 `server/code/app/index/compiler_tier.py` 中的 `run_tier`，由后者负责条目循环、故障隔离和全量运行时的重置。它还带有 `LOOM_CODE_SCIP_<LANG>_DELTA_ENABLED` / `_INTERVAL_SECONDS` / `_TIMEOUT_SECONDS` 设置（[添加 worker](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-a-worker.md)）。外部生产者则通过 `code.scip_ingest` 上传，由 `code_scip_apply` worker 应用完整的批次。
3. 在 `code_scip_apply` 的 `watermark_key_for` 中添加一个水位线键，并添加一行 `TierSpec`，这样 `code.index_status` 和控制台就会报告该层级。

编译器层级的行带有 `tier='scip'`。读取时，它们按限定名与快速层级合并，并优先使用编译器层级。indexer 跳过的文件，以及其 delta 尚未落地的提交，仍然从快速层级读取。

<a id="related"></a>

## 相关内容

- [代码索引概览](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)
- [代码索引架构](../architecture/code-index-architecture.md)
- [代码索引运维](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)
- [扩展 Loom](https://github.com/ClickHouse/loom/blob/main/docs/development/extending-loom.md)
