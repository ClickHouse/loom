<!-- i18n: source=docs/features/conversations-and-sessions.md blob=b2079f3bf2ad65d5e3c485f2587db24b35257342 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md), which is authoritative.

<a id="conversations-and-sessions"></a>

# 对话与会话

Loom 保存三类会话状态，每一类位于各自的平面：

| 平面 | 内容 | 是否嵌入、可搜索 | 写入方 |
| ------------------ | -------------------------------------------------------------------------- | --------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| 对话平面 | 一个线程的逐字对话轮次（`loom.conversations`） | 用户和助手的轮次以及压缩摘要，通过 `conversation.search` 搜索 | `conversation.append`、插件的捕获钩子、`loom ingest-transcript` |
| 会话简报 | 一条工作线的精简、可恢复状态（`loom.session_briefs`） | 否 | `session.brief_save`、编码插件在压缩和会话结束时写入 |
| 记忆 | 从对话内容中提取的持久事实 | 是，通过 `memory.search` | `memory.set`、`memory.set_from_messages` |

保存一个对话轮次并不会创建记忆；若要把对话转换为事实，请将其提交到 [`memory.set_from_messages`](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md)。所有者可以关闭对话记录功能，而不会丢失已提取的记忆，并且简报在召回时不会与事实竞争。

<a id="conversations"></a>

## 对话

<a id="recording-is-the-owners-choice"></a>

### 是否记录由所有者决定

只有当命名空间开启了对话日志时，`conversation.append` 才会存储一个回合。该开关（`loom.namespaces` 上的 `conversation_logging_enabled`）默认关闭，并且失败时默认关闭：没有对应行的命名空间不会被记录。所有者通过 `conversation.set_logging(enabled=true)`（admin）或控制台“对话”页面上的 **Record conversations** 开关将其开启。

日志关闭时，append 请求会被接受，但不保留任何内容：响应为 `stored: false, reason: "logging_disabled"`。客户端无法覆盖所有者的选择。如果某个回合在其嵌入运行期间被删除，append 会跳过迟到的向量，并返回 `stored: false, reason: "deleted"`。

<a id="writing-turns"></a>

### 写入回合

`conversation.append` 接受 `thread_id`、`role`（`user`、`assistant`、`system` 或 `tool`）、`content`，以及可选的 `msg_id`、`reasoning`（模型的推理内容，保存在单独的列中）、`user_id`、`run_id`、`metadata` 和 `tokens`。线程在首次 append 时创建；`conversation.create` 只是预先附加 `title`、`metadata` 或 `run_id`。

