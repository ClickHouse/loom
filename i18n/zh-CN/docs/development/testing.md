<!-- i18n: source=docs/development/testing.md blob=abc5f046c927795cb7b9c4d6a81dc5bc34b9cc85 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/development/testing.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/development/testing.md), which is authoritative.

<a id="testing"></a>

# 测试

某个测试属于哪个测试套件，每个套件运行需要什么，环境缺失时测试如何跳过，CI 强制执行哪些检查，以及如何重新生成测试所固定的内容。

<a id="the-suites"></a>

## 测试套件

| 套件                                            | 路径                                                                 | 依赖                                                                          | 本地运行                                                       | CI 中                                                          |
| ----------------------------------------------- | -------------------------------------------------------------------- | ----------------------------------------------------------------------------- | -------------------------------------------------------------- | -------------------------------------------------------------- |
| 单元测试                                        | `tests/unit/`                                                        | 无外部依赖；不需要 ClickHouse                                                 | `make test-unit`（同时运行 `pytest bench -q`）                 | `unit` 分段随机顺序运行；`unit-coverage`，覆盖率 ≥ 40%         |
| 基准测试工具                                    | `bench/`（其中的 `test_*` 文件）                                     | 无外部依赖                                                                    | `pytest bench -q`                                              | `unit-coverage`                                                |
| 集成测试                                        | `tests/integration/`                                                 | 已完成迁移的 ClickHouse 和正在运行的服务器                                    | `make test-integration`，并设置 `LOOM_BASE_URL`                | `integration`                                                  |
| 分析器语料库                                    | `tests/analyzer/`                                                    | 已完成迁移的 ClickHouse（不需要服务器）                                       | `pytest tests/analyzer -q`                                     | `integration`                                                  |
| 代码通道语料库                                  | `tests/code_lanes/`，由 `tests/unit/test_code_lanes_corpus.py` 把关  | 无外部依赖                                                                    | `python -m tests.code_lanes.corpus`                            | `unit`                                                         |
| 控制台                                          | `frontend/src/**/*.test.tsx`                                         | Node                                                                          | `npm --prefix frontend run test`（vitest，含 axe）             | `frontend-vitest`                                              |
| 控制台端到端测试                                | `frontend/e2e/*.spec.ts`                                             | Node、Chromium（`npx playwright install chromium`）                           | `npm --prefix frontend run e2e`                                | `frontend-e2e`                                                 |
| 插件钩子                                        | `integrations/*/tests/*.test.mjs`                                    | Node                                                                          | `node --test integrations/claude-code-plugin/tests/*.test.mjs` | `agent-plugins`                                                |
| 框架作为 extra 的 Python SDK 适配器             | `sdk/python/tests/`                                                  | `uv`；在 `.venv-sdk-adapters/` 下为每个适配器 extra 安装独立的 venv           | `make test-sdk-adapters`                                       | `sdk-python-adapters`                                          |
| TypeScript SDK                                  | `sdk/typescript/`                                                    | Node                                                                          | `npm --prefix sdk/typescript run check`                        | `sdk-typescript`                                               |
| 基准测试与质量门槛                              | `bench/*.py`、`make bench-*`                                         | 使用**真实**提供商的服务器                                                    | [运行基准测试](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)                 | 从不运行（CI 的 embedder 是假的）                              |

**基准测试为何不放进 CI。** CI 使用 `LOOM_EMBEDDING_MODEL=fake-ngram-1024` 和 `LOOM_LLM_PROVIDER=fake` 运行，因此其中的召回率数字无法反映真实检索的表现，而基于假 embedding 的质量门槛即使通过，也比没有门槛更糟。若要在 CI 中运行，就需要在仓库中存放付费提供商的密钥。这些门槛（`make bench-quick`、`bench-quality-gate`、`bench-code-intel`）由人工针对使用真实提供商的部署来运行，时机是在修改提取提示词、默认模型或 embedder 之前。

<a id="unit-tests"></a>

## 单元测试

