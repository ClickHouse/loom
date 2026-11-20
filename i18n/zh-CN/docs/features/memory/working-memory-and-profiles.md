<!-- i18n: source=docs/features/memory/working-memory-and-profiles.md blob=bedd8394f653eb3b5cd589b4bd7aed2749be116b model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md), which is authoritative.

<a id="working-memory-and-profiles"></a>

# 工作记忆与配置档案

有两个层面保存的文本，会在每次调用时直接交给 agent，而不是靠搜索找到：

- **工作记忆**：小型的命名块，用于租约、游标、检查点和"我做到哪里了"。这些块不会被嵌入、排序或搜索。
- **配置档案**：始终加载的、关于用户或 agent 的上下文（人设、偏好、关注点），渲染为一段提示词片段。

对于后续会话需要通过提问才能找到的内容，请使用 `memory.set`。对于无需提问就应该告知的内容，请使用这两个层面。如果把草稿行当作事实写入，它会在每次搜索和冲突检查中与真实事实相互竞争。

<a id="working-memory"></a>

## 工作记忆

块存放在 `loom.working_memory` 中，以身份元组加上 `run_id` 和 `block_key`（默认为 `scratchpad`，最多 80 个字符）作为键。块在重启后依然保留，且没有 TTL，因此可以作为长时间运行任务的恢复点（[追踪与恢复](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md)）。只有写入方删除块，或者某次擦除涉及该块时，它才会被移除。

| Op                       | 角色   | 作用                                                                            |
| ------------------------ | ------ | ------------------------------------------------------------------------------- |
| `working_memory.set`     | writer | 替换块的内容；会强制执行 `char_limit`。                                         |
| `working_memory.append`  | writer | 使用 `separator`（默认为换行符）追加 `addition`。                               |
| `working_memory.replace` | writer | 将块内所有出现的 `old` 替换为 `new`。                                           |
| `working_memory.delete`  | writer | 为块写入删除标记。                                                              |
| `working_memory.get`     | viewer | 读取单个块，并返回 `fill_ratio`。                                               |
| `working_memory.render`  | viewer | 将范围内的块（或最多 20 个指定的 `block_keys`）渲染为 XML 或 Markdown。         |