- **基于 `msg_id` 幂等。** 如果 `(thread_id, msg_id)` 已经写入，该回合不会再次写入（`reason: "duplicate"`）。没有 `msg_id` 时，服务器会根据线程、角色和内容的哈希派生一个，因此重新记录某个回合的客户端不会使对话记录翻倍。
- **用户和助手回合会被嵌入**，最多取其前 2,048 个字符，并保存在嵌入行中：重新嵌入从生成该向量的文本开始。早期版本写入的行保留 240 个字符，重新嵌入基于这些字符进行。`system` 和 `tool` 回合保留在对话记录中，不建立索引，但压缩生成的摘要除外。
- **嵌入失败时放行。** 嵌入失败时仍会存储该回合，并将错误记录在其嵌入行上。不会有任何重试：该回合不参与向量搜索，只能通过子串回退找到。
- **被判定为高敏感度的回合不会被嵌入。** 如果某个回合的文本包含高敏感度形态（粘贴的 API 密钥、`password=` 赋值、银行卡号、SSN 或明示的 PIN），它会按原样存储，但没有嵌入行，其文本也绝不会发送给嵌入器。`conversation.search` 永远不会返回它，无论是通过向量还是子串回退；`conversation.threads` 会列出这类最后一个回合，但摘录为空（被判定为高敏感度的标题同样显示为空）；它会检查摘录所显示的 240 个字符以及其后的 1,024 个字符。标题生成器绝不会把这样的回合发送给它的模型。如果一个线程的前八个用户和助手回合都被判定为高敏感度，或者其生成的标题被判定为高敏感度，该线程保持无标题，并在其 metadata 中标记 `title_refused`，使标题生成器不再重复请求；如果此类回合少于八个，则不做标记，等待一个普通回合。来自 `conversation.update` 或 `conversation.create` 的新 `metadata` 会替换该标记，随后标题生成器会再请求一次，并再次标记该线程。`conversation.get` 和 `conversation.list` 仍会在其线程中返回该回合（[敏感度](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#sensitivity)）。

<a id="saving-single-messages"></a>

### 保存单条消息

聊天客户端可以让用户保存单条消息以便日后搜索，而无需记录命名空间的对话记录。`conversation.save_message`（writer）就是这种主动选择，`conversation.search_saved_messages`（viewer）是对应的搜索。已保存的消息是一条对话行，绝不是用户个人集合中的文档（[共享](https://github.com/ClickHouse/loom/blob/main/docs/architecture/sharing.md#saved-messages)）。

只有当每个副本在压缩期间都保留已保存状态时，客户端才可以保存消息。参见[滚动升级规则](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md#the-rules)。

它们与 `conversation.append` 和 `conversation.search` 有六点不同：

- **Record conversations 开关不适用。** 即使该开关（或 `conversation.set_logging`）已关闭记录，保存的消息仍会被存储，并且保存不会开启记录。
- **消息被标记，而不是重新标注。** 对话记录行及其嵌入行带有 `is_saved` 和 `saved_at`，即用户保存该消息的时间。对话记录中已有的消息会就地标记：其 `run_id` 和内容保持原样。
- **搜索按用户进行。** `conversation.search_saved_messages` 只返回指定的那一个命名空间中归 `user_id` 所有且已标记的消息。`conversation.search` 对所有已记录的回合排序，并默认在调用者的各命名空间之间扩散查询。
- **搜索是混合式的。** 向量路径嵌入消息的前 2,048 个字符；BM25 路径读取其完整文本，因此精确词（工单 ID、表名）或长篇已保存回答深处的短语仍能排名。`search_mode="hybrid"`（默认）通过倒数排名融合两者，`semantic` 和 `lexical` 只运行其中一路，`lexical_weight`（0–1）用于缩放 BM25 路径。BM25 命中的摘录以匹配词为中心。语料库是一个用户的已保存消息，因此其统计量在每次查询时计算。
- **固定到单个用户的令牌在这里可用。** 通过同一个凭据为多个用户提供服务的集成，使用 `--read-scope own_user`（以及用于指定某个用户的 `--read-user-id`）来生成它。此时已保存消息操作只以该用户的身份执行：请求体中指定了其他 `user_id` 会被以 403 拒绝，并且每条语句都带有令牌所属的用户。终端用户令牌使用 `namespace: "@me"` 调用这些操作（[共享](https://github.com/ClickHouse/loom/blob/main/docs/architecture/sharing.md#saved-messages)）。对话表没有 `session_id`，因此 `own_session` 令牌会被拒绝。在这种令牌下，其他所有对话操作（`append`、`get`、`list`、`search`、`threads` 等）仍会被拒绝，因为一个线程混合了多个用户，没有可限定的用户维度。
- **每次保存都会检查所有权、删除状态和角色。** 保存存放在另一个 `user_id` 名下的消息会返回 `indexed: false, reason: "owner_mismatch"`。已被标记删除的消息返回 `reason: "deleted"`，已存储的 system 或 tool 回合（从不嵌入）返回 `reason: "unsupported_role"`。文本被判定为高敏感度的消息同样不会被嵌入：它返回 `reason: "high_sensitivity"`，并且新消息不会被存储。早期版本保存的此类消息绝不会成为 `conversation.search_saved_messages` 的向量或关键词命中，`conversation.list_saved_messages` 列出它时摘录为空（它检查所显示的 `conversation_search_excerpt_chars` 个字符以及其后的 1,024 个字符，与 `conversation.threads` 相同）。这些检查同样适用于已经保存的消息。

保存和取消保存与 append 的嵌入写入、压缩退役和线程删除共享一个可续期的线程租约。Append 和压缩在获取租约之前完成提供方的工作。

由对话记录行决定谁拥有已保存的消息。保存操作只在变更本身中认领无人拥有或调用者拥有的行，因此两个用户保存同一条无主消息时，第一个认领成功，第二个得到 `owner_mismatch`。只有在对话记录行读回为调用者所有之后，才会标记嵌入行。

保存可以安全重试。对一条新消息的两次并发保存只存储一个对话记录行：其插入携带从消息标识派生的去重令牌。每次调用都会重新读取该消息，并修复中断或并发写入留下的问题：写入缺失的嵌入行，在上次嵌入失败时重新嵌入，并标记仍未标记的行。嵌入失败的保存会将消息存为已保存，并返回 `indexed: false, reason: "embed_failed"`；再次保存会重试嵌入。如果对话记录 append 的嵌入在并发保存之后才落地，它会标记自己的嵌入行。

`conversation.list_saved_messages`（viewer）无需查询即可分页浏览某个用户的已保存消息，最近保存的在前（`limit` 最大 200，一个不透明的 `cursor`，以及可选的 `conversation_id`）。其 `ts` 是用户保存该消息的时间，与对话记录的消息时间无关。没有保存时间戳的行使用其消息时间戳。它是“已保存”面板的首个视图；用户输入后由搜索来缩小范围。`conversation.saved_status`（viewer）接受一个 `conversation_id` 和最多 500 个 `message_ids`，并返回其中哪些已被该用户保存，因此客户端在打开对话时一次调用就能绘制每条消息的保存状态。

`conversation.unsave_message`（writer）将已保存的消息从其所有者的搜索中撤回。在记录对话记录的命名空间中，该回合保留在对话记录里，只是标志被清除，因为所有者选择了记录它。在不记录的命名空间中，该回合通过轻量级删除被移除，因为它只是因保存而存在（`removed: true`）。嵌入保留一个墓碑，使旧版本无法重新出现。调用者的用户从未保存过的消息，或另一个用户保存的消息，返回 `found: false`；它从不说明是哪一种。取消保存是幂等的，再次保存会恢复该消息。按用户的擦除会连同该用户的其他对话行一起删除已保存的消息。

<a id="reading-and-searching"></a>

### 读取与搜索

| 操作                   | 角色   | 作用                                                                                                                       |
| ---------------------- | ------ | -------------------------------------------------------------------------------------------------------------------------- |
| `conversation.threads` | viewer | 命名空间的近期线程，最新的在前（`window_days`、`limit`、最大 100,000 的 `offset`；`agent="*"` 表示所有 agent）。 |
| `conversation.get`     | viewer | 一个线程的元数据；`include_messages=true` 会分页返回其回合（`order="desc"` 用于获取末尾）。                              |
| `conversation.list`    | viewer | 一个线程的回合，基于游标分页，可按 `run_id` 筛选。                                                                |
| `conversation.search`  | viewer | 对已嵌入回合的向量搜索，可按线程、运行或角色筛选。                                                                |
| `conversation.update`  | writer | 修改线程的 `title`、`metadata` 或 `run_id`。                                                                         |

`conversation.search` 按余弦相似度对回合排序，丢弃低于 `LOOM_CONVERSATION_SEARCH_MIN_SIMILARITY`（0.30）的结果，并默认在调用者的各命名空间之间扩散查询（`namespace_scope="all_mine"`）。当嵌入器不可用时，它会回退到子串匹配，使该操作仍能响应。命中结果显示回合的前 `LOOM_CONVERSATION_SEARCH_EXCERPT_CHARS` 个字符（默认 240）；请用 `conversation.get` 或 `conversation.list` 读取该回合。该操作对 MCP 隐藏；agent 使用 [`loom_search`](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md) 的对话部分。

`memory.recall` 是另一种读取：它搜索提取过程读取过的回合（保存在 `loom.memory_evidence` 中），无论对话层是否记录，这些回合都存在。参见[搜索与提问](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)。

<a id="compacting-and-deleting"></a>

### 压缩与删除

- **`conversation.compact`**（writer，需要 LLM 提供方）会对最后 `keep_recent`（默认 10）个回合之前、能放入摘要器提示词的最早回合进行摘要，将摘要写成一个已嵌入的 `system` 回合，其 `metadata.compacted_from` 列出被摘要的 `msg_id`，并将原回合及其嵌入标记为墓碑：随后 `conversation.search` 会找到摘要来代替它们。如果线程中另一个 agent 仍持有一个文本相同的有效回合（即回合搜索建立索引的那类回合），则该回合保留其嵌入（见下文删除）。少于 `min_to_compact`（默认 4）个新回合的窗口不会被处理。早先的摘要既不计入 `min_to_compact`，也不计入 `messages_summarized`。窗口起始处的摘要从单独的额度折叠进提示词；放不进剩余额度的摘要保持有效，而失败的运行留下的摘要（其回合全都仍有效并在窗口内，位于第一个回合的 `ts`）会被不经阅读直接替换。窗口更靠后的摘要与任何回合一样占用提示词预算，并随之被替换。被判定为高敏感度的回合不纳入窗口：它保持有效，绝不会发送给摘要器，因此放在窗口第一个回合位置的摘要，在线程中可能排在它之前。用 `conversation.save_message` 保存的回合同样绝不会被摘要或标记为墓碑；它原样保留在摘要与近期回合之间。无法嵌入的摘要不会压缩任何内容（`reason: "embed-failed"`），被判定为高敏感度的摘要同样如此（`reason: "summary-high-sensitivity"`），因为它从不嵌入。与 `embed-failed` 不同，这种拒绝不值得重试：只要其回合仍然有效，窗口就保持不变，因此重试要为同一次摘要器调用付费，且很可能得到同样的摘要。只包含已保存回合或被判定为高敏感度回合的窗口返回 `reason: "nothing-to-compact"`。
- **`conversation.delete`**（admin）将一个线程的回合、线程行和嵌入标记为墓碑；`confirm_thread_id` 必须等于 `thread_id`。如果另一个 agent 也在该线程中记录了某个回合，只要该 agent 持有一个文本相同、被搜索建立索引的有效副本（用户或助手回合，或摘要），该回合就保留其嵌入，因为两个副本共享一个嵌入行；该行采用那个副本的 agent、角色、运行和用户，因此执行删除的 agent 自己的搜索会丢弃它（压缩的做法相同）。恰好 240 个字符的行（早期版本的截断，或恰好这个长度的回合）只有在其前 2,048 个字符与所属 agent 的回合匹配的副本存在时才会保留，在没有可供核对的消息行时则被标记为墓碑。删除还会处理该线程中那些所属 agent 已不再持有该回合有效消息的行：该 agent 已删除或压缩的回合（早期版本遗留了一些），或保留期 TTL 已使其过期的回合。若要清除某个人或某个命名空间在各处的数据，请使用 [`erasure.request`](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)。

被标记为墓碑的回合会从所有读取中过滤掉，只有 `erasure.export` 例外，它会返回这些回合，除非指定 `include_tombstoned: false`。只有当运维人员设置了表 TTL（`LOOM_RETENTION_CONVERSATIONS_DAYS`，0 表示永久保留）时，它们才会从磁盘上移除。该 TTL 只覆盖 `loom.conversations`：回合的嵌入行保存其最多 2,048 个字符的文本，没有 TTL。一旦其消息过期，该线程中的整线程删除会将它交给该回合的有效副本或将其标记为墓碑；擦除请求会将其标记为墓碑，并且只有带 `purge` 的 `namespace_list` 擦除才会移除它。`erasure.export` 不返回嵌入行，因此被 TTL 过期的回合的摘录会随主体一起被擦除，但不在其导出中。

<a id="thread-titles"></a>

### 线程标题

`conversation_titler` worker 为无标题的线程（由没有 `conversation.create` 的 append 创建）生成一个由 LLM 撰写的简短标题，控制台的“对话”页面以它作为首项。它覆盖记录对话的命名空间中，在最近 `LOOM_CONVERSATION_TITLER_WINDOW_HOURS`（168）小时内活跃且至少有 `LOOM_CONVERSATION_TITLER_MIN_TURNS`（4）个回合的线程，每轮最多处理 `LOOM_CONVERSATION_TITLER_MAX_THREADS_PER_PASS`（10）个。它默认开启（`LOOM_CONVERSATION_TITLER_ENABLED`），在使用 fake LLM 时不做任何事。

<a id="capturing-sessions-from-clients"></a>

## 从客户端捕获会话

| 客户端 | 轮次如何到达 Loom |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Claude Code 插件 | Stop hook（`hooks/loom-conversation.mjs`）会把会话记录中的每个新轮次追加进来，`msg_id` 即记录的 uuid；设置 `LOOM_CAPTURE_CONVERSATIONS=false` 可将其关闭。它会缓存命名空间的记录判定结果，并在记录关闭时跳过追加。 |
| Codex 插件 | 采用相同的捕获方式，参见 [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md)。 |
| Cursor | `integrations/cursor/cursor-session-hook.sh` 由 Cursor 的 after-conversation hook 运行，并把会话记录交给 `loom ingest-transcript`。 |
| 其他 agent | 对导出的会话记录运行 `integrations/generic/ingest.sh`。 |
| 任意 MCP 客户端 | 默认的 `core` 列表不包含 `conversation.append`，其中也没有任何内容提示模型记录轮次：请使用会话记录 hook 进行捕获，或挂载 `?toolset=all`，其列表中包含 `loom_conversation_append`。 |
| 你自己的代码 | 使用 `conversation.append`，或下文的 Python SDK `Session`。 |

`loom ingest-transcript --namespace NS --file session.md` 会读取一个 Markdown 文件（其中交替出现 `# user` / `# assistant` 小节），或一个由 `{role, content}` 组成的 JSON 数组，并追加每个轮次（`--dry-run` 只统计轮次数；`--thread-id` 用于设置固定的 id）。

这些插件还会在会话结束时把该会话的轮次交给 `memory.set_from_messages(defer=true)`，因此无论是否记录了会话记录，都会提取事实，并通过 `observe.record` / `observe.crystallize` 把工具操作记录为情景观察（参见[提取](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md)）。逐个 hook 的接入方式见 [Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md)。

<a id="session-briefs"></a>

## 会话简报

会话简报是一条工作线的可恢复状态：目标、状态、决策、死胡同、已完成事项、未决问题、后续步骤、关键文件、约束和指针。它会在压缩之后、第二天、在另一个客户端中或由团队成员读取。它不会被嵌入，也不会由 `memory.search` 返回。任何能读取该命名空间的 agent 都可以恢复它。

| 操作 | 角色 | 作用 |
| ---------------------- | ------ | ---------------------------------------------------------------------------------------------------------------------- |
| `session.brief_save`   | writer | 根据 `events`（自上次保存以来发生的事情）、agent 编写的 `sections`，或两者一起，修订（或新建）简报。 |
| `session.brief_get`    | viewer | 按 `brief_key` 加载简报；否则加载某个会话写入的最新简报（`from_session`）；再否则加载某个 `project` 的最新简报。 |
| `session.brief_list`   | viewer | 列出该命名空间的简报，最新的在前，每条一行（`mine_only` 只列出调用者自己的）。 |
| `session.brief_delete` | writer | 当简报的作者要求忘记它时，删除该简报。 |

**寻址。** 空的 `brief_key` 会变成 `auto/<project>/<branch>`，因此同一条工作线上的每个会话都会修订同一份简报；如果这两者都未知，则变成 `session/<session_id>`。键需匹配 `^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,159}$`。限定到单个用户（或会话）的令牌，会把其简报存为 `~<user>~<key>`（或 `~<user>~<session>~<key>`），并以 `<key>` 的形式看到它们；覆盖整个命名空间的令牌会以该前缀列出它们，并可以通过该前缀获取或删除它们。受限令牌不能发送带 `~` 的键。

**修订。** 带有 `events` 的保存（类型为 `user`、`assistant`、`edit`、`command`、`error`、`read`、`search`、`tool`、`note`、`summary`；每次调用最多 4,000 条）会在服务端进行浓缩。当 `LOOM_SESSION_BRIEF_LLM` 开启（默认）时，由 `LOOM_SESSION_BRIEF_LLM_MODEL` 指定的模型针对每个 `LOOM_SESSION_BRIEF_WINDOW_CHARS` 个字符的窗口修订一次简报，每次保存最多处理 `LOOM_SESSION_BRIEF_MAX_WINDOWS` 个窗口；如果没有该模型的密钥，则由部署所配置的 LLM 来写；如果没有 LLM，则由确定性构建器写出目标、状态、文件、错误和指针（没有决策或死胡同）。响应中的 `author` 为 `llm`、`deterministic` 或 `model`（由调用方 agent 编写各部分）。在运营方的[提取策略](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md#operator-extraction-policy)下，构建器不会运行，也不会从这些事件中获取客户端摘要。模型调用失败的窗口会被跳过，其余窗口照常修订。仅包含事件且没有可调用的模型（未配置、`use_llm: false`，或组织在修订第一个窗口之前关闭了模型调用）的保存不会写入任何内容，并返回 `changed: false`、`reason: "not-revised"`；如果组织在部分窗口已修订之后才关闭，则保留这些窗口，不再修订更多窗口。仅包含事件且所有模型调用都失败、被未读取的开关拦住或无法解析的保存，会被以 503 拒绝，由客户端重新发送。如果保存同时带有 agent 的 `sections`，则在这两种情况下都会把它们写在原有简报之上，并且不从事件中取用任何内容。
状态部分在每次修订时被替换；决策、死胡同和已完成事项会累积。每次修订都保存在 `loom.session_brief_revisions` 中；`brief_get` 上的 `revision=N` 可读取其中一次修订。如果一次保存重复了上一次保存的会话和 `event_cursor`，则视为重试，不写入任何内容；`if_revision` 使保存成为比较并设置（不匹配时返回 409）。

**恢复由人来决定。** 编码插件会在压缩和会话结束时保存简报，客户端不消耗任何模型 token，而会话开始时的引导信息只会列出已保存的简报名称。加载简报发生在用户提出要求时，通过 `loom-session` skill 或控制台“对话”页面的简报部分完成。`brief_get(full=true)` 会把客户端自己最近一次的压缩摘要原文附加在简报下方。简报绝不会自动加载：只有用户知道自己要恢复的是哪条工作线。

<a id="example"></a>

## 示例

Python SDK 的 `Session` 将这些 op 绑定到一个 `(user, session)` 范围：`append` 把对话轮次写入转录记录，并且每经过 `flush_every` 个轮次，就写入 `memory.set_from_messages`；`resume` 是该范围的 `context.pack` 加上转录记录的末尾部分。

```python
from loom import Agent

async with Agent("dev", "local", "support-bot", base_url="http://localhost:7777") as bot:
    s = bot.session("thread-42", user_id="u1")
    resumed = await s.resume(query="resume the billing migration")
    await s.append(
        {"role": "user", "content": "We moved invoices to the new ledger."},
        {"role": "assistant", "content": "Noted; I'll update the export job."},
    )
    print(s.transcript_stored)  # False when the namespace does not record
    await s.close()  # flushes the buffered turns to extraction

    await bot.briefs.save(
        project="billing",
        branch="ledger-move",
        sections={
            "goal": "Move invoices to the new ledger",
            "next_steps": ["Update the export job"],
        },
    )
```

`close()` 时的提取需要真实的 LLM 提供方才能找出事实；使用默认的 fake 提供方时，这些轮次仍会被记录。

<a id="settings"></a>

## 设置

| 设置                                                                          | 作用                                                                                                                                                                      |
| --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_CONVERSATION_SEARCH_MIN_SIMILARITY`                                   | `conversation.search` 的余弦相似度下限（[知识 → 对话](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#conversations)）。                                                     |
| `LOOM_CONVERSATION_SEARCH_EXCERPT_CHARS`                                    | 搜索命中结果或已保存消息列表实时显示的一轮对话的篇幅（[知识 → 对话](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#conversations)）。                        |
| `LOOM_CONVERSATION_TITLER_*`                                                | 标题生成 worker 的开关、运行周期、窗口和每轮处理上限（[知识 → 对话](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#conversations)）。                                  |
| `LOOM_RETENTION_CONVERSATIONS_DAYS`, `LOOM_TIERING_CONVERSATIONS_WARM_DAYS` | 对话记录的可选 TTL 和迁移到温存储卷的设置（[保留](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#retention)、[存储分层](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#storage-tiering)）。 |
| `LOOM_SESSION_BRIEF_LLM`, `…_LLM_MODEL`, `…_WINDOW_CHARS`, `…_MAX_WINDOWS`  | 简报的压缩方式（[上下文](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#context)）。                                                                                              |
| `LOOM_MCP_SESSION_RUN_IDLE_SECONDS`                                         | MCP 会话在这段时间内没有任何调用后，关闭其隐式运行（[MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#mcp)）。                                                           |

<a id="limits"></a>

## 限制

- **“我追加了内容，但什么都没有。”** 该命名空间已关闭记录功能，追加操作返回了 `stored: false`。请使用 `conversation.set_logging` 开启。
- **对话记录不会被提取。** 已记录的对话轮次不会自动变成记忆。
- **`conversation.search` 以向量检索为主。** embedding 未能捕捉的精确措辞可能排名靠后；`memory.recall` 会在提取所读取的对话轮次上进行词法检索和时间窗口读取。
- **brief 有长度限制。** 渲染后的 brief 保持在约 12,000 个字符以内；每个列表部分都有上限（例如 20 条决策、12 条死胡同、8 个后续步骤），因此较旧的条目会被丢弃。
- **brief 不是锁。** 同一分支上的两个会话会修改同一个 brief；如果不希望它们交错修改，请传入 `if_revision`。

<a id="related"></a>

## 相关内容

- [提取](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md) — 将对话记录转换为记忆。
- [工作记忆与档案](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md) — 插件同样会写入的“我进行到哪了”暂存块。
- [运行](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md) — 会话的写入和观察所归入的运行台账。
- [统一搜索](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md) — `loom_search` 的对话部分。
- [追踪与恢复](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md) — 恢复一个长时间运行的任务。
- [记忆架构](https://github.com/ClickHouse/loom/blob/main/docs/architecture/memory-architecture.md) — 从内部视角看对话表和简报表。