- **不使用 ClickHouse。** 单元测试使用桩（stub）替代客户端；任何需要真实 SQL 语义的测试都应放在集成测试或分析器测试套件中。
- **随机顺序，并行运行。** CI 使用 `-n auto -p randomly` 运行测试套件，每个核心一个工作进程，并首先打印随机种子，因此每个测试必须在任何其他测试之后通过，也必须能与任何其他测试同时通过。[`tests/README.md`](https://github.com/ClickHouse/loom/blob/main/tests/README.md#rules-every-test-keeps) 说明了这些规则，以及如何根据种子和日志复现失败。
- **异步测试使用 `await asyncio.sleep(...)` 来休眠**，绝不使用 `time.sleep`。
- **挂钟日期。** 如果夹具中使用固定日期，而过期检查会将其与 `now()` 比较，那么在该日期超过阈值之前，测试都会通过。应通过代码中的时间接缝（time seam）固定时钟，而不是硬编码一个会被检查拿去与挂钟时间比较的日期。

<a id="integration-tests"></a>

## 集成测试

集成测试套件针对运行中的服务器执行，方式与 CI 的 `integration` 作业相同：

```bash
bash dev/ci_start_clickhouse.sh                      # or the dev stack's ClickHouse
make migrate
LOOM_INSTANCE_LABEL=dev:$USER:tests LOOM_BACKGROUND_WORKERS=off make server &
LOOM_BASE_URL=http://localhost:7777 make test-integration
```

- `tests/conftest.py` 会把仓库的 `.env` 加载到测试进程中（只填充缺失的变量），因此测试和服务器会选用相同的 provider：用某个 embedder 写入的行，在使用另一个 embedder 读取时不可见。
- 除非你已导出 `LOOM_API_KEY`，否则每个测试都会在其中得到一个通配 org 的 writer 令牌（`agent` fixture 使用它），而 `mint_test_token` fixture 会为测试自己的 org 签发令牌。两者都使用测试进程的 `LOOM_JWT_SECRET` 签名，该值必须与服务器的一致。
- 运行服务器时设置 `LOOM_BACKGROUND_WORKERS=off`：需要测试 worker 的用例会在进程内针对自己的命名空间调用其 `run_pass`，而运行中的 supervisor 所管理的 worker 没有命名空间过滤，会把这些测试的行消耗掉。对于一次 pass 只取走所有租户共享队列中的一项的 worker，例如 `knowledge_ingest`，测试通过按 pass 的方式运行自己的那一项来验证（`ingest_run.take_and_run`）。
- `tests/integration/test_document_object_store_live.py` 还需要在 `LOOM_TEST_S3_ENDPOINT` 中提供一个兼容 S3 的存储（CI 会运行一个本地存储，参见 `.github/workflows/ci.yml`），没有则跳过。
- `tests/integration/test_knowledge_ingest_live.py` 中的发布测试会运行各自的作业，作业会把原始文件发送给文档提取器：需要有一个已路由 SQL 和 TOML 的提取器（`LOOM_DOCUMENT_EXTRACTOR_URL` 或 `LOOM_DOCUMENT_EXTRACTORS_JSON`，并附带其令牌；参见[配置参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md)），没有则跳过。`tests/integration/test_knowledge_inspect_ingest.py` 还会通过 `knowledge.inspect` 断言服务器已配置一个提取器，因此请为服务器提供相同的设置。CI 会在每个部分的服务器之前启动参考提取器（`.github/workflows/ci.yml`；[运行方法](https://github.com/ClickHouse/loom/blob/main/extractor/README.md)）。
- `tests/integration/test_pg_authority.py` 需要 Postgres authority 的服务器：用 `dev/ci_start_postgres.sh` 启动 Postgres，运行 `make migrate`，并为服务器和 pytest 都设置 `LOOM_STORAGE_AUTHORITY=postgres` 和 `LOOM_PG_DSN=postgres://loom:loomdev@localhost:5432/loom`（该脚本的数据库；`LOOM_PG_HOST_PORT` 可更改端口），因为其测试也会在测试进程中运行 authority 的代码。以 `LOOM_PG_STANDBY=0`（没有同步 standby）启动的 Postgres，也需要在 pytest 的环境中设置 `LOOM_PG_STANDBY=0`，这样 standby 健康检查测试会被跳过。对于 ClickHouse authority 的服务器，除了仅使用 API 的测试外，其余测试都会跳过。
- `tests/integration/test_pg_clickhouse_live_ch.py` 通过带有 `pg_clickhouse` 的 Postgres 读取 [SQL 接口](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)：用 `dev/ci_start_pg_clickhouse.sh` 启动一个（使用 host 网络，端口 5433，因此需要 Linux Docker 主机：容器会访问发布在 127.0.0.1 上的 ClickHouse；`LOOM_PGCH_POSTGRES` 和 `LOOM_PGCH_VERSION` 用于选择镜像），并在该 ClickHouse 的 `LOOM_CH_*` 旁边设置 `LOOM_PGCH_DSN=postgres://app:appdev@127.0.0.1:5433/app`。没有这些设置则跳过。之后请删除该容器（`docker rm -f loom-pgch`，或你的 `LOOM_PGCH_CONTAINER`）。
- CI 设置了 `LOOM_LATENCY_BUDGET_MULTIPLIER=10`：共享 runner 的噪声很大，默认的每个 op 预算会触发违规。
- 依赖自助注册的测试在 CI 中会跳过，因为 CI 没有 `LOOM_SELF_SIGNUP=open`；请优先使用签发的令牌。
- **层级。** `tests/integration/conftest.py` 注册了四个标记，本地快速运行时可以将其排除（`-m "not slow and not destructive"`）；CI 会分部分运行整个目录（见下文）。`slow` 会有意消耗实际耗时（租约失效、重试耗尽），`destructive` 会停止合并、运行 `OPTIMIZE` 或删除整个范围，`provider` 需要真实的 embedder 或 LLM，在使用模拟实现时会跳过，`recovery` 会触发故障并验证恢复路径。
- **部分。** `LOOM_TEST_SHARD=k/n` 只收集 n 个部分中第 k 部分的测试文件。每个部分包含完整的文件，因此模块 fixture 只会在一个部分中构建。文件按 `tests/shard_weights.json` 记录的各自秒数分配；该文件未列出的文件，则按其测试函数数量乘以记录的每个测试函数秒数来估算，没有记录时每个按一秒，所有集成文件都是如此（`tests/_shards.py`）。CI 把测试套件分成四个部分，每个部分使用各自的栈，另有隔离门禁部分；第 k 部分的失败可以用 `LOOM_TEST_SHARD=k/4 make test-integration` 复现。
- **失败诊断。** 失败测试的报告会附带其最近 25 个 Loom 请求（方法、URL、状态、`X-Request-Id`、延迟），其中 JWT、bearer 令牌和密钥已被脱敏，因此可以按 request id 找到对应的服务器日志行。
- **清理。** `unique_org(label)`（`tests/integration/_resources.py`）会生成 `it-<label>-<8 hex>`。会话结束时，每个已生成 org 的所有行都会从每个带有 `org` 列的表中删除，并统计残留；存在残留则本次运行失败。`LOOM_IT_KEEP_ROWS=1` 会保留这些行以便调试。

<a id="framework-adapters"></a>

## 框架适配器

框架属于 SDK extra 的 SDK 适配器不会把其框架放进 `uv.lock`，因此 `make test-sdk-adapters` 会把每个框架安装到 `.venv-sdk-adapters/` 下各自独立的 venv 中（两个框架可能对同一个包有互不兼容的版本限制），用于运行它们的单元测试（`sdk/python/tests/test_<module>.py`）；`make test-sdk-adapter-<extra>` 则只运行其中一个。它们的集成测试（`tests/integration/test_sdk_<framework>_*.py`）在没有该框架时会被跳过；要运行这些测试，请把该 extra 的依赖安装到 `.venv` 中（`uv pip install --python .venv/bin/python -r <(python scripts/gates/sdk_extra_requirements.py langchain)`），然后按照[集成测试](#integration-tests)中的方式针对服务器运行。`uv sync` 会再次移除该框架。

<a id="how-tests-skip"></a>

## 测试如何跳过

环境缺失的测试会带着原因跳过，绝不会因为缺少变量而失败。使用 `os.environ.get(...) or pytest.skip(...)`，不要使用 `os.environ[...]`。共享 fixture 遵循这一规则：没有 `LOOM_BASE_URL` 时，`control_plane_url` 会跳过。如果已设置 `LOOM_BASE_URL`，但 `/v1/health` 没有返回 200，说明服务器已损坏，而不是缺失，此时测试会失败，并给出该 URL 和错误信息。`require_real_embedder` 会在运行 fake embedder 的服务器上跳过搜索质量测试。analyzer 测试套件（`migrated_ch`）在以下情况下会跳过，并在原因中给出地址和修复方法：`LOOM_CH_*` 指向的地址上没有 ClickHouse 应答，或者目标库的迁移落后。在 `CI` 下则会失败，因为该作业会先启动一个 ClickHouse 并运行迁移。

<a id="the-analyzer-statement-corpus"></a>

## 分析器语句语料库

Loom 的 ClickHouse 语句是由众多构建器在各种标志下生成的 Python 字符串。单元测试桩接受任何字符串，因此，遮蔽了已过滤列的别名、没有 `WITH` 就被读取的 CTE、在子查询中使用的外层别名，或驱动无法绑定的参数类型，都会到达服务器，并以失败后放行的分支出现：表现为缺失行，而不是报错。`tests/analyzer/` 会遍历每个构建器的所有取值组合，按形状对语句去重，并针对已迁移的空表对每条语句运行 `EXPLAIN QUERY TREE`，这些问题都会在几毫秒内被拒绝。`shapes.txt` 是提交到仓库的评审依据：修改了某条语句形状的 pull request 会在 diff 中显示新的头部。

```bash
pytest tests/analyzer -q                              # what CI runs
python -m tests.analyzer.corpus --write               # regenerate shapes.txt (review the diff)
python -m tests.analyzer.corpus --case erasure --errors
```

[分析器 README](https://github.com/ClickHouse/loom/blob/main/tests/analyzer/README.md)介绍了用例格式、对已迁移目标的要求，以及如何导入通过 `LOOM_CH_CAPTURE_STATEMENTS` 捕获的生产环境语句形状。

<a id="ci-gates"></a>

## CI 关卡

大多数关卡是单元测试，因此 `make test-unit` 会运行它们；其余的是 `make lint` 的步骤。每个关卡都说明了失败时应运行什么。

| 关卡                            | 位置                                                                                  | 失败条件                                                                                                                                                                                     |
| ------------------------------- | ------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 公开文案                        | `tests/unit/test_public_copy.py`                                                      | 代码树中出现相邻产品、人名、内部主机或命名空间，引用了论文或设计记录；公开页面提到竞争对手                                                                                                      |
| 文档链接                        | `tests/unit/test_doc_links.py`                                                        | 根目录 Markdown、`docs/` 或集成 README 中的相对链接或 `#anchor` 无法解析                                                                                                                         |
| 设置参考                        | `tests/unit/test_settings_reference.py`, `settings_reference.py --check`              | 生成的配置参考与设置源不一致，或某个字段没有对应的行                                                                                                                                             |
| 文档参考                        | `tests/unit/test_docs_reference.py`, `docs_reference.py --check` (`make lint`)        | `docs/reference/` 下生成的区域已过期，或某个 op 或渲染器没有对应的行                                                                                                                             |
| 默认 env 与文档一致             | `tests/unit/test_stock_env_matches_docs.py`                                           | 快速入门中所述的默认 `.env` 值与 `.env.example` 不一致，或文档中的 compose 命令缺少 `--env-file .env`                                                                                            |
| 函数长度                        | `scripts/gates/function_length_gate.py` (lint)                                        | 某个文件新增了过长的函数，或已列出的函数增长超过其基线                                                                                                                                           |
| mypy 棘轮                       | `scripts/gates/mypy_gate.py` (`make typecheck`)                                       | 任一文件的 strict-mypy 错误数多于 `scripts/gates/mypy_baseline.json`                                                                                                                             |
| 密钥                            | `scripts/gates/check_no_secrets.py` (lint), `tests/unit/test_no_secrets_guard.py`     | 受版本控制的文件中含有形似凭据的值                                                                                                                                                               |
| 控制台无障碍                    | `biome lint frontend/src` (`make lint`, CI job `format`)                              | 控制台的某个 `.tsx` 新增了无障碍违规                                                                                                                                                             |
| 跨主体泄漏                      | `tests/integration/test_cross_principal_leakage.py`                                   | 任何读取 op 在以另一个租户身份、使用 canary nonce 调用时返回了 nonce 或 500；载荷由每个 op 的输入模型合成，因此新的读取 op 在合入当天就会被探测                                                 |
| Worker 注册表快照               | `tests/unit/test_worker_registry_snapshot.py`, `snapshots/worker_registry.json`       | 在 `defaults`、`all_on` 或 `production` 配置下，启用的 worker 集合、顺序、运行频率或截止时间发生变化                                                                                              |
| Worker 注册表一致性             | `tests/unit/test_worker_registry_parity.py`                                           | `/v1/admin/workers.status` 列出的 worker 与 supervisor 运行的 worker 不一致                                                                                                                      |
| 迁移仅增量                      | `tests/unit/test_migrations_additive.py`                                              | 某个迁移删除或重命名了表或列，或在已声明的重建之外交换了表                                                                                                                                       |
| 迁移编号                        | `tests/unit/test_migration_numeric_order.py`                                          | 两个迁移文件使用了相同的编号                                                                                                                                                                     |
| Schema 检查                     | `schema-check` job：先做严格的试运行，然后运行 `make schema-check`                    | 账本中包含仓库里没有的文件，或 `schemas/schema.sql` 与回放全部迁移的结果不一致                                                                                                                   |
| CLI 接口快照                    | `tests/unit/test_cli_surface_snapshot.py`, `snapshots/cli_tree.json`, `cli_help.json` | 任何 `loom` 解析器、参数、默认值或 `--help` 文本发生变化                                                                                                                                         |
| Op 可达性统计                   | `tests/unit/test_op_reach_census.py`, `op_reach_census.py --check`                    | 某个 op 除原始 HTTP 外没有其他可达路径且不是实验性的，某个 op 既没有控制台调用点也没有 `server/operations/dashboard.py` 中的条目，或 `reach_manifest.py` 已过期                                    |
| Op 接口面                       | `tests/unit/test_op_surfaces.py`                                                      | 某个 op 的 MCP 路由、对隐藏 op 的拒绝，或其在 `server/operations/dashboard.py` 中的条目与其可达性不一致                                                                                          |
| MCP 列表预算                    | `tests/unit/test_mcp_listing_budget.py`                                               | 默认的 `tools/list` 增长超过其 token 上限，或让模型指向它无法调用的工具                                                                                                                          |
| 设置访问                        | `tests/unit/test_settings_attribute_access.py`                                        | 代码使用 `getattr(settings, "<field>", default)` 读取设置                                                                                                                                        |
| 生成的 SDK 和插件副本           | `sdk_py_gen.py`, `sdk_ts_gen.py`, `agent_plugin_sync.py` `--check` (lint)             | 生成的文件与其源文件不一致（[代码生成](https://github.com/ClickHouse/loom/blob/main/docs/development/code-generation.md)）                                                                       |
| 格式化                          | `format` job，以及 `lint` 中的 `ruff format --check`（本地用 `make format-check`）    | ruff、Biome、Prettier、Taplo 或 shfmt 会修改某个文件                                                                                                                                             |

要为有意的变更重新生成快照：

```bash
LOOM_UPDATE_CLI_SNAPSHOT=1 python -m pytest tests/unit/test_cli_surface_snapshot.py
LOOM_UPDATE_WORKER_SNAPSHOT=1 python -m pytest tests/unit/test_worker_registry_snapshot.py
python scripts/gates/function_length_gate.py --update   # only ever lowers the baselines
python scripts/gates/mypy_gate.py --update
```

棘轮基线只降不升：`--update` 只会写入更低的计数，因此债务不能在文件之间转移。

<a id="the-ci-workflow"></a>

## CI 工作流

`.github/workflows/ci.yml` 在每个目标为 `main` 或 `release/X.Y` 分支线的拉取请求上运行，也在推送到这两者时运行，使用 Python 3.12（`sdk-python-package` 会增加其他版本）和 Node 22。对拉取请求的较新推送会取消该拉取请求正在运行的工作流；每次推送到 `main` 或发布分支线，都在各自的并发组中运行至结束，因此某个合并提交上的失败不会被下一次提交掩盖。每个 action 都固定到完整的提交 SHA（其版本写在注释中），工作流只授予各作业对仓库的只读访问权限（`permissions: contents: read`）。

| 作业                   | 运行内容                                                                                                                                                                                                                                                                                                                                                                                     |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `clickhouse`           | 解析 `dev/clickhouse_release.sh` 指定的 ClickHouse 版本，使 `unit`、`schema-check` 和 `integration` 测试同一个版本                                                                                                                                                                                                                                                                           |
| `lint`                 | `ruff check`、`ruff format --check`，对 `sdk_py_gen.py`、`sdk_py_client_gen.py`、`sdk_py_admin_gen.py`、`agent_plugin_sync.py`、`settings_reference.py`、`settings_schema.py`、`docs_reference.py` 和 `op_reach_census.py` 的 `--check`，对 SDK 的 `mypy --strict`，mypy 棘轮，函数长度和密钥检查门禁                                                                                          |
| `unit`                 | 将 `pytest tests/unit` 分成六部分（`LOOM_TEST_SHARD`），每部分在各自的运行器上，以随机顺序在所有核心上运行并统计覆盖率                                                                                                                                                                                                                                                                      |
| `unit-coverage`        | 合并各部分的覆盖率（下限 40%）并检查各模块的下限，然后运行 `pytest bench` 和通用集成示例（`integrations/generic/test.sh`）；其 `shard-weights` 制品是本次运行中每个单元测试文件的耗时（秒），当各部分耗时相差过大时，将其提交为 `tests/shard_weights.json`                                                                                                                                  |
| `sandbox`              | 在真实的 bubblewrap 下运行恶意仓库作业沙箱测试套件，跳过即视为失败（`LOOM_TEST_REQUIRE_SANDBOX=1`）                                                                                                                                                                                                                                                                                           |
| `schema-check`         | 全新的 ClickHouse（`dev/ci_start_clickhouse.sh`）、所有迁移、严格的试运行、`make schema-check`                                                                                                                                                                                                                                                                                                |
| `ts-typecheck`         | 控制台构建（`tsc -b` + Vite）及其包体积预算（`npm run budget`）                                                                                                                                                                                                                                                                                                                              |
| `format`               | 使用固定版本的 Biome、Prettier、Taplo 和 shfmt 检查，以及 Biome 对 `frontend/src` 的无障碍 lint                                                                                                                                                                                                                                                                                              |
| `helm`                 | 对每个 overlay 运行 `helm lint --strict` 和 kubeconform，对 `deploy/okf-sync/cronjob.yaml` 运行 kubeconform，检查 chart 必须拒绝的取值，以及 `scripts/gates/helm_extractor_isolation.py`、`helm_metadata_egress.py` 和 `helm_assistant_isolation.py`                                                                                                                                           |
| `agent-plugins`        | 对插件 hook 和 grep shim 运行 `node --test`，Cursor hook 示例（`integrations/cursor/test.sh`），对两个插件及 shim 的每个 hook、库和 `bin/` 辅助脚本做解析检查，并检查 grep shim 的入口点是否可执行                                                                                                                                                                                           |
| `frontend-vitest`      | vitest（单元、组件、axe），在不同运行器上分部分运行                                                                                                                                                                                                                                                                                                                                          |
| `frontend-e2e`         | Playwright，在不同运行器上分部分运行                                                                                                                                                                                                                                                                                                                                                         |
| `integration`          | 分部分运行，每部分都有自己的 ClickHouse、迁移、参考提取器和已启动的服务器（假提供方，worker 关闭）：编号部分拆分 `pytest tests/integration`（`LOOM_TEST_SHARD`），隔离门禁部分包含提供方暂存测试、开启认证的门禁和 `pytest tests/analyzer`                                                                                                                                                    |
| `integration-postgres` | 针对 `LOOM_STORAGE_AUTHORITY=postgres` 的服务器，在带同步备库的 Postgres 16 和 18 上运行记忆、知识、隔离、幂等、擦除和恢复 ID 测试套件以及开启认证的门禁（[Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md)）                                                                                                                                              |
| `mixed-version`        | `scripts/gates/mixed_version.py`：在同一个 ClickHouse 上并排构建 N-1 和 N，N-1 为 `HEAD~1`：对拉取请求，是其合并提交的第一个父提交，即合并时它所指向的分支；对推送，是上一个提交                                                                                                                                                                                                             |
| `sdk-typescript`       | `sdk_ts_gen.py --check`、`npm run check`（lint、类型、版本、README 示例、测试）、`npm run check:dist`                                                                                                                                                                                                                                                                                         |
| `sdk-python-adapters`  | `make test-sdk-adapters`：对每个其框架属于 SDK extra 的适配器，在各自带有该框架的 venv 中（位于 `uv.lock` 之外）运行其测试，并对其模块以及无需 extra 的适配器（`__init__.py`、`langgraph.py`、`strands.py`）运行 `mypy --strict`；在 Python 3.12 上运行，并在 3.11 上运行，且每个直接依赖（SDK 自身的、extra 的、pytest）都取其下限版本                                                       |
| `sdk-python-package`   | 用 uv 构建 SDK wheel，将其安装到干净的 venv 中并运行 `scripts/gates/sdk_wheel_smoke.py`，在 Python 3.11、3.12 和 3.14 上运行，另外在 3.11 和 3.14 上运行，且每个声明的依赖都取其下限版本                                                                                                                                                                                                      |

`.github/workflows/plugin-platforms.yml` 在变更涉及 `integrations/`、插件市场、`.gitattributes` 或该工作流本身时运行：在 Linux（x86_64、arm64、Alpine）、macOS（Apple silicon、Intel）、Windows（x86_64、arm64）和 WSL 2（Ubuntu、Node 18）上运行插件和 grep shim 测试套件，然后运行真实的 Claude Code 和 Codex 会话，插件从检出的代码安装（Claude Code 插件下的 `tests/e2e/agent-session.mjs`：本地的假服务同时应答模型 API 和 Loom）；还会在 Ubuntu 和 macOS 运行器上让 `node` 不在 agent 的 `PATH` 中，在 Ubuntu、macOS 和 Windows 运行器上完全没有 `node`（每个 hook 都失败，但不阻塞该轮对话），以及在 Windows 上没有 Git Bash 的情况。

`.github/workflows/codeql.yml` 在目标为 `main` 或 `release/X.Y` 分支线的拉取请求上、推送到这些分支时、每周以及按需运行 CodeQL：Python、JavaScript/TypeScript 和工作流各有一个 `codeql (<language>)` 检查。其发现的问题是代码扫描告警（仓库的 Security 标签页），由维护者分类处理；这些检查不阻塞合并。它排除的路径及原因写在该工作流的头部注释中。

`.github/workflows/scorecard.yml` 在推送到 `main` 时、每周以及按需运行 OpenSSF Scorecard 检查。它发布 README 徽章所显示的分数，并把发现的问题加入代码扫描；它不在拉取请求上运行，也不阻塞合并。

`.github/workflows/docs-site.yml` 在目标为 `main` 且改动了 `docs/`、`mkdocs.yml`、`llms.txt`、`llms-install.md`、`scripts/docs_site/`、`scripts/codegen/page_summary.py` 或该工作流本身的拉取请求上，以及每次推送到 `main` 时，用 `mkdocs build --strict` 构建文档站点，因此失效的链接或锚点会使 `docs site / build` 检查失败。仅当仓库变量 `DOCS_SITE_ENABLED` 为 `true` 时，它才会从 `main` 将站点发布到 GitHub Pages。

堆叠在另一个分支上的拉取请求，在重新指向 `main` 或 `release/X.Y` 分支线之前不会运行任何检查；存在合并冲突的拉取请求同样不会运行检查：推送后出现“no checks reported”通常就是这两种情况之一。

<a id="related"></a>

## 相关内容

- [本地开发](https://github.com/ClickHouse/loom/blob/main/docs/development/local-development.md)、[代码生成](https://github.com/ClickHouse/loom/blob/main/docs/development/code-generation.md)、[迁移](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md)
- [基准测试方法](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md)
