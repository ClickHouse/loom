<!-- i18n: source=docs/architecture/source-push.md blob=3114dcae551415671efdfa6f880845f601eb7342 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/architecture/source-push.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/source-push.md), which is authoritative.

<a id="document-source-push-notifications"></a>

# 文档源推送通知

需要设置 `LOOM_KNOWLEDGE_SOURCE_PUSH_ENABLED=true`，才能根据提供方通知请求文档源同步。通知不会写入文档内容，也不会更改文档源的访问策略。常规的 15 分钟轮询仍然保持启用，是丢失投递后的恢复途径。禁用推送会暂停设置、投递和续期，但不会阻止密钥重新加密（reseal）或设置退役。

<a id="source-entry-points"></a>

## 源码入口

- `server/knowledge/api/ops_source_push.py`（`OPERATIONS`）声明管理员的设置、确认和状态操作。
- `server/knowledge/app/source_push_routes.py`（`notification`）限制回调请求体的大小，并分发提供方的投递。
- `server/knowledge/app/source_push.py`（`receive`、`_fresh`、`_put`、`renew_drive`、`reseal_registry`）对提示进行身份验证，并协调注册表生命周期的写入。
- `server/knowledge/domain/source_push.py`（`fingerprint`、`verify_drive`、`verify_notion`、`verify_linear`）绑定设置并验证提供方的证明；`server/knowledge/store/source_push.py`（`get`、`put`）持久化注册表版本。
- `server/knowledge/app/sources.py`（`sources_lease`、`deactivate_namespace_sources`）协调文档源的生命周期变更；`server/erasure/store/steps_ledgers.py`（`delete_source_push_by_namespace`）完成注册表的擦除。

<a id="invariants"></a>

## 不变量

- 通过身份验证的投递经由现有的文档源路径请求同步。提供方的负载不能写入内容，也不能扩大文档源的访问范围。
- 投递必须与当前的文档源 ID、设置代次（generation）和凭据、条目以及配置指纹相匹配。文档源租约和顺序读取用于隔离文档源的替换、退役以及提供方的迟到响应。
- 推送密钥使用绑定用途的加密封装。只有管理员可以查看待确认的质询；普通读取、审计主体和幂等重放都不包含它。
- 注册表写入与命名空间擦除共用一个带围栏的写入者租约。擦除会删除所有已存储的版本，而发布设置要求整个集群已确认支持该擦除步骤。

<a id="registration-and-authentication"></a>

## 注册与身份验证

推送注册表独立于文档源注册表和同步游标。每一行把组织、命名空间、文档源 ID、提供方和随机的设置代次，绑定到文档源凭据与条目的指纹上。配置是管理员操作，并且必须显式配置 HTTPS 公共基础 URL。

回调 URL 的路径中包含命名空间、文档源 ID 和设置代次，而确切的组织名放在必需的 `org` 查询参数中。查询编码会保留含有斜杠或 URL 保留字符的组织名。回调在记录同步请求之前，会在命名空间的文档源租约下重新读取注册表和当前文档源。文档源已被移除或替换、凭据已变更、条目集合已变更、命名空间处于非活动状态，或设置代次已退役时，投递都会被拒绝。回调的负载和请求头既不存储也不记录日志。

- Drive 渠道使用相互独立的随机渠道 ID 和加密封装的渠道令牌。投递必须在过期前与活动渠道的 ID、令牌和资源 ID 相匹配。这是渠道令牌验证：Drive 不会对这些投递签名。过早到达的 `sync` 通知会被确认，但不会请求工作。只有重新检查设置代次之后，监视（watch）响应才会被激活。
- Notion 设置会为新的设置代次捕获一个有大小限制并加密封装的验证质询。设置处于待确认状态时，只有管理员可以查看它。管理员在提供方界面中验证订阅后，确认该确切的设置代次和质询指纹。未签名的质询不能激活订阅，也不能请求同步。活动状态下的投递要求原始请求体的 HMAC 以及已配置的工作区身份。
- Linear 设置首先给出一个待确认的通知地址。管理员在该地址创建 webhook，然后用其签名密钥确认同一设置代次。投递要求原始请求体的 HMAC、已签名请求体中的毫秒级时间戳在一分钟之内，以及已配置的组织身份。

