<!-- i18n: source=docs/architecture/agent-framework-adapter.md blob=d7c22dbc73bf2a6dd64de2cb8471a3fafc9d76ba model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agent-framework-adapter.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agent-framework-adapter.md), which is authoritative.

<a id="microsoft-agent-framework-context-provider"></a>

# Microsoft Agent Framework 上下文提供者

`LoomContextProvider` 基于异步 Python SDK 实现了该框架的上下文提供者接口。可选的 extra 只允许经过测试的框架次版本系列；核心 SDK 和服务器不会导入该框架。

运行之前，提供者会搜索最新的用户文本，并把匹配的事实添加到框架的指令中。运行之后，它会把新的用户文本和助手文本分成有限大小的批次，发送给 `memory.set_from_messages`。每个轮次中该钩子只运行一次，包括在框架的中间件循环中。框架会保留自己的历史记录；本提供者存储持久事实，而不是取代历史记录提供者。

<a id="invariants"></a>

## 不变量

用户 id 与框架会话 id 共同派生出一个 Loom 会话，以防两个用户的同名会话共享提取出的分块。省略用户时，会读取令牌允许访问的命名空间中的全部记忆，并写入共享行。显式指定的用户 id 不能带有首尾空白。命名空间授权决定访问权限；user 参数只是过滤条件。服务器提供 actor 和来源溯源信息，并应用常规的冲突、擦除和滚动 schema 规则。不会添加数据库、缓存或本地历史记录。错误会通过框架的提供者钩子向上传播，包括模型回复之后出现的错误。并发写入遵循服务器的常规冲突规则。

<a id="conversation-protection"></a>

## 对话保护

一个提供者只服务于一个逻辑对话族。它会保留构造时和回调入口处已有的、由应用拥有的保护句柄，包括环境中的贡献者以及之后每个绑定的客户端句柄。原生的用户 id 和会话 id 只用于路由请求，不会创建或替换这一授权。恢复时，会在构造提供者之前重新打开现有的 capsule。

自有的召回和提取在 SDK 的自动捕获上下文中运行。传输层会在凭据检查之后、分发之前重新检查每个保留的对话族，在转换之前观察回复，并在放行带标记的回复之前，将所有贡献者持久地标记。第一次带标记的召回可以到达模型。之后的自动工作会被拒绝，并且每个提取批次都会在下一个批次开始之前接受检查。模型回复之后出现的拒绝会通过原生提供者生命周期向上传播；它无法撤销已给出的回答或工具产生的效果。提供者不会添加能力声明，也不会添加恢复后宿主的资格认定。

<a id="source-entry-points"></a>

## 源码入口

- `sdk/python/loom/integrations/agent_framework.py`：`LoomContextProvider`。
- `sdk/python/tests/test_agent_framework.py`：原生提供者和 agent 的行为。
- `tests/integration/test_sdk_agent_framework_memory.py`：实时提取与召回。

[框架设置](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#microsoft-agent-framework)
