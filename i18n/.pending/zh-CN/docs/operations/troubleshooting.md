<!-- i18n: source=docs/operations/troubleshooting.md blob=3ede5f4702a909ff463f5c011fadcc17a512eb56 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md), which is authoritative.

<a id="troubleshooting"></a>

# 故障排查

每个条目依次给出现象、确认原因的检查方法以及修复办法。若要在账本、查询日志和链路追踪中跟踪一次请求，请参阅[调试请求](https://github.com/ClickHouse/loom/blob/main/docs/guides/debugging.md)。

<a id="start-here"></a>

## 从这里开始

在查看任何日志之前，用这三条命令就能回答大多数问题：

```bash
curl -s http://localhost:7777/v1/health | jq '{status, ch_ok, ch_error, schema, problems}'
docker exec loom-server loom doctor              # the same problems, plus pre-boot checks
curl -s -XPOST http://localhost:7777/v1/ops.health -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"org":"<org>"}' | jq   # ClickHouse parts, workers, stubs (ORG_ADMIN)
```

`problems` 中的每一项都带有 `fix`，请先阅读它。在客户端机器上，`loom doctor --client` 会检查 SDK 和 hooks 所解析到的服务器 URL 与令牌。各字段的含义见[监控](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md#health-checks)。

<a id="server-and-clickhouse"></a>

## 服务器与 ClickHouse

<a id="every-operation-fails-and-v1health-returns-503"></a>

### 所有操作都失败，且 /v1/health 返回 503

- **原因：** 服务器无法连接 ClickHouse。启动也会在第一次迁移处停止。
- **检查：** 运行 `curl -s localhost:7777/v1/health | jq .ch_error`，然后运行 `docker exec loom-server loom doctor`（查看 `clickhouse_reachable` 一行）。
- **修复：** 更正 `LOOM_CH_HOST` / `LOOM_CH_PORT` / `LOOM_CH_USER` / `LOOM_CH_PASSWORD`。端口 8443 意味着使用 TLS；在其他端口上使用 TLS 时，请设置 `LOOM_CH_SECURE=true`。在 ClickHouse Cloud 上，请检查服务是否在运行（空闲的服务会暂停），以及其 IP 允许列表是否包含 Loom 所在主机。

<a id="loom-up-times-out-or-the-container-exits-at-boot"></a>

### loom up 超时，或容器在启动时退出

- **原因：** 通常是迁移或 ClickHouse 错误、`.env` 中的值格式有误，或者在 compose 文件之外运行的进程没有设置 `LOOM_INSTANCE_LABEL`。
- **检查：** 运行 `docker logs loom-server`；日志中会指出失败的迁移文件或设置项。使用 `docker compose --env-file .env -f dev/docker-compose.cloud.yml config >/dev/null` 校验 env 文件（请使用你实际启动所用的 compose 文件）。
- **修复：** 按日志中的错误处理。开启认证的部署（`LOOM_DEPLOYMENT=1`）如果仍使用已提交到仓库的开发用 `LOOM_JWT_SECRET`，会按设计拒绝启动：请设置真正的密钥（[部署](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#required-environment)）。

<a id="writes-fail-with-too_many_parts-or-merges-are-failing"></a>

### 写入因 TOO_MANY_PARTS 失败，或合并失败

- **现象：** 写入失败并返回 ClickHouse 错误 252 `TOO_MANY_PARTS`，或者 `ops.health` 报告 `clickhouse_parts: fail`。
- **检查：** 合并是否在失败，这几乎总是原因所在，而不是写入速率。先在 `system.part_log` 中查看合并错误：

  ```sql
  SELECT table, error, count() AS failures, any(exception)
  FROM system.part_log
  WHERE database = 'loom' AND event_type = 'MergeParts' AND error != 0
    AND event_time > now() - INTERVAL 1 HOUR
  GROUP BY table, error;
  ```

- **修复：** 如果异常指明了服务器无法合并的跳数索引，请将该索引排除在合并时物化之外。该设置只修改元数据，可以撤销；新插入的数据分片仍会构建该索引：

  ```sql
  ALTER TABLE loom.<table> MODIFY SETTING
    exclude_materialize_skip_indexes_on_merge = '<existing list>,<index name>';
  ```

  合并成功后，数据分片数量会在几分钟内回落。不要先删除该索引（之后重建会重写整张表），也不要只调高 `parts_to_throw_insert`：合并停止时，分片数量会持续增长。如果没有合并在失败，请查找是否有写入方每条语句只插入一行（按 `log_comment` 对 `system.query_log` 分组，参见[查询归因](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md#logs-traces-and-query-attribution)）。

<a id="writes-take-seconds-after-a-clickhouse-cloud-replica-restart"></a>

### ClickHouse Cloud 副本重启后写入耗时数秒

- **现象：** 写入密集的操作（`memory.set`、`run.*`、`conversation.append`）耗时数秒，而读取仍然很快，且没有报错，常出现在一次部署之后，但部署并不是原因。
- **检查：** 副本的运行时长，以及系统日志表是否正在重新挂载：

  ```sql
  SELECT hostName(), uptime() FROM clusterAllReplicas(default, system.one);
  SELECT count() FROM system.query_log
  WHERE event_time > now() - INTERVAL 15 MINUTE AND query ILIKE 'ATTACH TABLE system.%';
  ```

- **修复：** Loom 一侧无需处理。重启后，服务会在一段时间内重新挂载其系统表，挂载完成后插入会恢复正常（通常在一小时内）。不要因此回滚部署；如果问题持续，请联系 ClickHouse Cloud 支持。

<a id="requests-fail-with-backend_timeout-or-overloaded"></a>

### 请求因 BACKEND_TIMEOUT 或 OVERLOADED 失败

- **原因：** 某条 ClickHouse 语句超过了其类别的截止时间（504 `BACKEND_TIMEOUT`），或者进程中所有池化的 ClickHouse 连接都持续处于繁忙状态（503 `OVERLOADED`）。在持续的写入速率下，批量插入（审计行、转录轮次、agent_events 刷新）会占用连接，最长为 `LOOM_CH_BATCHED_INSERT_WAIT_MAX_MS`，再加上刷新的时间；后台批量插入（使用事件和写入标记的刷新）最长占用 `LOOM_CH_BATCHED_INSERT_BACKGROUND_WAIT_MAX_MS`，再加上刷新的时间（[批量插入](../architecture/clickhouse.md#batched-inserts)）。`OVERLOADED` 也可能表示某条语句发现 ClickHouse 的内存低于服务器或 ClickHouse 用户的内存需求（代码 241，所有查询共用；服务器日志中会有 `ClickHouse is short of memory` 及 ClickHouse 的报错行，每 5 秒一次）。对于 `conversation.delete` 或 `conversation.compact`，`OVERLOADED` 还可能表示该线程 embedding 的写入在 8 轮协调中一直在持续落盘；请在该线程的写入稳定后重新发送。
- **检查：** 在 `system.query_log` 中按 `log_comment` 查看最慢的来源（[监控](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md#logs-traces-and-query-attribution)）；用 `ops.health` 查看分片压力；对于内存，查看 `system.query_log` 中 `exception_code = 241` 的语句以及旁边 `memory_usage` 最大的语句，并与服务器的 `max_server_memory_usage` 和用户的 `max_memory_usage_for_user` 对照。
- **修复：** 处理占主导的查询或 worker；对于内存问题，处理那条语句，或为 ClickHouse 服务增加内存。距离其 ClickHouse 很远的 Loom（跨广域网）需要将 `LOOM_LATENCY_BUDGET_MULTIPLIER` 设置在 20 左右，这样延迟预算就不会在每次调用时触发。对于持续写入速率下的 `OVERLOADED`，请按批量插入占用的连接数来确定连接池大小（[批量插入](../architecture/clickhouse.md#batched-inserts)）：调高 `LOOM_CH_POOL_MAXSIZE`（需要重启），用 `loom settings set ch_batched_inserts false` 关闭对应分组的批处理（启动时用 `LOOM_CH_BATCHED_INSERTS=false`；需要等待的异步插入仍会等待），或者用 `loom settings set ch_batched_insert_wait_max_ms <ms>`（以及 `ch_batched_insert_background_wait_max_ms`）在线降低插入等待时间。[运行时设置](https://github.com/ClickHouse/loom/blob/main/docs/operations/runtime-settings.md)介绍了在线覆盖。[错误参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md)列出了所有错误代码。

<a id="providers-and-search-quality"></a>

## 提供商与搜索质量

<a id="searches-fail-with-503-embed_timeout"></a>

### 搜索失败并返回 503 EMBED_TIMEOUT

- **原因：**embedding 提供商在 `LOOM_MEMORY_SEARCH_EMBED_BOUND_S`（2.5 秒）内两次尝试（默认重试一次，由 `LOOM_MEMORY_SEARCH_EMBED_BOUND_RETRIES` 控制）都没有返回查询的 embedding。如果查询的向量已在 embedding 缓存中，仍可正常应答。
- **检查：**查看服务器日志中的 `query embed exceeded …s (attempt n/m)` 警告，以及 `loom.agent_events` 中 `error_code = 'EMBED_TIMEOUT'` 的行。提供商挂起时不会设置健康标志；只有当它同时返回错误时，`/v1/health` 才会显示 `provider_failing:<name>`。同时查看提供商的状态页面。
- **修复：**直接使用 HTTP 或 MCP 的调用方重试一次；SDK 调用方已经重试过，因此应退避等待。如果问题反复出现，说明提供商响应缓慢或已宕机：可以等待其恢复，或者对于响应慢但仍在应答的提供商，调大该时限。每次失败的搜索最多会让服务器每次尝试占用一个时限的时间。

<a id="search-returns-arbitrary-results-or-extraction-writes-nothing"></a>

### 搜索返回任意结果，或提取没有写入任何内容

- **现象：**`set_from_messages` 没有写入任何事实，`memory.ask` 返回空答案，或者搜索按字符重合度排序。
- **原因：**进程运行在 fake 提供商上：被配置为 fake（`LOOM_LLM_PROVIDER=fake`，所有默认 `.env` 都这样设置；或者检出目录的 `.env` 或已升级的安装中使用 `fake-ngram-*` embedding），或者被配置为真实提供商但缺少密钥。
- **检查：**`curl -s localhost:7777/v1/health | jq '.problems[].code'` 显示 `llm_key_missing`、`embedder_key_missing`、`fake_llm` 或 `fake_embedder`（响应体中为 `fake_llm: true`、`fake_embedding: true`）。
- **修复：**设置问题的 `fix` 中指明的密钥（`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`GEMINI_API_KEY`）以及 `.env` 中的提供商配置块，然后重新创建容器（`docker compose … up -d --force-recreate`）。只设置密钥不会改变 `.env` 中固定为 `fake` 的提供商。在本地部署中，向量宽度在创建表时就已固定，因此从 fake embedder 切换需要执行 `make down-all && make dev`。由 fake embedder 生成 embedding 的行需要执行 `loom reembed`。设置 `LOOM_REQUIRE_REAL_PROVIDERS=true`，这样下次就会显示 `status: degraded`。

<a id="search-quality-dropped-after-an-embedder-change"></a>

### 更换 embedder 后搜索质量下降

- **原因：**新的 embedder 会改变所有相似度分数。仍由旧模型生成 embedding 的行处于不同的向量空间。
- **检查：**`loom reembed --dry-run` 会统计非当前模型的行；`loom.search.top_similarity` 会显示分数的变化。
- **修复：**执行 `loom reembed`（[维护](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md#re-embed-and-re-extract)），然后重新确定 `loom.search.top_similarity` 上告警的基线。开启 `LOOM_ADAPTIVE_THRESHOLDS`（默认开启）时，相似度阈值会在启动探测之后跟随新的 embedder；`loom recalibrate` 会立即重新运行该探测。

<a id="a-write-is-missing-from-search-or-was-not-stored"></a>

### 写入的内容在搜索中缺失，或未被存储

- **检查：**使用 `loom drops --namespace NS` 查看摄取管道丢弃或截断了什么，并查看该次写入自身的响应（`status`、冲突判定、`pipeline_errors`）。`loom.embed.pending` 统计仍在等待向量的行数。
- **修复：**按照丢弃原因处理（[维护](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md#memory-ingest-drops)）；对于存在冲突的写入，在控制台的 Review 队列中解决（[冲突与拒绝](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)）。

<a id="auth-and-tokens"></a>

## 认证与令牌

<a id="requests-fail-with-401-auth_invalid-or-503-auth_unavailable"></a>

### 请求失败，返回 401 AUTH_INVALID 或 503 AUTH_UNAVAILABLE

- **现象：** 返回 401 `AUTH_INVALID`，或返回 503 `AUTH_UNAVAILABLE` 并带有 `Retry-After: 5`。
- **检查：** 查看错误详情中的 `message`（已过期、签名错误、已吊销、缺少 bearer）。在客户端机器上，`loom doctor --client` 会测试钩子和 SDK 将要发送的令牌。
- **解决：**
  - 签名错误表示该令牌是用不同的 `LOOM_JWT_SECRET` 签发的（密钥已轮换，或是针对另一个部署签发的）：请使用 `docker exec loom-server loom mint-token …` 重新签发。
  - 已过期或已吊销的令牌需要换发新令牌；`loom revoke-token --list` 会列出吊销记录。
  - 启动后立即出现 `AUTH_UNAVAILABLE`，表示吊销列表尚未加载（`revocation_not_loaded`）；在加载完成之前，服务器会拒绝所有令牌，因此请等待几秒后重试，如果问题持续存在，请检查 ClickHouse。
  - 环境中遗留的旧 `LOOM_API_KEY` 会覆盖 SDK 和插件使用的 `~/.loom/token`。
  - 首次开启认证后，每次调用都需要令牌：请按上述方法签发一个。

<a id="an-id-you-know-exists-reads-as-missing"></a>

### 明明存在的 id 却被当作不存在

- **原因：** 仅通过组织范围的 id 寻址的 op，例如 `memory.get_by_id`，会按行不存在来应答，而不是返回会确认该 id 存在的 403。通常的原因是令牌在该行所在的命名空间上没有该 op 所需的角色。当调用方能访问多个包含该 id 的命名空间时（例如执行 `namespace.restore` 之后），在读取之前先解析 id 的 op 也会这样应答。规则及其例外情况见[行级范围的操作](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#row-scoped-operations)。
- **解决：** 使用在该行所在命名空间上具有该 op 所需角色的令牌（[令牌与授权](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)）；如果 id 存在于多个命名空间中，请在其命名空间内通过键来寻址该行（`memory.get`，写入则使用 `memory.update`）。

<a id="schema-and-upgrades"></a>

## 架构与升级

<a id="migrations-pending-or-checksum-drift"></a>

### 迁移待执行或校验和漂移

- **现象：**`/v1/health` 报告 `pending_migrations`，或 `loom migrate --dry-run --strict` 以退出码 1 退出（仅在出现仅存在于账本的行或校验和漂移时如此，仅有待执行文件不会触发），或 `redeploy.sh` 的预检中止。
- **检查：**`loom migrate --dry-run --strict` 会先列出待执行的文件，然后列出 `ledger-only` 行（仓库中已不存在的账本文件名）和 `checksum drift`（已应用文件的内容与记录的 SHA-256 不再一致）。
- **修复：**
  - 待执行文件：运行 `loom migrate`，或设置 `LOOM_BOOT_MIGRATE=true` 后重启。
  - 校验和漂移或 ledger-only 行，意味着某个迁移文件在被某台服务器应用之后又被修改（或被重命名并修改），通常是因为服务器运行了未合并分支的代码。将已应用的 DDL 与仓库中的文件进行比较。如果效果相同，则记录当前文件（以最新的一行为准）：

    ```sql
    INSERT INTO loom._migrations (filename, checksum, applied_by)
    VALUES ('<file>', '<sha256 of the file>', 'operator');
    ```

    如果某个 ledger-only 行的 DDL 已存在于新名称之下，可以用 `DELETE FROM loom._migrations WHERE filename = '<old name>'` 将其删除。

  - 如果效果不同，请编写一个新的迁移来调和架构；切勿修改已应用的文件（[迁移](../development/migrations.md)）。
  - 如果启动因某个表“仍有旧布局的行”而失败，需要先对该表运行 `scripts/ops/table_rebuild.py`（[升级](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#how-migrations-run)）。

<a id="requests-fail-with-503-schema_pending"></a>

### 请求返回 503 SCHEMA_PENDING

- **原因：**语句引用的表，将由当前构建所携带的某个迁移创建，而服务尚未应用该迁移（进程以 `LOOM_BOOT_MIGRATE=false` 启动，或某个迁移执行失败）。
- **修复：**同上文的待执行迁移。

<a id="cells"></a>

## 单元（Cells）

<a id="a-process-with-loom_cell-unset-is-gated"></a>

### 未设置 LOOM_CELL 的进程被拦截

- **现象：** 除健康探针外，所有请求都返回 503，`/v1/health/ready` 的 `cell` 检查失败，没有 worker 运行，原因中写明该部署“is a cell in enforced mode and LOOM_CELL is unset”、“holds a partial cell state (... present)”，或其“cell state is not read yet”。声明了 org 的 CLI 命令会拒绝执行，并提示“this deployment is a cell in enforced mode (cell ...) and LOOM_CELL is unset”、“holds a partial cell state and LOOM_CELL is unset”或“cell state could not be read (...)”；对于第一种，请在 CLI 上设置 `LOOM_CELL`；对于最后一种，请按其给出的原因处理：缺少授权（[升级步骤 15](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#upgrade-procedure)），或 Postgres 没有响应。
- **原因：** 在 Postgres 权威模式下，每个服务进程都会读取该部署的 cell 状态（`loom.cell_first`、`loom.tenancy_mode`、`loom.cell_self`、`loom.tenancy`）。未设置 `LOOM_CELL` 时，只有在这些表都没有任何行，或者它们保存的是处于 `recording` 状态的第一个 cell 的完整状态时，进程才会提供服务（[cell 架构](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md#the-tenancy-check-and-placement)）。
- **检查：** 路由器的目录中是否有该部署的行。没有路由器的部署可跳过此项。先在使用 `LOOM_PG_DSN` 的 psql 会话中执行：

  ```sql
  SELECT deployment_id AS id FROM loom.authority \gset
  \connect '<router DSN>'
  SELECT cell, is_first, state, placed FROM loom_router.cells WHERE deployment_id = :'id';
  ```

- **修复**，针对“not read yet”：进程启动时 Postgres 迁移尚未执行（`LOOM_BOOT_MIGRATE` 关闭），或此后一直没有连上 Postgres；请执行迁移（`/v1/health` 会列出它们）或恢复连接，随后一秒内的下一次读取会按下文放行或拦截该进程。
- **修复**，其他情况，按检查结果处理：
  - **该部署是一个 cell**（目录中有它的行，且它既不是未接入的后续 cell，也不是正在回退的第一个 cell，见下文）：将 `LOOM_CELL` 设置为该 cell 的名称；对于部分状态，再次运行 `loom cell prepare`，它会补全状态。
  - **该部署本不应是 cell**（目录中没有行，或没有路由器），或者是**未接入的后续 cell**（其行显示 `is_first = false`、`state = 'attaching'` 且 `placed = 0`），而你要把它变回单一部署：在一个事务中清除状态，在使用 `LOOM_PG_DSN` 的 psql 会话中（检查之后用 `\connect` 连回该库），以拥有 schema `loom` 各表的角色（即执行迁移的角色；修改触发器需要表的所有者）执行。之后该部署为 `open`。未接入的后续 cell 在目录中的行仍然有效，直到 `loom cell remove --cell <cell>` 将其删除，而重新 prepare 该部署需要先删除它。如果遇到锁失败，且 psql 仍然打开，先执行 `ROLLBACK`，然后重新执行整个步骤。

    ```sql
    \set ON_ERROR_STOP on
    BEGIN;
    SET LOCAL lock_timeout = '100ms';
    -- the attach lock: held by a running attach, prepare or update
    SELECT pg_advisory_xact_lock(7813586394088235880);
    SELECT pg_advisory_xact_lock(7813586394288842610);  -- the migration lock
    ALTER TABLE loom.cell_first DISABLE TRIGGER cell_first_immutable;
    DELETE FROM loom.cell_first;
    DELETE FROM loom.tenancy_mode;
    DELETE FROM loom.cell_self;
    DELETE FROM loom.tenancy;
    ALTER TABLE loom.cell_first ENABLE ALWAYS TRIGGER cell_first_immutable;
    ALTER TABLE loom.cell_first ENABLE ALWAYS TRIGGER cell_first_no_truncate;
    COMMIT;
    ```

    然后在同一会话中，以同一角色，在状态事务之后删除 org 触发器和 `loom.tenancy` 的守卫（这样执行迁移的启动过程中的 `ensure` 就不会再次创建它们）；如果遇到锁失败，且 psql 仍然打开，先执行 `ROLLBACK`，然后重新执行该步骤：

    ```sql
    \set ON_ERROR_STOP on
    BEGIN;
    SET LOCAL lock_timeout = '100ms';
    SELECT pg_advisory_xact_lock(7813586394288842610);  -- the migration lock
    SELECT format('DROP TRIGGER %I ON loom.%I', t.tgname, c.relname)
    FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'loom'
      AND t.tgname IN ('tenancy_insert', 'tenancy_update', 'tenancy_record') \gexec
    COMMIT;
    ```

    如果状态事务在 attach 锁上失败，说明该 Postgres 上正在运行 attach、prepare 或 update，这可能持续数小时：期间重新执行该步骤会以同样方式失败。下面的查询可以找出持有该锁的会话：

    ```sql
    SELECT a.pid, a.usename, a.application_name, a.client_addr, a.backend_start
    FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
    WHERE l.locktype = 'advisory' AND l.granted
      AND l.classid = (7813586394088235880 >> 32)::oid
      AND l.objid = (7813586394088235880 & 4294967295)::oid AND l.objsubid = 1
      AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database());
    ```

  - **正在回退的第一个 cell**（其行显示 `is_first = true`、`state = 'attaching'` 且 `placed = 0`，不论其 `mode` 是什么，因为在第 3 步的锁上被拒绝的 attach 会让它保持 `enforced`；并且在其 Postgres 上 `SELECT mode FROM loom.tenancy_mode` 读到 `recording`）：按以下顺序回退。在每个进程上取消设置 `LOOM_CELL`，此后每个进程都按 `recording` 提供服务；执行上面的状态事务和删除步骤；然后用路由器的运维凭据和该 cell 的 `LOOM_PG_DSN` 运行 `loom cell remove --cell <cell>`，在该 Postgres 读到 `open` 并记录该行的 deployment id 之前，它会拒绝执行。先删除该行会使每个新 org 都返回 503，先清除状态则会拦截每个设置了 `LOOM_CELL` 的进程。路由器无法读取该 cell 的 Postgres，所以直接在路由器上对该行调用 `DELETE` 会跳过这项检查，并使仍处于 prepared 状态的 cell 对其新 org 返回 503，且在执行上述步骤之前无法再次注册。清除后的存储不再保留任何 cell 状态，该部署需要重新通过 `loom cell prepare` 来准备。`placed > 0` 的第一个 cell 只能向前推进，不能回退：请再次运行它的 attach（删除操作会拒绝它）。

  - **另一个部署上恢复出来的 cell Postgres 副本**带有源 cell 的 deployment id，因此检查会返回仍在运行的源 cell 的行。不要清除它，也不要让它与源 cell 并行提供服务。

<a id="a-cli-with-loom_cell-set-refuses-a-command-that-writes-for-no-org"></a>

### 设置了 LOOM_CELL 的 CLI 拒绝不针对任何 org 写入的命令

- **现象：** 不针对任何 org 写入或执行撤销的命令（`loom secrets reseal`、`loom settings set`、`loom ch-setup`、`loom revoke-token` 等）退出，并提示“refusing: this Postgres's first row names cell ..., not LOOM_CELL=...”、“refusing: this cell's state could not be read (...)”或“refusing: this ClickHouse could not be read for the cell check (...)”。
- **原因：** 设置了 `LOOM_CELL` 时，这类命令在改动任何内容之前，会先从其 `LOOM_PG_DSN` 读取该 cell 的 `first` 行，并对照该 Postgres 检查其 ClickHouse（[cell 架构](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md#the-tenancy-check-and-placement)）。如果 first 行指向另一个 cell，说明该 CLI 指向了那个 cell 的存储。无法恢复访问权限的撤销操作则会在 Postgres 中断期间照常执行，并输出该 cell 未被检查的提示。
- **修复：** 切勿清除拒绝信息中所指的状态：那是另一个 cell 的状态。请把 `LOOM_PG_DSN` 和 ClickHouse 设置指向本 cell 自己的存储；如果 first 行所指的正是你想要的 cell，则把 `LOOM_CELL` 设置为该 cell。对于无法读取的状态，请按其给出的原因处理：缺少授权（[升级步骤 15](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#upgrade-procedure)），或 Postgres 或 ClickHouse 没有响应。

<a id="a-boot-logs-cell-triggers-not-applied"></a>

### 启动时记录“cell triggers: not applied”

- **现象：** 处于非 `open` 状态的 cell 在启动时记录 `cell triggers: not applied on loom.<table> (...)`，并且 `loom doctor` 指出该表缺失或被禁用的触发器（在任何此类状态下是 `loom.cell_first` 的触发器，在 `enforced` 下则是某个 org 表的触发器或 `loom.tenancy` 的守卫）。
- **原因：** 启动过程会创建 cell 的 org 触发器和守卫，并把 `loom.cell_first` 的触发器重新设为 always，每次尝试的锁等待为 100 毫秒，最多尝试三次，间隔一秒，在两种情况下会跳过某个表继续提供服务。当该表仍然繁忙时，日志会指出持有或排队等待冲突锁的会话：对该表的长事务或批量写入，或者 autovacuum；短时间的尝试不会使 autovacuum 让步，只能让它自行完成（等待足够久以取消它，会使该表的所有写入方都排在启动过程之后）。当执行迁移的角色不是该表的所有者时，日志会显示 `this role cannot alter it`，`loom doctor` 会将其列为 `owner` 缺陷。在之后的某次启动完成该表之前，不执行 tenancy 检查的写入方在该表上不会被拒绝；Loom 自身的写入不受影响。
- **修复：** 对于繁忙的表，如果日志指出的事务已陈旧，就结束它，或者等待 autovacuum 完成（见 `pg_stat_progress_vacuum`）；对于该角色无法修改的表，请以拥有 schema `loom` 各表的角色运行启动，或用 `ALTER TABLE loom.<table> OWNER TO <role>` 把该表交给该角色。然后在开启 `LOOM_BOOT_MIGRATE` 的情况下重启一个 pod，并重复此过程，直到 `loom doctor` 不再指出缺失或被禁用的触发器。

<a id="the-cell_marker-readiness-check-names-the-tenancy-marker"></a>

### cell_marker 就绪检查指出 tenancy 标记

- **现象：** 设置了 `LOOM_CELL` 的进程，其 `/v1/health/ready` 报告 `cell_marker` 检查并给出“pooled Postgres sessions do not keep the tenancy marker”：在 `enforced` 或 `partial` 的 cell 上检查失败；在仍处于 `recording` 的第一个 cell 上检查通过但带有该原因；并且该进程的 cell 心跳把 `marked` 报告为 `false`。
- **原因：** 进程与 Postgres 之间有连接池。连接池用自己的启动参数打开其服务端连接，因此这些连接没有 `loom.tenancy_checked`；用 `SET` 转发客户端参数的连接池也会被读为未标记，因为刷新会在读取标记之前运行 `RESET ALL`。cell 有了 org 触发器之后，这类会话对该 cell 不持有的 org 的每次写入都会被触发器拒绝，包括 Loom 自己的写入。
- **修复：** 按照 cell 的要求（[Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md)），让 `LOOM_PG_DSN` 直接指向 Postgres，不经过任何模式的连接池，然后重启这些进程：更改 `LOOM_PG_DSN` 需要重启。该检查在每次 cell 刷新（`LOOM_TENANCY_MODE_CACHE_S`）时重新读取，并在重启后的第一次刷新时恢复通过。

<a id="a-boot-fails-with-function-loomtenancy_refuse_-does-not-exist"></a>

### 启动失败，提示“function loom.tenancy_refuse_… does not exist”

- **现象：** 一个 `enforced` cell 在执行迁移的启动中创建 cell 触发器时失败，提示 `function loom.tenancy_refuse_unheld() does not exist`（或 `…_unmarked()`），并且 `loom doctor` 在任何状态下都指出该函数不存在。
- **原因：** 该函数在其迁移执行之后被人手工删除了（org 触发器对应 011，`loom.tenancy` 守卫对应 012；`DROP FUNCTION … CASCADE` 会连同其触发器一并删除）。迁移记录中已列出该文件，所以之后的启动都不会再次创建该函数，在它恢复之前，未经检查的写入方不会被拒绝。如果新版本的 `loom doctor` 在该版本首次执行迁移的启动之前指出该函数不存在，则无需处理：那次启动会运行该文件。
- **修复：** 以拥有 schema `loom` 各表的角色，用 `psql` 运行迁移文件（`schemas/pg_migrations/011_tenancy_triggers.sql` 或 `012_tenancy_guard.sql`），然后，在 `enforced` cell 上，开启 `LOOM_BOOT_MIGRATE` 重启一个 pod：它的启动会创建这些触发器（在其他任何 cell 上，由 attach 在之后创建）。

<a id="writes-are-slow-or-a-script-is-refused-on-a-deployment-that-is-not-a-cell"></a>

### 在不是 cell 的部署上，写入变慢或脚本被拒绝

- **现象：** 在 cell 状态为 `open`（每个进程都提供服务）的部署上，Loom 自身的写入以及任何脚本或批量加载器的写入，每行耗时更长；对 `loom.cell_first` 没有 `SELECT` 权限的角色收到“permission denied for table cell_first”；或者脚本对 `loom.tenancy` 的插入以 SQLSTATE `LT001` 失败。
- **原因：** org 触发器仍然留在它的表上：可能是对已 prepare 的 cell 做了仅 schema 的恢复，或清理时跳过了删除它们的步骤。在 `open` 状态下，它们放行每一行，但每次未标记的写入仍会读取状态。`loom.tenancy` 的守卫 `tenancy_record` 会以 SQLSTATE `LT001` 拒绝脚本写入的 tenancy 行：这一行本身会使状态变为 `partial`。
- **检查：**

  ```sql
  SELECT c.relname, t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'loom'
    AND t.tgname IN ('tenancy_insert', 'tenancy_update', 'tenancy_record');
  ```

- **修复：** 以拥有 schema `loom` 各表的角色，执行[未设置 LOOM_CELL 的进程被拦截](#a-process-with-loom_cell-unset-is-gated)中的删除步骤。

<a id="workers"></a>

## 后台 Workers

<a id="a-worker-fails-every-pass"></a>

### worker 每轮都失败

- **症状：** `loom.worker.consecutive_failures` 一直在 3 或更高；workers 日志反复出现 `<name>: pass failed` 或 `pass exceeded its deadline`，随后出现 `DEGRADED`。
- **检查：** 用 `GET /v1/admin/workers.status` 查看该 worker 的最近一次错误，用 `docker logs loom-workers 2>&1 | grep -E '<name>: (pass|DEGRADED)'` 查看回溯信息。
- **修复：** 根据异常信息处理。反复出现超时取消，说明这一轮的工作量相对于它的间隔太大（积压过多，或提供方响应太慢）。提供方报错通常意味着缺少密钥或配额不足。如果某个 LLM worker 使用的是 fake 提供方，请禁用该 worker，或配置一个真实的提供方。若想在排查期间停止某一个 worker，将它的开关设为 false 并重新创建 workers 容器；其他 worker 会继续运行。

<a id="no-worker-runs-at-all"></a>

### 所有 worker 都不运行

- **检查：** 查看 `/v1/health?include=workers`，确认每个进程上的 `workers_leader`；必须有一个进程持有租约。`workers_lease_absent` 为 warn，表示没有任何进程持有有效租约。`workers_lease_unreachable` 问题会说明某个进程为何无法向 ClickHouse 发起请求；`workers_lease_given_up` 则表示它的 workers 子进程一再启动失败。
- **修复：** 让每个副本都以 `LOOM_BACKGROUND_WORKERS=auto` 和 `LOOM_DEPLOYMENT=1` 运行；设为 `off` 的进程永远不会运行它们，而在部署之外的进程在 `auto` 下也不会运行。只有在没有任何部署使用的 ClickHouse 上才应设为 `on`：它会运行自己的一份副本，并且不获取租约。升级期间，较旧的构建会把租约让给仍在运行的较新构建。

<a id="host"></a>

## 主机

<a id="the-host-or-clickhouse-runs-out-of-disk"></a>

### 主机或 ClickHouse 磁盘空间耗尽

- **症状：**Loom 主机的磁盘被占满（容器无法写入，镜像构建失败），或者 ClickHouse 报告可用空间不足。
- **检查：**在主机上运行 `docker system df`，并查看 `var/code_repos` 的大小；在 ClickHouse 中，使用[维护](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md#disk-pressure-hygiene)中的各表大小查询和 `system.disks` 查询。磁盘已满时，请把诊断输出写入 `/dev/shm`。
- **修复：**限制 Docker JSON 日志的大小，清理构建缓存和悬空镜像（`docker builder prune`、`docker image prune`）。如果某个已停止的容器是你的回滚手段，请不要清理已停止的容器。在 ClickHouse 中，大规模删除数据后让合并操作折叠各版本，用 `loom code compact --apply` 压缩代码向量表，或者启用按表保留策略。

<a id="local-stack"></a>

## 本地部署

- **控制台返回 503。** 服务器从源码运行（`make server`），但没有构建前端。请运行 `make frontend-build`；无论是否构建前端，`/v1/admin/*` 都会响应。
- **部署忽略了你的 `.env`。** 不带 `--env-file .env` 的 `docker compose` 命令会使用 compose 文件中的默认值（启用认证，使用未配置密钥的 OpenAI 提供方）。请使用 `make dev` 或传入该标志。
- **从源码启动的服务器运行的是其他代码。** 请参阅[本地开发](../development/local-development.md#running-the-server-from-source)。

<a id="clients-plugins-and-mcp"></a>

## 客户端、插件与 MCP

- **编码 agent 插件**有各自的症状表（hook 找不到 Node、没有 primer、捕获内容未送达、工具被重复列出）：[Claude Code](../features/integrations/claude-code.md) 和 [Codex](../features/integrations/codex.md)。插件的 `loom-status` skill 无需 Loom 代码仓库即可运行客户端检查。
- **账本中没有该请求的记录。**请求从未到达服务器。`loom doctor --client` 会给出 SDK 和 hook 解析出的服务器 URL 与令牌，以及各自的来源；环境中过期的 `LOOM_API_KEY` 或 `LOOM_BASE_URL` 会优先于 `~/.loom/`。使用该令牌调用 `GET /v1/whoami`，可以看到服务器验证过的主体、组织、命名空间和角色。对令牌未指明的命名空间返回 403，或者带范围的令牌在请求体中没有命名空间而返回 `IDENTITY_INCOMPLETE`，属于授权问题，不是 bug（[令牌与授权](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md#namespace-access)）。临时目录中遗留的 `loom-flush-*.json` 文件是从未送达服务器的插件捕获内容（[无头机器人](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md#5-when-the-bot-is-quiet)）。
- **MCP 工具没有出现在列表中。**列表随 MCP URL 的 `?toolset=` 而变化，与调用方无关。在 `all` 下，核心列表和 atomic 名单都未列出的实验性 op 不会出现在列表中，但仍可按名称或通过 `loom_op` 调用。`code.*` 工具只有在设置 `LOOM_MCP_CODE_TOOLS=true` 时才会出现在默认 toolset 中（[MCP 工具集](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets)）。
- **MCP 调用返回 `isError: true`。**除 401 以外的拒绝（`LOCKED`、`PINNED_CONFLICT`、`BLACKLISTED`、`RATE_LIMITED`、`UNKNOWN_ARGUMENT` 等）会作为带有 `isError` 的工具结果返回，HTTP 错误详情位于 `structuredContent` 中；401 或 5xx 则是 JSON-RPC 错误。请根据 `code` 进行匹配。
- **你发送的某个字段被忽略了。**响应会在 `ignored_args` 中列出该字段，如果有相近的已知字段，还会给出 `did_you_mean`；写操作会以 422 `UNKNOWN_ARGUMENT` 拒绝相近的拼写错误（[未知字段与已移除字段](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#unknown-and-removed-fields)）。
- **429 `RATE_LIMITED`。**参见[监控](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md#rate-limits-and-traffic-classes)中的限流桶；`Retry-After` 会告知何时可以重试。

<a id="filing-a-bug"></a>

## 提交 bug

`loom report` 会将 `loom doctor` 的诊断结果输出到 bug 表单的 Environment 字段中（仅包含是否存在的布尔值，绝不包含密钥的值）；`--open` 会打开 bug 表单并填入版本信息（[提交 issue](https://github.com/ClickHouse/loom/blob/main/CONTRIBUTING.md#filing-an-issue)）。
