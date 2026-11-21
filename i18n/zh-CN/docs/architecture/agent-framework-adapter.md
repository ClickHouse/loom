<!-- i18n: source=docs/architecture/agent-framework-adapter.md blob=6a5670ddd5050886dcda1b0bf590a83063c1e4aa model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agent-framework-adapter.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agent-framework-adapter.md), which is authoritative.

<a id="microsoft-agent-framework-context-provider"></a>

# Microsoft Agent Framework 上下文提供者

`LoomContextProvider` 基于异步 Python SDK 实现了该框架的 context-provider 接口。可选的 extra 仅允许经过测试的框架次版本系列；核心 SDK 和服务器不会导入该框架。

每次运行之前，provider 会搜索最新的用户文本，并将匹配的事实添加到框架的 instructions 中。运行结束后，它会把新的用户和助手文本分成有界的批次，发送给 `memory.set_from_messages`。该 hook 每个轮次只运行一次，包括在框架 middleware 循环中。框架自己保存历史记录；该 provider 存储的是持久事实，而不是取代 history provider。

<a id="invariants"></a>

## 不变式

用户和框架会话 id 共同派生出一个 Loom 会话，从而避免两个用户的同名会话共享提取出的 chunk。省略用户时，会读取令牌所允许的命名空间中的全部记忆，并写入共享行。显式指定的用户 id 前后不能带有空白字符。命名空间授权决定访问权限；user 参数只是一个过滤条件。服务器提供 actor 和来源溯源信息，并应用常规的冲突、擦除和滚动 schema 规则。不会增加任何数据库、缓存或本地历史记录。错误会通过框架的 provider hook 向上传播，包括模型回复之后发生的错误。并发写入遵循服务器通常的冲突规则。

<a id="source-entry-points"></a>

## 源码入口

- `sdk/python/loom/integrations/agent_framework.py`：`LoomContextProvider`。
- `sdk/python/tests/test_agent_framework.py`：原生 provider 和 agent 的行为。
- `tests/integration/test_sdk_agent_framework_memory.py`：实时提取与召回。

[框架设置](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#microsoft-agent-framework)