提供方身份在设置期间使用文档源现有的凭据读取。通知请求体有固定的大小上限。重复的有效通知会与现有的待处理同步请求合并；正在等待的完整同步会被保留。成功的回调会在四秒的处理预算内返回 HTTP 200。

<a id="drive-coverage-and-renewal"></a>

## Drive 覆盖范围与续期

Drive 的用户变更渠道并不覆盖所有共享云端硬盘的变更。因此，设置会为用户语料以及账号所属的每个共享云端硬盘创建渠道，最多 19 个云端硬盘。完整且有边界的成员列表可避免把直接共享文件的云端硬盘 ID 误认为成员关系：非成员的直接共享条目使用用户日志。如果语料集合不完整或超出上限，设置会拒绝，而不会声称推送覆盖完整。把文件移动到新的语料中可能需要重新设置；轮询仍然能发现该变更。

一个有边界、默认关闭的 worker 会在渠道过期前为其续期。它创建一个新的唯一渠道，验证响应和文档源的设置代次，然后在文档源租约下完成替换，并尽力停止旧渠道。续期失败时轮询仍然可用，并且过期的渠道会在设置状态中显示。

配置目录控制提供方工作、单个渠道停止、总清理以及 worker 单轮的期限。它们的默认值分别是 40 秒、2 秒、5 秒和 120 秒。一轮在没有足够时间完成一次提供方尝试和两个清理窗口时，就不再开始新的续期。如果提供方工作加清理的耗时可能超出 120 秒续期租约内的 48 秒工作窗口，或者单轮预算不留出开始续期的时间，配置会被拒绝。渠道停止期限不能超过总清理窗口。

<a id="lifecycle-and-storage"></a>

## 生命周期与存储

注册表按组织、命名空间和文档源 ID 排序，因此命名空间读取可以裁剪物理键范围。维护扫描也按同样的顺序推进。注册表存储完整的身份元组、由服务器标记的操作者和派生的来源信息。密钥使用绑定用途的加密封装，并参与重新加密。普通的文档源读取和审计主体从不包含令牌或质询。管理员的设置读取会绕过幂等重放存储，因此已显示的待确认质询在激活后不能从普通缓存中被重放。

文档源移除、凭据轮换、条目更新和命名空间退役，都会在同一个文档源租约下使推送设置退役。外部监视响应迟到时，不能使已退役的设置复活。命名空间擦除通过现有的已结算擦除机制删除注册表的所有版本。配置写入要求集群能够识别该注册表的擦除步骤；集群状态无法读取时会拒绝。在回滚到不含此注册表的构建之前，请先禁用推送，用当前构建擦除注册表的各行，并确认命名空间擦除已完成。无法停止的提供方渠道会自行过期；已退役的回调会拒绝它们。

<a id="alternatives"></a>

## 替代方案

把推送状态放进文档源的行中，会使每次投递都与凭据写入竞争，并有带着旧凭据继续使用的风险。把它放进同步详情，则会把加密封装的设置密钥与普通检查输出混在一起。独立的注册表避免了这两种问题。通知只是提示，因此不需要持久化的事件队列、提供方负载归档和第二条摄取路径。

<a id="provider-contracts"></a>

## 提供方约定

- [Drive 推送通知](https://developers.google.com/workspace/drive/api/guides/push)
- [Drive 用户日志与共享云端硬盘日志](https://developers.google.com/workspace/drive/api/guides/about-changes)
- [Notion webhook 验证](https://developers.notion.com/reference/webhooks)
- [Notion 事件投递](https://developers.notion.com/reference/webhooks-events-delivery)
- [Linear webhook 验证](https://linear.app/developers/webhooks)
