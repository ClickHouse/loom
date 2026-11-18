<!-- i18n: source=docs/guides/extraction-pipeline.md blob=a8d7e423185b09c1fa986972aa01b8c1b70d77d8 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/guides/extraction-pipeline.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/extraction-pipeline.md), which is authoritative.

<a id="customize-the-extraction-pipeline"></a>

# 自定义提取流水线

`memory.set_from_messages` 会让对话记录依次经过一组有序的步骤，这些步骤共享同一个状态。请求可以通过 `pipeline_steps` 替换这组步骤：重新排序或跳过某些步骤，也可以调用由运维人员注册的进程内步骤。本页只介绍步骤列表；提取本身的行为请参阅[提取](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md)。

`pipeline_steps` 是 HTTP 请求体字段。它对 MCP 工具 schema 不可见，也不是 Python SDK 中 `memory.set_from_messages` 辅助函数的参数；请通过 HTTP 或 `call(...)` 发送。生成的 op 输入类型（`sdk/python/loom/ops.py`、`sdk/typescript/src/generated/ops.ts`）包含该字段。

<a id="the-default-pipeline"></a>

## 默认流水线

`server/memory/app/pipeline.py` 中的 `DEFAULT_PIPELINE`：

| 步骤                              | 作用                                                                                             |
| --------------------------------- | ------------------------------------------------------------------------------------------------ |
| `loom.fetch_namespace_categories` | 为提取器加载命名空间的标签分类体系。                                                             |
| `loom.fetch_existing_memories`    | 取最相近的已有记忆（最多 50 条），展示给提取器。                                                 |
| `loom.llm_extract`                | 执行提取调用。                                                                                   |
| `loom.drop_recall_echoes`         | 丢弃复述本会话已获取记忆的事实。                                                                 |
| `loom.drop_session_echoes`        | 丢弃复述本会话自身 Loom 调用所携带内容的事实。                                                   |
| `loom.drop_activity_narration`    | 丢弃对工具活动的叙述，这类内容没有持久的结果。                                                   |
| `loom.drop_non_durable`           | 编码会话：丢弃状态快照、日常事务和计划。                                                         |
| `loom.verify_durable`             | 编码会话：对剩余事实进行验证。                                                                   |
| `loom.hash_dedup`                 | 丢弃规范哈希已存储的事实。                                                                       |
| `loom.bulk_write`                 | 将 bot 规则（`about="agent"`）归入学习队列，其余通过 `memory.bulk_set` 写入。                    |

编码会话是指 `extra_tags` 带有编码 agent 来源标签（`source:claude-code`、`source:codex`、`source:cursor`）的对话记录，或轨迹回放。这两个编码步骤对其他对话记录原样放行。

**空的 `pipeline_steps` 会运行上面这组步骤；非空的则严格按顺序运行其中列出的步骤**，并共享同一个状态。省略 `loom.bulk_write` 意味着不会写入任何内容：响应仍会报告提取到的内容。

<a id="run-a-shorter-pipeline"></a>

## 运行更短的流水线

不使用去重上下文，直接提取并写入，适用于一次性导入已知全新的数据：

```bash
curl -s "$LOOM_BASE_URL/v1/memory.set_from_messages" \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' -d '{
  "org": "acme",
  "namespace": "support",
  "agent": "importer",
  "messages": [{ "role": "user", "content": "..." }],
  "pipeline_steps": [{ "step_id": "loom.llm_extract" }, { "step_id": "loom.bulk_write" }]
}'
```

这需要真实的 LLM provider（`LOOM_LLM_PROVIDER` 及其密钥）；默认堆栈中的假 provider 无法提取有意义的事实。

<a id="step-behaviour"></a>

## 步骤行为

每个描述符的形式为 `{"step_id": ..., ...}`；整个对象会作为配置传给该步骤。

- **每个请求最多 16 个步骤**。
- **`hook.*` 步骤会被拒绝**，返回 422 `VALIDATION_FAILED`：服务器不会代表调用方发起任何出站调用。
- **未知的步骤 id** 会被跳过，并在响应的 `pipeline_errors` 中以 `unknown_step:<id>` 报告。
- **失败时放行的步骤。** 失败的获取类步骤会将错误记录到 `pipeline_errors`（例如 `fetch_existing_failed:<error>`），流水线继续运行。
- **`loom.llm_extract` 失败时中止**：提取出错会使请求以 502 终止，因此不会在对话记录得到 `written=[]` 的情况下悄悄返回。它的描述符接受 `{"judge": true}` 以开启评判关卡；对默认步骤列表，请求上的 `judge_extraction=true` 效果相同。
- **耗时。** 响应的 `stage_ms` 包含每个步骤的墙钟耗时（`step.<step_id>`）。每个步骤还会在名为 `pipeline.step` 的 OpenTelemetry span 中运行，步骤 id 作为其属性。

<a id="register-an-in-process-step"></a>

## 注册进程内步骤

运维人员在服务器进程启动时导入的代码中注册步骤：

```python
from server.memory.app.pipeline import WorkflowState, register_step


async def spam_filter(state: WorkflowState, config: dict) -> WorkflowState:
    state.extracted_facts = [
        f for f in state.extracted_facts if "spam" not in str(f.get("value", "")).lower()
    ]
    return state


register_step("operator.spam_filter", spam_filter)
```

之后请求即可使用 `{"step_id": "operator.spam_filter"}`；描述符中的其他键会作为 `config` 传入。该步骤在进程内运行，对整个 `WorkflowState` 拥有读写权限：身份、消息、已有记忆、提取出的事实、计数器。`register_step` 会替换同一 id 下已有的注册，因此运维人员的步骤也可以替换内置步骤。

替换 `loom.bulk_write` 的步骤，写入的内容中不得包含 `about="agent"` 的事实：这些是 bot 的规则，由内置步骤归入学习队列。

> **警告：** 丢弃或改写 `extracted_facts` 的步骤，会改变每个指定它的调用方所存储的内容。放在 `loom.bulk_write` 之后的步骤可以在 `state.written` 中看到已写入的行，但无法撤销写入。

若要向 Loom 本身添加内置步骤，请在 `server/memory/app/pipeline.py` 中添加该函数及其 `register_step` 调用，将其放入 `DEFAULT_PIPELINE`，并在改动前后各运行一次 `make bench-quick`：默认步骤列表决定着每一次提取的结果。

<a id="limits"></a>

## 限制

- 步骤按顺序执行，不支持分支或并行。
- 默认步骤列表是全局的，不支持按命名空间配置流水线。
- 没有步骤级重试。

<a id="related"></a>

## 相关内容

- [提取](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md)
- [扩展 Loom](../development/extending-loom.md)
- [写入记忆](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)