字段列表：[操作参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#working_memory)。

<a id="size-limits"></a>

### 大小限制

| 限制                 | 值                                                                        |
| -------------------- | ------------------------------------------------------------------------- |
| 默认 `char_limit`    | 每个块 4,096 个字符                                                       |
| 最大 `char_limit`    | 16,384 个字符                                                             |
| 超出限制             | 422 `WORKING_MEMORY_LIMIT_EXCEEDED`，并附带 `value_length` 和 `char_limit` |
| 每次渲染的块数       | `limit` 为 1–100（默认 20）                                               |

每次写入都会返回 `value_length` 和 `fill_ratio`（`len(content) / char_limit`），调用方可以据此在写入被拒绝之前先行精简内容。如果 `replace` 的 `old` 文本不存在，会返回 422 `SUBSTRING_NOT_FOUND`；如果目标块不存在，则返回 `WORKING_MEMORY_BLOCK_NOT_FOUND`。对不存在的块调用 `working_memory.get`，会返回 `found=false`。

```python
from loom import Agent

async with Agent(org="acme", namespace="support", agent="triage-bot") as bot:
    await bot.working_memory.set(
        "cursor: ticket 4812\nlease: holds refunds queue", block_key="state", run_id="run-7"
    )
    await bot.working_memory.append("step 3 done", block_key="state", run_id="run-7")
    print(await bot.working_memory.render(run_id="run-7"))
    # <working-memory block="state">cursor: ticket 4812 ...</working-memory>
```

<a id="profiles"></a>

## 档案

档案分区是一条 `memory_type='profile'` 的记忆行，其键为分区的 `label`。`profile.set`、`profile.append` 和 `profile.replace` 是对 `memory.set` 的薄封装，固定了该类型和 `char_limit`。读取单个分区使用以 label 为键的 `memory.get`；Python SDK 将其封装为 `agent.profile.get(label)`、`agent.profile.list()` 和 `agent.profile.delete(label)`（这些是 SDK 方法，不是 wire op）。

| Op                 | 角色   | 作用                                                                                         |
| ------------------ | ------ | -------------------------------------------------------------------------------------------- |
| `profile.set`      | writer | 在 `label` 处创建或替换分区（并发写入时使用 `if_match_sha256`）。                            |
| `profile.append`   | writer | 向分区追加内容；当其他客户端先写入时，通过 compare-and-swap 重试。                           |
| `profile.replace`  | writer | 重写分区内的某个子串（子串不存在时返回 422 `SUBSTRING_NOT_FOUND`）。                         |
| `profile.render`   | viewer | 将所有分区（或按顺序指定的 `labels`）渲染为一个 XML 或 Markdown 片段。                       |
| `profile.activity` | viewer | 哪些客户端读取过该档案，以及读取时间（`lookback_days`，上限为 30）。                         |

字段列表：[操作参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#profile)。

- **限制。** 默认 `char_limit` 为 8,192 个字符，按值的规范 JSON 计算；超出时返回 422 `CHAR_LIMIT_EXCEEDED`。
- **范围。** `profile.render` 默认读取命名空间内所有 agent 的分区（`scope="namespace"`），因此在控制台中写入的人设会到达每个 MCP 客户端。`scope="this_agent"` 则只读取调用者自己的分区。在 `read_scope` 为 `own_user` 或 `own_session` 时，只渲染该用户（或会话）的分区以及命名空间级别（`user_id` 为 `-`）的分区。
- **搜索。** 档案行不会出现在 `memory.search` 中。`loom_search` 工具有一个档案分支，当问题与用户相关时，会按语义对分区打分；每个分区的向量在每次写入后于请求路径之外刷新。参见[统一搜索](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)。
- **将事实归并进档案。** `profile_consolidation` worker（默认关闭，由 `LOOM_PROFILE_CONSOLIDATION_ENABLED` 控制）通过一次模型调用，将某个用户的偏好和决策记忆归并到一个带版本的分区（label 为 `consolidated_profile`）。带有受保护属性标签的行会被排除。其来源为 `derived`（`source_trace` 为 `profile.fold:<label>:<memory ids>`，即它所读取的事实）。

```python
async with Agent(org="acme", namespace="support", agent="triage-bot") as bot:
    await bot.profile.set("persona", "Terse. Cite ticket ids. Never promise refunds.")
    await bot.profile.append("preferences", "Prefers EUR invoices.", user_id="u-42")
    system = await bot.profile.render(user_id="u-42", read_scope="own_user")
```

<a id="structured-profiles"></a>

## 结构化画像

聊天机器人需要在第一次回复之前就了解用户，此时它读取的是一个由运营方定义形状的对象，而不是自由文本。管理员为命名空间设置画像 schema；`structured_profile` worker 根据用户的记忆，按该形状持续更新每个用户的画像；`profile.structured` 返回画像及其状态。读取不会调用模型。

| Op                     | 角色   | 作用                                                                                                                                         |
| ---------------------- | ------ | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `profile_schema.set`   | admin  | 设置命名空间的 schema（`json_schema`）；会记入审计。替换旧的 schema。                                                                        |
| `profile_schema.get`   | viewer | 返回 schema、其 `schema_sha256`、设置者和设置时间。                                                                                          |
| `profile_schema.clear` | admin  | 移除 schema；会记入审计。已构建的画像仍作为记忆行保留。                                                                                      |
| `profile.structured`   | viewer | 某个用户的画像：`profile`、`status`、`built_at`、`memory_id`、`version`（以及 `version_text`，即字符串形式的同一数值，在 JavaScript 中精确）。 |

schema 是一个 JSON Schema 对象，含 1 到 64 个 `properties`，最大 16 KiB。每个属性有 `type`（`string`、`number`、`integer`、`boolean`，或带有 `items` 的 `array`，其元素为前述类型之一，可选 `maxItems` 最大为 50）和 `description`，模型会读取该描述。一份完整的答案最多包含 256 个值，每个属性计一个，每个数组计 `maxItems` 个，这样才能放进模型的输出上限。字符串可使用 `enum`、`pattern`、`minLength` 和 `maxLength`；数字可使用 `minimum` 和 `maximum`，这些约束与[类型化记录](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md#typed-records)所用的相同。每个属性都出现在每份画像中，记忆无法确定时为 `null`，因此 schema 没有 `required`；`additionalProperties` 只能为 `false`。任何层级都接受 `title`，但会被忽略。

```python
schema = {
    "type": "object",
    "properties": {
        "preferred_name": {"type": "string", "description": "What the user asks to be called"},
        "timezone": {"type": "string", "description": "IANA time zone, e.g. Europe/Berlin"},
        "languages": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Languages they speak",
        },
    },
}
async with Agent(org="acme", namespace="support", agent="chat", api_key=ADMIN_TOKEN) as admin:
    await admin.client.profile_schema.set({"json_schema": schema})

async with Agent(org="acme", namespace="support", agent="chat") as bot:
    out = await bot.client.profile.structured({"user_id": "u-42"})
    # {"status": "ready", "profile": {"preferred_name": "Sam", "timezone": "Europe/Berlin",
    #  "languages": ["en", "de"]}, "built_at": "...", ...}
```

| `status`    | 含义                                                                                                                                                                                                  |
| ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ready`     | 根据用户当前的记忆，按当前 schema 构建；没有任何事实的用户，所有属性均为 `null`。                                                                                                                    |
| `stale`     | 构建之后记忆或 schema 发生了变化，或构建时其某个事实未被返回；在下一轮重建之前按已构建的内容提供。                                                                                                    |
| `pending`   | 没有有效画像：尚未构建，或已构建的行已过期、被编辑清除（直到 worker 重建）、或被标为 `high`（保持不动，直到该 key 移到新版本）；每个属性读作 `null`。                                                 |
| `no_schema` | 命名空间没有 schema，或其存储的 schema 已无法解析；`profile` 为 `{}`。                                                                                                                                |

- **存储。** 画像是该用户 key 为 `structured_profile` 的 `profile` 行（`agent` 为 `-`），因此 `profile.render` 和 `context.pack` 也会带上它，[`memory.history`](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md#versions-memoryhistory) 会列出它的各个版本。其来源（provenance）为 `derived`；`source_trace` 为 `profile.fold:structured_profile:<memory ids>`，即构建所依据的记忆。只有 worker 写入的行（其 `actor`）才会被返回：调用方写入该 key 的行读作没有画像（`pending`），无论它带什么标签，除非该行被固定（pinned）、锁定或标为 `high`，否则 worker 会在其上重建。
- **来源。** 用户当前的 `semantic`、`preference` 和 `decision` 事实，过滤规则与自由文本折叠相同：绝不使用 `high` 行、重复项（计入其规范行）、关于他人的事实（`about:other`）、受保护属性或已存储的遗忘请求。每个事实的时间取其成立时间（`valid_at`），否则取其最后一次变更时间；构建时先读取置信度最高、变更最近的事实，因此一次编辑会被当作最新的陈述。
- **过期判定。** 每份画像都带有其 schema 属性的哈希（`title` 或属性顺序的变化不会改变它）、其来源的摘要（每个来源的 id、内容哈希、置信度，以及构建读取并据以排序的日期：`valid_at`，否则为最后一次变更）和来源数量。新增或编辑的事实、回退、恢复、被删除的事实、置信度或日期的变化，以及使某行进入或离开来源的变更（变为 `high`、类型改变、获得或失去 `about:other`、`attribute:` 或 `forget` 标签、过期或超过其 `valid_to`），都会改变摘要。重述或强化也会，因为它把该事实移到模型所读内容的最新位置：冷却期之后会多一次重建。对来源事实执行 `memory.lock` 或 `memory.unlock` 同样如此：它会写入内容相同的新版本，使该事实的日期变为加锁时间，并把它移到模型所读内容的最新位置，进入构建所读事实中最新的部分。对于不同 key 下相互矛盾的两个事实，最后被加锁或解锁的那个随后读作最新。召回或回填会保留该行最后一次变更，因此不会触发。摘要覆盖所有来源，而构建只读取置信度最高且最新的 `LOOM_STRUCTURED_PROFILE_MEMORY_LIMIT`（100）条：对于事实多于此数的用户，该范围之外的变更仍会花费一次模型调用，却不可能改变答案。schema 变更会从下一轮起重建，每轮最多 `LOOM_STRUCTURED_PROFILE_MAX_USERS_PER_PASS` 份画像。删除（来源比画像记录的数量少）会跳过冷却，并在命名空间下一次被扫描时重建，只要各轮还有预算，最迟在 `LOOM_STRUCTURED_PROFILE_SCAN_MAX_AGE_SECONDS`（600）秒之后；`profile.structured` 会立即将其报告为 `stale`。其他变更在 `LOOM_STRUCTURED_PROFILE_COOLDOWN_SECONDS`（600）之后重建。如果构建缺少本应读到的事实（它所记录的事实之一，在记忆上限之内，未被返回：ClickHouse 副本落后于统计其来源的那个副本，或在 Postgres 下，权威库对该事实持有的某个变更尚未到达 ClickHouse），则读作 `stale`，并在冷却之后再次构建，只要该变更仍然缺失，每次都会调用一次模型。如果一次构建没有读到它所记录的任何事实，则不会调用模型：它会用全 `null` 的画像替换已提供的画像，每个冷却期之后再写一次；对没有已提供画像的用户（`pending`）则不写入任何内容。事实全部消失的用户会得到全 `null` 的画像，且不调用模型。已过期或被编辑清除的画像行读作没有画像（`pending`），并会被重建。`high` 画像行同样读作没有画像，但 worker 保持其原样：它像被固定一样，直到该 key 移到新版本。
- **输出上限。** 模型可以写出 `LLM_MAX_TOKENS` 中 `structured_profile` 的条目（32，可通过 `LOOM_LLM_MAX_TOKENS_OVERRIDES` 修改）加上 schema 对一份完整答案的估算：每个 key 连同其标点，以及每个值（每个属性一个，每个数组 `maxItems` 个），其大小按 `maxLength` 或其最长的 `enum` 值计，每个 token 两个字符；两者都没有的字符串按 48 个 token，数字或布尔值按 12 个。`profile_schema.set` 会拒绝估算值超过 16,384 个 token 的 schema。比估算更长的字符串仍可能被截断；请为自由文本属性设置 `maxLength` 以留出空间。
- **校验。** 模型的答案在写入之前要按 schema 校验：类型错误、超出其 enum 或约束的值、不是有限数的数字（或超过 2^53 的整数）、以及 schema 未声明的属性，都会被丢弃（置为 `null`）。
- **轮次。** 每一轮最多构建 `LOOM_STRUCTURED_PROFILE_MAX_USERS_PER_PASS`（20）份画像。它从上一轮停止的位置开始，按顺序处理带有 schema 的命名空间，每个命名空间得到均等的份额。没有可构建内容的命名空间不消耗份额，所以这一轮会继续读取后面的命名空间，直到预算用完或全部处理完；某个命名空间剩下的份额，会交给用满了自己全部份额的命名空间，从它们的扫描已列出的用户中取用。一轮最多可运行 `LOOM_STRUCTURED_PROFILE_MAX_PASS_SECONDS`（600）秒，之后 supervisor 会放弃它，并且在这段时间的后半段不再接收新用户：正在进行的构建有后半段时间来完成，尚未处理到的用户仍留在列表中等待下一轮。对一个命名空间的一次扫描最多列出其份额的十倍，之后的各轮从该列表中取用，直到列表取空、schema 变化，或列表达到 `LOOM_STRUCTURED_PROFILE_SCAN_MAX_AGE_SECONDS`（600）秒的存在时间；如果一次扫描发现所有落后的用户（少于其请求的数量），则在列表取空后，在此之前不会重复扫描，因此在没有其他用户落后时，一个命名空间在该时间内最多被扫描一次。构建失败的用户（模型出错，或答案不是 JSON 对象）要等待 `LOOM_STRUCTURED_PROFILE_BACKOFF_BASE_SECONDS`（300）秒，之后翻倍，直到 `LOOM_STRUCTURED_PROFILE_BACKOFF_CAP_SECONDS`（21600）秒为止。在等待期间进行的命名空间扫描会把它排除在外，所以它会在等待结束后的第一次扫描时再次被尝试：最迟为失败之后等待时间加上 `LOOM_STRUCTURED_PROFILE_SCAN_MAX_AGE_SECONDS`（600）秒。扫描或存储的 schema 出错的命名空间会被计数，并以同样方式退避，直到等待结束或其 schema 变化；正在等待的命名空间不占用这一轮的位置。画像行拒绝的构建（已锁定、已固定、标为 `high`、在黑名单中），或模型的答案在输出上限处被截断的构建，不会重试，直到用户的记忆、画像行（取消固定或解锁，被编辑清除的行也一样）或 schema 发生变化，或经过 `LOOM_STRUCTURED_PROFILE_REFUSAL_RETRY_SECONDS`（21600）秒，所以自行失效的锁会被重试。这些等待和扫描列表保存在 worker 的进程中，当另一个进程接手 worker 租约时会重新开始。一轮如果处理了所有带 schema 的命名空间并且没有发现落后的用户，则等待 `LOOM_STRUCTURED_PROFILE_IDLE_INTERVAL_SECONDS`（600）秒再进行下一轮；只处理了轮转中一部分的，则按 worker 的间隔再次运行。
- **擦除。** 对没有事实也没有画像行的用户，构建不写入任何内容，因此在扫描列出某用户之后被擦除的用户，在后续某一轮处理到他们时不会得到新行。如果在模型回答之后，它所读的某个事实已不再是当前的（运行期间被擦除或删除），构建同样不写入任何内容。剩下的只是该检查与写入之间的短暂间隙：擦除流程运行期间在此写入的行，是它最终清点时会发现的有效行，流程会因此以失败关闭（fail closed）。对来源事实执行 `memory.redact` 会清除其 trace 中提到它的每个画像版本，并且在画像 key 被锁定时会被拒绝。失败后重试的 redact 会触及同样的画像 key。trace 中没有记忆 id 的画像版本（仅有 `profile.fold:<label>`，即不记录来源的构建所写的形式，包括滚动升级期间的旧构建）不会被触及，其文本会保留到用户被擦除为止（[安全](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#sensitivity)）。
- **范围。** 读取始终针对一个 `user_id`。绑定到某个用户的令牌（`read_user_id`，即最终用户的令牌）只能读取该用户的画像，无论请求体中有没有 `user_id`；读取其他用户会返回 403。限定在单个会话的令牌（`own_session`）会被拒绝（没有 `session_id` 时为 `IDENTITY_INCOMPLETE`，带有一个时为 `AUTH_INSUFFICIENT`）：画像跨越用户的多个会话。
- **模型调用。** 只有 worker 会调用模型，且只针对有 schema 的命名空间，绝不会针对已关闭模型调用的组织（[`org.set_model_calls`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#org)），并通过已配置的提供方或推理网关进行。

<a id="how-contextpack-loads-them"></a>

## `context.pack` 如何加载它们

`context.pack` 会先渲染 `working_memory`，再渲染 `profile`，二者都排在依赖查询的各部分之前，因此 pack 的开头在不同问题之间保持不变，客户端可以在其后设置 prompt-cache 断点（`stable_prefix_chars`、`stable_hash`）。工作记忆按由窄到宽的顺序分层：针对某次 run 的 pack 还会包含所属 session 的块（`run_id` 为 `-`），针对某个 session 的 pack 还会包含 agent 的块（`session_id` 为 `-`，`run_id` 为 `-`）。插件的“我上次做到哪了”笔记（`session:<project>`）位于 agent 层；设置 `session_block` 后，其他项目的 `session:*` 笔记会被排除。各部分的顺序、预算以及其他 `context.*` 操作，参见[统一搜索](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)。

<a id="settings"></a>

## 设置

| 设置                            | 作用                                                                                                                                                        |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_PROFILE_CONSOLIDATION_*`  | 画像合并 worker（[整合](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#consolidation)）。                                                                  |
| `LOOM_STRUCTURED_PROFILE_*`     | 结构化画像 worker（[结构化画像](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#structured-profiles)）。                                                |
| `LOOM_MEMORY_ATTRIBUTE_TAGGING` | 为行标记受保护的属性，合并时会排除这些行（[敏感度与来源](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#sensitivity-and-provenance)）。 |

<a id="limits"></a>

## 限制

- **超出限制的写入会被拒绝，而不是被截断。** 请关注 `fill_ratio`。
- **工作记忆不是召回。** 块不会被排序，也不会被嵌入。除了 `context.pack` 和 render/get 操作之外，只有指明了 run 或 session 的调用才会携带块：`memory.ask` 会把该 run 的块放入其综合提示词中，`memory.search` 会在 `scratchpad` 中返回该 run 和该 session 的块。两者都在请求的读取范围内读取。
- **配置文件的每个小节，每个标签对应一行。** `profile.set` 会替换该行；先前的版本仍可通过 [`memory.history`](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md#versions-memoryhistory) 读取。
- **除非设置 `scope="this_agent"`，否则渲染默认涵盖所有 agent。**

<a id="related"></a>

## 相关内容

- [写入记忆](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)：持久化事实。
- [对话与会话](../conversations-and-sessions.md)：会话简报。
- [统一搜索](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)：`context.*` 和 `loom_search`。
