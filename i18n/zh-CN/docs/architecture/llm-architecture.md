<!-- i18n: source=docs/architecture/llm-architecture.md blob=e4be5f935957cae12d8a7a0579f2dbb45846a398 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/architecture/llm-architecture.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/llm-architecture.md), which is authoritative.

<a id="llm-and-embedding-architecture"></a>

# LLM 与 embedding 架构

Loom 有两个可插拔的提供方：embedder（`server/embedding/`）负责把文本转换为向量，LLM 客户端（`server/llm/`）负责补全提示词。两者都在进程启动时根据设置选定一次。当密钥缺失时，两者都会回退到确定性的伪实现，因此没有密钥的安装也能启动并作出响应。运维人员的配置方法见[部署](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)；所有设置见[配置参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md)。

<a id="goals-and-non-goals"></a>

## 目标与非目标

| 目标                                                       | 非目标                                                   |
| ---------------------------------------------------------- | -------------------------------------------------------- |
| 写入路径上的每道正确性检查都无需 LLM 即可运行。            | 通用的 LLM 网关。Loom 只为自身功能调用模型。             |
| 未配置密钥的安装也能启动，并明确提示当前运行在模拟实现上。 | 与提供方无关的提示词。提示词针对特定模型调优，更换后会重新评测。 |
| 更换 embedder 不会悄悄导致召回校准出错。                   | 混用向量空间。来自不同模型的向量不会相互比较。           |
| 每次提供方调用都会归属到发起它的 op 或 worker 轮次，并记录其成本。 | 运行时提示词管理。提示词就是代码。                       |

<a id="embedders"></a>

## 嵌入器

<a id="selection"></a>

### 选择

`server/embedding/embedder.py`（`provider_for`、`get_embedder`）在每个进程中选定一次所使用的嵌入器：

1. 首先解析标签。`prefix:<dim>:<model>` 和 `ctx[<variant>]:<model>` 是建立在所用嵌入器之上的视图，不能作为所用模型。
2. 若设置了 `LOOM_EMBEDDING_PROVIDER`（`fake`、`local`、`openai`、`gemini`），则使用它；其他任何值都会抛出错误。
3. 否则根据 `LOOM_EMBEDDING_MODEL` 推断：`fake-ngram*` 为 fake，`local` 或 `local:*` 为 local，`text-embedding-*` 为 OpenAI，`gemini*` 为 Gemini。其他任何名称都会抛出 `unknown embedding model`。

| 提供方 | 模块                       | 模型名称                                                                | 依赖                                    | 说明                                                                     |
| -------- | ---------------------------- | -------------------------------------------------------------------------- | ---------------------------------------- | ------------------------------------------------------------------------- |
| `fake`   | `server/embedding/fake.py`   | `fake-ngram-<dim>`                                                         | 无                                  | 哈希字符三元组，经 L2 归一化。结果确定；为开发环境的默认值。 |
| `local`  | `server/embedding/local.py`  | `local` (`bge-small`), `local:bge-small`, `local:bge-base`, `local:minilm` | `local-embedder` extra（`fastembed`） | CPU ONNX；维度分别为 384、768 和 384。无需网络。                        |
| `openai` | `server/embedding/openai.py` | `text-embedding-3-small`, `text-embedding-3-large`                         | `OPENAI_API_KEY`                         | 请求 `dimensions=LOOM_EMBEDDING_DIM`。                                 |
| `gemini` | `server/embedding/gemini.py` | `gemini-embedding-001`                                                     | `GEMINI_API_KEY` 或 `GOOGLE_API_KEY`     | 请求 `outputDimensionality=LOOM_EMBEDDING_DIM`（最高 3072）。          |

**维度。** 所用维度为 `LOOM_EMBEDDING_DIM`（默认 3072）。OpenAI 和 Gemini 会按该维度请求；请选择支持该维度的模型。local 模型输出其原生维度。启动时会探测实际维度（`probe_embedding_dim`）；若不一致，会在 `/v1/health` 中显示为 `embedding_dim_mismatch`。

**镜像构建。** Docker 镜像安装了 local 嵌入器，并附带 `bge-small` 的文件，这些文件固定到某一修订版本和 sha256（`server/embedding/local_models.py`）；栈中的 `env.example` 使用它来生成嵌入。

<a id="protocol-and-paths"></a>

### 协议与路径

`Embedder` 协议包含 `model_name`、`dim`、`async embed(text)` 和 `async embed_many(texts)`。

| 路径                 | 发送内容                      | 使用方                                                                  |
| -------------------- | -------------------------- | ------------------------------------------------------------------------ |
| `embed`（查询）      | 经空白归一化的文本 | 搜索。有缓存。                                                          |
| `embed_many`（写入） | 原始文本                   | 每一个存储的向量：`memory.set`、提取、摄取、恢复 worker。 |

- **缓存**（`server/embedding/_cache.py`）。查询依次从以下位置获取结果：每进程的 LRU（`LOOM_EMBEDDING_CACHE_SIZE`、`LOOM_EMBEDDING_CACHE_TTL_S`），针对同一文本的进行中调用，`loom.embedding_cache`（文本长度不超过 `LOOM_EMBEDDING_CACHE_SHARED_MAX_CHARS`，且仅匹配 `dim`），最后才是提供方。缓存键对请求所属的组织、模型和归一化文本做哈希，因此一个组织不会拿到另一个组织的向量，也不会与其共享进行中的调用：否则较快的响应会告诉它，另一个组织嵌入过相同的文本。请求之外的调用（worker、CLI、启动探测）没有可用于划定范围的组织，会跳过缓存：worker 的身份带有它最近处理的那一行所属的组织，这不能作为下一段文本的范围。批量条目使用另一个基于原始文本的键，因此两条路径不会互相命中缓存。缓存失败时会回退到提供方。
- **批量。** OpenAI 和 Gemini 每次请求最多接受 100 个输入；`memory.set` 在一次请求中同时嵌入值和键。
- **第二向量通路。** `prefix:<dim>:<model>`（Matryoshka 前缀，`server/embedding/prefix.py`）和 `ctx[<variant>]:<model>`（所用模型作用于改写后的文本，`server/embedding/alias.py`）仅在列于 `LOOM_CODE_SEARCH_VECTOR_MODELS` 时才会构建。次级嵌入器绝不会回退到 fake；缺少密钥时会抛出错误。

<a id="provider-calls"></a>

### 提供方调用

OpenAI 和 Gemini 共用 `server/embedding/_provider.py`：

| 关注点      | 行为                                                                                                                                                                                                                                                                   |
| ------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 重试        | 传输错误以及 408、409、425、429、500、502、503、504；`LOOM_EMBEDDING_RETRY_ATTEMPTS`（3）次尝试，指数退避（基数 0.5 s，上限为 `LOOM_EMBEDDING_RETRY_MAX_WAIT_S`，10 s）。                                                                                   |
| 输入检查  | 先对输入统计 token（OpenAI 为 8191 个 token，Gemini 为 2048 个）；输入过长时会抛出错误，而不是被截断。                                                                                                                                              |
| 输出检查 | 空向量、维度不匹配或非有限值会抛出 `MalformedEmbedding`。                                                                                                                                                                                        |
| 超时     | 共享 HTTP 客户端使用 `LOOM_HTTP_CLIENT_TIMEOUT_S`（20 s；连接使用 `LOOM_HTTP_CLIENT_CONNECT_TIMEOUT_S`，5 s）。查询路径每次尝试使用 `LOOM_MEMORY_SEARCH_EMBED_BOUND_S`（2.5 s），重试次数为 `LOOM_MEMORY_SEARCH_EMBED_BOUND_RETRIES`（1），之后返回 503 `EMBED_TIMEOUT`。 |
| 健康状态       | 调用结果会上报到 `/v1/health`。取消不算提供方故障。                                                                                                                                                                                                     |

<a id="changing-the-embedder"></a>

### 更换嵌入器

更改 `LOOM_EMBEDDING_MODEL` 或 `LOOM_EMBEDDING_DIM` 会改变向量空间。已存储的向量带有各自的 `embedding_model`，搜索从不跨模型比较，因此旧的行在重新嵌入之前将不再匹配。

`loom reembed`（`server/memory/app/reembed.py`，`reembed_all`）会重写 `memory_embeddings`、`entity_vectors` 和 `conversation_embeddings` 中 `embedding_model` 与当前所用模型不同的每一行。它根据已存储的内容重新生成向量，并保留其他所有列。在 `memory_embeddings` 和 `conversation_embeddings` 中，模型属于排序键，因此新旧行会并存，直到使用 `--delete-old`。`entity_vectors`（`ReplacingMergeTree(created_at)`）则就地替换。参数：`--org`、`--namespace`、`--batch-size`、`--dry-run`、`--key-only`、`--resume`，以及 `--in-database <named
collection>`，后者通过 ClickHouse 的 `aiEmbed` 为每张表执行一次 `INSERT … SELECT`（需要包含该函数的 ClickHouse 版本；当设置了客户或身份请求头，或要求使用网关时会被拒绝，因为这些调用不指明调用者，并会跳过网关的检查）。参见 [CLI 参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md)。

`embedding_worker` 负责其余部分。它按从新到旧的顺序嵌入 `embedding_dim = 0` 的占位行；带有旧模型标签的占位行会在当前所用模型下重新嵌入，旧占位行则被标记为删除。没有嵌入行的记忆会先在当前所用模型下获得一个占位行，并复制其 `status`、`invalid_at` 和证明信息。tracker 轮次会重新嵌入由其他模型写入的行。

<a id="adaptive-thresholds"></a>

## 自适应阈值

同样是 0.4 的余弦相似度，对不同的 embedder 含义并不相同。搜索门限（相似度下限、abstain、HyDE 回退、查询改写、跳过 rerank）和写入门限（冲突、重复）都是余弦阈值，因此更换模型会使所有这些阈值发生变化。

当 `LOOM_ADAPTIVE_THRESHOLDS` 开启时（默认开启），`server/memory/app/embedder_calibration.py` 中的 `calibrate_embedder` 会在启动时推导这些阈值。它对三个内置语料（释义对、问题到事实对、干扰对）进行 embedding，并读取它们的余弦分布（`derive_thresholds`）：

| 阈值 | 推导方式 |
| ------------- | ------------------------------------------ |
| 噪声下限 | 干扰对 p90 + 0.02 |
| abstain | 噪声下限 + 0.05 |
| 查询改写 | max(问题-事实 p10, 噪声下限 + 0.01) |
| HyDE 回退 | max(问题-事实 p25, 查询改写) |
| 跳过 rerank | max(问题-事实 p75, HyDE 回退) |
| 冲突 | 释义对 p50 |
| 重复 | 释义对 p75 |

每次搜索会按三个层级依次解析阈值：

1. 命名空间级校准（`loom.namespace_calibration_result`，由 `code.calibrate` 写入）；
2. 集群校准（`loom.embedder_calibration`，每个 embedder 模型一行）；
3. 标量设置（`LOOM_MEMORY_SEARCH_MIN_SIMILARITY`、`LOOM_MEMORY_SEARCH_ABSTAIN_THRESHOLD`、`LOOM_MEMORY_SEARCH_HYDE_FALLBACK_THRESHOLD`、`LOOM_MEMORY_SEARCH_QUERY_REWRITE_THRESHOLD`、`LOOM_LLM_RERANK_SKIP_ABOVE`、`LOOM_CONFLICT_THRESHOLD`）。

`calibration.get` 和 `/v1/health`（`adaptive_thresholds`）会显示当前生效的阈值；在使用相同门限的所有副本上，`adaptive_thresholds.calibration_hash` 都相同。

- 所有副本使用同一份校准。启动时，副本会采用其 embedder 在 `loom.embedder_calibration` 中的那一行，只有在没有该行时才进行探测（第一个启动的副本会发布它的探测结果）。每个副本每隔 `LOOM_CALIBRATION_REFRESH_SECONDS`（60）秒重新读取该行，并采用已变更的内容；启动时探测失败的副本会在这个周期再次探测。各命名空间的行会缓存 60 秒，因此在一个副本上运行的 `code.calibrate` 会在一分钟内对所有副本生效。
- 假的 embedder 会跳过校准并使用标量门限。探测失败时也是如此，它不会导致启动失败。
- 如果关闭自适应，且使用的是 `text-embedding-3-small` 以外的真实 embedder（标量门限正是针对该模型调优的），启动时会记录一条警告。重新进行基准测试由你负责。
- `recalibration` worker（每周运行，由 `LOOM_RECALIBRATION_ENABLED` 控制，运行在 leader 上）会重新运行探测，将结果发布为集群校准，在漂移达到 0.05 或更大时发出警告，并对至少包含 5 个相似对和 3 个干扰对的命名空间语料重新探测。

**原因。** 根据模型自身的分布推导阈值，使更换 embedder 只是一次配置变更。代价是：这些阈值是统计得出的。如果某个命名空间语料中的“相似”对是修订版本而不是释义，就会把冲突和重复门限设得过低。

<a id="llm-clients"></a>

## LLM 客户端

<a id="selection-1"></a>

### 选择

`server/core/llm_provider.py` 中的 `resolve_llm_provider` 负责选择提供方：

1. 显式设置的 `LOOM_LLM_PROVIDER` 永远不会被覆盖。
2. 否则根据 `LOOM_LLM_MODEL` 判断：`claude*` 为 `anthropic`；`gpt*`、`o1*`、`o3*`、`o4*`、`chatgpt*` 为 `openai`；`fake*` 为 `fake`。
3. 否则根据已存在的密钥判断：有 `OPENAI_API_KEY` 时选 `openai`，模型为 `gpt-6-luna`；其次有 `ANTHROPIC_API_KEY` 时选 `anthropic`，模型为 `claude-haiku-4-5`。
4. 否则为 `fake`（`fake-echo-1`）。

`Settings` 在加载时解析一次；`settings.llm_provider` 始终是具体的值，`llm_provider_inferred()` / `llm_provider_source()` 会说明它是如何得出的。如果提供方是推断出来的，启动时会记录一行 INFO 日志。缺少密钥时，`loom doctor` 的 `llm_provider` 一行会给出警告。默认的 `.env` 固定设置 `LOOM_LLM_PROVIDER=fake`。

| 提供方    | 模块                    | 密钥                                                  |
| ----------- | ------------------------- | ---------------------------------------------------- |
| `openai`    | `server/llm/openai.py`    | `OPENAI_API_KEY`                                     |
| `anthropic` | `server/llm/anthropic.py` | `ANTHROPIC_API_KEY`                                  |
| `local`     | `server/llm/local.py`     | 无：使用指向 `LOOM_LOCAL_LLM_BASE_URL` 的 OpenAI 客户端 |
| `fake`      | `server/llm/fake.py`      | 无                                                   |

各阶段的模型（`LOOM_MEMORY_ASK_SYNTH_MODEL`、`LOOM_CODE_SUMMARY_MODEL` 以及其他 `*_model` 设置）通过 `get_llm_client_for_model` 构建各自的客户端，并根据名称推断提供方；如果再次写出部署自身的 `LOOM_LLM_MODEL`，则在已解析的提供方上运行（`Settings.provider_of`），因此名称看起来像托管模型的本地模型仍然保持本地运行。

<a id="protocol"></a>

### 协议

`LLMClient` 有 `provider`、`model` 和一个方法：

```python
async def complete(system, user, max_tokens=1024, response_format=None) -> LLMResult
```

`LLMResult` 包含文本、输入和输出 token、缓存输入和缓存写入 token、实际作答的提供方和模型，以及 `from_cache`。不支持工具调用。

**结构化输出。** 调用方传入 `response_format={"type": "json_object"}`。OpenAI 客户端会直接透传。Anthropic 客户端没有 JSON 模式：它会在系统提示词后追加一条 JSON 指令，去除代码围栏，并将系统块标记为可缓存。解析由调用方完成（`server/llm/json_text.py`）。

**按模型学习请求形态。** 请求内容不由模型名称决定。每个客户端先构建它期望的请求（temperature、最低的推理强度、输出上限），然后 `server/llm/traits.py` 根据提供方的响应进行学习，每个模型在每个进程中学习一次：

- 如果返回 400 并指出模型不接受某个字段（`temperature`、`reasoning_effort`、`output_config`，或以另一个名称出现的上限字段），则去掉该字段：重新发送不含该字段的调用，之后的调用也不再带上它；
- 如果返回 400 并拒绝某个推理强度，则选用其列出的最低强度（`LOOM_LLM_OPENAI_REASONING_EFFORT` 可改为固定某个强度）；
- 如果返回 400 并指出模型的输出上限，则将上限限制到该值；
- 推理和思考 token 会计入输出上限，因此回复中报告的这部分 token 数会成为该模型的预留量（见过的最大值的两倍，最多为 `LOOM_LLM_HIDDEN_REASONING_RESERVE_MAX`），并加到每个调用方的上限上。如果回复触及上限却没有可见文本，则以上限最大值作为预留量重新发送一次。

因此，新模型无需修改代码；最初的一两次调用就会让进程了解它的要求。`LOOM_LLM_ANTHROPIC_EFFORT` 会向 Claude 模型发送思考强度。

<a id="retries-timeouts-fallback"></a>

### 重试、超时与回退

- **重试**（`server/llm/_retry.py`）：针对传输错误以及 429、500、502、503、504、529；最多尝试 `LOOM_LLM_RETRY_ATTEMPTS`（4）次，采用指数退避（基数 0.5 秒，上限为 `LOOM_LLM_RETRY_MAX_WAIT_S`，20 秒）。请求超时 `LOOM_LLM_REQUEST_TIMEOUT_S` 为 300 秒：它只用于捕获挂起的连接，因此模型响应慢或输出很长都不会触发它。需要更短超时的调用方可自行包装调用（`memory.search` 上的 HyDE 等待 `LOOM_MEMORY_SEARCH_HYDE_TIMEOUT_S`，6 秒）。
- **回退模型。** `LOOM_LLM_FALLBACK_MODEL` 会将客户端包装在 `FallbackLLMClient` 中（`server/llm/fallback.py`）。发生服务中断（传输错误、可重试状态码、超时）或请求被拒绝（400、413、422）时，它会把调用交给第二个模型；401、403 和 404 则直接向上抛出。每次切换都会记录日志并计数（`loom.llm.fallback`），结果中会注明实际作答的模型。回退不会串联。
- **缺少密钥。** 客户端为 fake（`_build_llm_client`），并以 WARNING 级别记录日志。
- **某个身份被拒绝。** 设置了身份请求头时，来自网关主机、且与 `LOOM_INFERENCE_IDENTITY_REFUSALS_JSON` 所列响应模式相匹配的响应，表示网关拒绝了该调用的身份或要求其等待（[身份请求头](#identity-headers)）：该调用不会重试，也不会交给回退模型。

<a id="response-cache"></a>

### 响应缓存

`CachedLLMClient`（`server/llm/cache.py`）将补全结果缓存在 `loom.llm_response_cache` 中（TTL 为 7 天）。缓存键按模型区分，并涵盖客户端所服务的组织、系统提示词、用户提示词、`max_tokens`、`response_format` 和额外参数，因此一个组织不会得到另一个组织的答案；未指定组织构建的客户端会直接调用提供方。只有来自所请求模型的非空答案才会被存储，因此回退模型的答案不会被缓存。每一行都记录了提示词中包含其文本的记忆键（`window_keys`），所以擦除和脱敏时可以按键删除。用户擦除时，还会删除包含该用户记忆摘要的行（带有 `consolidated` 和 `src:<key>` 标签），因为这类摘要会在另一个身份的键下引用这些记忆。

| 使用方         | 开关                                                                                                    |
| ------------ | --------------------------------------------------------------------------------------------------------- |
| `memory.ask` | `LOOM_MEMORY_ASK_LLM_CACHE_DEFAULT`（开启）、`LOOM_MEMORY_ASK_LLM_CACHE_NAMESPACES`、每个请求的 `llm_cache` |
| `code.ask`   | `LOOM_CODE_ASK_LLM_CACHE`（开启）；temperature 非零时会绕过缓存                                        |
| 抽取   | 关闭，除非 `LOOM_LLM_POLICY_BY_TRAFFIC_CLASS` 将其启用（默认对 `bench` 类启用）            |

<a id="the-customer-of-a-model-call"></a>

## 模型调用的客户

设置 `LOOM_INFERENCE_CUSTOMER_HEADER` 后，每个发往模型的请求（chat、embedding、rerank、classify）都会带上其数据所属的组织；设置 `LOOM_INFERENCE_FEATURE_HEADER` 后，还会带上发起该请求的功能（`server/llm/customer.py`）。这些请求头由发送方自行添加：共享的 HTTP 客户端也承载不属于模型的调用，这类调用不指明客户。

- **组织从哪里来。** 请求：传输层在令牌验证后记录的身份（`observability.genai`），即该 op 所指向的组织。worker 或 CLI：在调用外层使用 `customer.for_org(org)`，组织取自它所处理的行；带 `--org` 的 CLI 命令以该组织为范围。worker 自身的身份不计入，因为一轮处理会记下它最后处理的那一行的组织，而下一行的调用不能沿用它。部署自身的调用（启动时的 embedder 探测、重新校准 worker 的固定配对、provider 检查）在 `customer.system()` 下运行，并使用 `LOOM_INFERENCE_SYSTEM_CUSTOMER` 作为名称。抽样的检索评判会发送被评判组织的查询和记忆文本，因此它使用该组织的名称。`code.refresh` 会运行轮询器的摄取流程，它的调用仍归属调用方（其令牌的 subject），而轮询器自己的调用归属命名空间。
- **每次调用一个客户。** 批量处理多个组织的行的作业（embedding worker、`loom reembed`、模型重建）在调用前按组织拆分批次。
- **网关的命名。** `LOOM_INFERENCE_CUSTOMER_FORMAT` 把组织 id 转换为网关的客户 id，`LOOM_INFERENCE_FEATURE_MAP_JSON` 把 Loom 的功能名映射为网关的功能名。服务多个客户的 cell 应设置 `LOOM_INFERENCE_CUSTOMER_REQUIRED`，这样没有该请求头时它不会启动。
- **没有客户，就不调用。** 未指明客户的调用会在发送任何内容之前抛出 `Unattributed`（`MODEL_CALL_UNATTRIBUTED`）。遗漏的调用路径会明确失败，而不是记到无人名下。embedding worker（记忆和代码）会先做检查，对未指明组织的行立即放弃，记录该错误而不发起模型调用，因为重试无济于事，且该拒绝不属于 provider 故障。

- **被关闭的组织不发起调用。** `org.set_model_calls` 写入 `loom.org_model_calls`；`customer.require()` 在每个发送方中最先执行（先于网关令牌、embedding 缓存和 provider 健康检查），它把调用所属的组织与各进程持有的已关闭组织副本（`customer.switched_off_orgs`）比对。缓存的向量或回答同样会被拒绝：该开关是紧急关闭开关，因此任何由模型产生的内容都不会到达该组织。该副本最多每隔 `LOOM_MODEL_CALLS_POLICY_REFRESH_SECONDS` 读取一次，且信任时间不超过 `LOOM_MODEL_CALLS_POLICY_MAX_STALE_SECONDS`，超过后所有组织的模型调用都会被拒绝（`customer.SwitchUnread`）。作业对 SwitchUnread 拒绝的处理与对开关关闭的处理相同（`customer.HOLDS`）：其行保持原状且不消耗尝试次数，重建步骤会再次运行，fail-open 的 reranker、classifier 或 judge 则放行。当副本过旧，或本进程所做的更改尚未被读取时，调用会等待一次读取完成；否则使用手头的副本。每个作业在选取工作时都会排除已关闭的组织（在查询中排除，因此其行既不消耗尝试次数，也不会占满批次而挤掉其他组织）；代码摄取会跳过其仓库，冻结其整个代码索引。组织恢复时，早于清扫周期一小时的冲突标记已超出清扫范围，就像在故障中丢失的评判一样：写入按原样保留。该检查在 `require()` 中执行，先于 provider 健康记录，因此拒绝绝不会被计为 provider 故障。在没有客户头或身份头的情况下，作业中包含多个组织的批次是一次调用，除非其中某个组织已被关闭：这时 `customer.groups` 会按组织拆分，于是开关拒绝该组织的调用，其他组织继续。

**为什么用请求头，而不是每个客户一个令牌。** 网关已经把 Loom 当作一个服务（一个令牌）；为每个组织各发一个凭据，意味着要签发、轮换和吊销成千上万个，而网关能检查的内容并不会因此增加。请求头中的组织就是请求所指向的组织，并且已按其令牌获得授权。

<a id="identity-headers"></a>

### 身份请求头

`LOOM_INFERENCE_IDENTITY_HEADERS_JSON` 用来指明一次调用还为谁而发，适用于按 agent 或按个人计费或限流的网关（`server/llm/identity.py`）。它把请求头名称映射到模板；`customer.require()` 根据调用的归属渲染这些模板，发送方会把它们与客户头一起加到发往网关的每个 chat、embedding、rerank 和 classify 请求上。

| 占位符 | 请求 | 作业（`customer.for_org`） |
| ------------- | ------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| `{org}` | 它所指向的组织 | 该行的组织 |
| `{namespace}` | 传输层记录的命名空间 | 该行的命名空间 |
| `{subject}` | 令牌的 subject | 该行的写入者，否则为该行所在命名空间所属的 agent |
| `{agent}` | agent 的 subject（`sa:<trust>:<agent>`）中的 `<agent>` | 同左，取自该 subject |
| `{tenant}` | 其 trust 对 `LOOM_INFERENCE_TENANT_CLAIM` 所指声明所要求的唯一值 | 同左 |
| `{email}` | 个人的地址：subject，或其经验证的目录拼写 | 同左 |
| `{user_id}` | 请求的 `user_id`，否则为 subject | 该行的 `user_id`，否则为 subject |

- **为空则省略。** 模板渲染结果为空的请求头不会发送，因此单独的占位符在其值未知时会被省略；占位符周围的文本按原样发送。只有当请求头无法承载某个值时（空格、`%`、可打印 ASCII 之外的字符），才会对其做百分号编码。前缀声明（`tenant_id=acme-*`）不指明任何租户。
- **agent 信息成组发送。** 模板中含有 `{agent}` 或 `{tenant}` 的请求头要么一起发送，要么都不发送：只要其中任一占位符渲染为空（例如其 trust 不要求精确租户的 agent，或 trust 自身的令牌），这些头就都不发送，网关会应用其默认身份，而不是拒绝一半的身份。其他请求头各自独立。
- **没有组织，就不调用。** 设置了身份头后，未指明组织的调用会被拒绝（`MODEL_CALL_UNATTRIBUTED`，并记录日志），与客户头的情形相同，除非它是部署自身的调用（`customer.system()`）：不带 `--org` 运行的 `loom` 命令，或身份未被记录的请求，绝不会以网关默认身份发出。`loom` 命令同样会指明其 `--namespace`，因此它的调用是为该命名空间所属的 agent 发出的；当请求头指明 agent 时，带 `--org` 但没有命名空间的命令调用会同样被拒绝，而不是记到网关的默认 agent 名下。
- **写入者优先。** 作业的调用是为写入该行的主体发出的：即其 `actor` 中的 subject（`human:` 或 `process:` 加上令牌的 subject）。流水线自身的 actor（`process:extraction-worker`、`process:cli:<command>`、`provenance.PIPELINE_ACTORS`）不是主体。延迟执行的 `memory.set_from_messages` 会把其入队者存放在队列行上；实体提取器、embedding worker、投影修复和代码链接评判会读取它们所处理记忆的写入者。Observe-extract、profile 折叠和结构化 profile 会取其 prompt 可用的观察或事实中最新的主体写入者。版本决定这些写入的先后，因此一次更新算作一次新的写入。Reflection 和周期摘要取会话记忆中最新的写入者（`memory_repo.session_writers`）；冲突解决器取当前行的写入者。只有没有记录主体的行（对话轮次、共享范围的向量）才取其所在命名空间所属的 agent（归 `sa:<trust>:<agent>` 所有，即其 trust 在该 agent 首次令牌交换时创建的主目录）。只有部署自身的调用（`customer.system()`）不指明任何人。
- **每次调用一个身份。** 批量处理行的作业（embedding worker、`loom reembed`、模型重建）按它们渲染出的身份分组（`customer.groups`），因此一次调用绝不会携带两个 agent 的文本，而同一个 agent 在多个命名空间中的行共用一次调用。
- **读取。** trust 的声明（`loom.machine_identities`）、命名空间的所有者和账户的邮箱，每个进程每分钟最多读取一次。读取失败时使用手头的副本；若没有副本，则调用被搁置（`identity.IdentityUnread`，属于 `customer.HOLDS` 之一）：它会等待，而不是以别人的身份发出，作业会把该工作放置 30 秒，不消耗尝试次数。
- **只发给网关。** 身份头只发送给 `LOOM_INFERENCE_GATEWAY_HOSTS` 所列的主机，因此设置身份头需要同时设置它。模型客户端的请求钩子（`server/clients/_http.py`）会从任何其他请求中去掉它们，包括重定向的跳转：运营方的 reranker、命名空间的 classifier、Cohere 或其他地方的 provider 永远不会知道调用是为谁而发。reranker 和 classifier 同样通过模型客户端发送。
- **单个身份被拒绝。** `LOOM_INFERENCE_IDENTITY_REFUSALS_JSON` 列出的是来自网关主机的响应模式；来自网关主机且与其中某个模式匹配的回答，表示网关拒绝了该身份，而不是 provider 故障。每个条目指明一个状态码，以及错误体中（`error` 下）的 `code`、`type` 或 `message` 至少一项，各自要求精确匹配；消息中的单词不算数。默认配置按 code 匹配超出预算的 agent（`budget_exceeded`）、被拒绝的 agent（`agent_denied`）或身份指明不完整的 agent（`identity_incomplete`、`identity_malformed`），并对 `agent_request_limit` 选择等待；计量服务中 Anthropic 形式的预算回答不带 code，因此按其 type 和完整消息匹配。网关对自身故障也会给出的回答（`token_unavailable`、`identity_missing`）不在其内，因此健康检查仍能发现故障或配置错误。模型客户端的响应钩子（`gateway.answered`）对 `refused` 下的条目抛出 `ModelCallRefused`，于是请求返回 403 `MODEL_CALL_REFUSED`，并附带网关的 `gateway_status` 和 `gateway_code`；对 `wait` 下的条目抛出 `ModelCallDeferred`，返回 503 `MODEL_CALL_DEFERRED`，并附带网关的 `Retry-After`。若请求的调用被要求等待两秒或更短，它会等待一次，由共享的模型客户端重新发送，之后才会被延迟，重定向之后同样如此。两种回答都不会被进一步重试，也不会交给回退模型，`/v1/health` 不会从中读取任何内容，组织的开关也不受影响。两者都属于 `customer.HOLDS`，因此作业会保留这些工作而不消耗尝试次数。
- **作业的搁置。** 在该回答之后，作业（`customer.for_org`）对同一源、路径和模型上该身份的调用会被搁置：由请求钩子（`gateway.sending`）拒绝且不发送任何内容，拒绝后搁置 `LOOM_INFERENCE_REFUSAL_COOLDOWN_SECONDS`，等待后搁置该回答的 `Retry-After`（最多 5 分钟）。它发往其他模型或 embedding 的调用照常进行，其他身份的调用也一样。客户头是身份的一部分。该路径和模型上的一次成功（无论来自请求还是作业）会结束搁置。请求总是会询问网关，网关可能随时重新接受或重新放行先前被拒绝的身份。任何其他回答都属于 provider，与以往一样：其他 403 是 provider 故障，绝不重试，其他 429 是速率限制，会重试。
- **跳过，而不是停滞。** 搁置持续期间，作业会在下一次选取时排除被搁置的工作（`server/workers/holds.py`），因此其批次会转给其他身份的工作，而不是再次被只会被搁置的工作占满。提取队列把该请求标记为带原因的 `held`，直到搁置结束，同时存入一个旧版 worker 在回滚后可以回收的 `running` 租约。在此期间，它不会认领该入队者在该命名空间中的其他请求（按其 `enqueued_by` 列）。profile 折叠读取其槽位数的四倍，把被搁置身份的槽位让给下一个读到的身份，并把它排除在扫描之外；结构化 profile worker 在搁置结束前排除被搁置的用户。embedding worker 记录每个被搁置 stub 的 `embed_retry_at`，不消耗尝试次数。投影修复在应用上限之前排除被搁置的失败 ID。embedding 选取在应用任一上限之前排除被搁置的行，即使健康的写入者使用相同的命名空间和 agent 标签也一样。批量 embedder（embedding worker、`loom reembed`、模型重建）一次搁置一个分组（`customer.GROUP_HOLDS`：组织被关闭、身份被拒绝、被要求等待或未读取），并写入其他分组的调用所得到的向量。模型重建会暂存这些健康的向量，但在每个被搁置的行都能重建之前，保留该页的游标和计数器。其保存的重试截止时间在 worker 更换后依然有效，并阻止在搁置结束之前调用 provider。发布之前，验证仍会拒绝丢失的源向量。

**为什么用命名空间的所有者。** 作业处理的某些行没有记录主体（对话轮次、共享范围的向量），而 agent 的令牌把它自己的命名空间作为主目录，因此该命名空间指明了这项工作是为哪个 agent 做的。没有记录写入者的共享命名空间的行，发出时带上已渲染出的请求头，但不带 agent。

**为什么用一张回答表。** 网关对单个身份的拒绝与其自身的故障可能共用一个状态码（agent 不被允许时的 403，以及 Loom 的凭据不被允许代表 agent 行事时的 403），而消息中的单词并不是契约。该表列出网关针对单个身份给出的精确回答，凡是网关提供稳定 code 的都按 code 匹配，只有这些回答会搁置该身份，而不是被当作 provider 宕机。

<a id="fake-providers"></a>

## 模拟 provider

fake provider 可以让没有密钥的安装正常启动，让测试离线运行，并让 CI 保持确定性。不要用它们对外提供服务。每一次回退都是可见的：

- 回退会写入日志（embedder 为 ERROR，LLM 为 WARNING）。
- `/v1/health` 和控制台会显示一条 `problems` 条目：`fake_embedder`、`embedder_key_missing`、`fake_llm`、`llm_key_missing`，或者在真实 provider 最近一次调用失败时显示 `provider_failing:<name>`（见 `server/observability/problems.py`）。
- 设置 `LOOM_REQUIRE_REAL_PROVIDERS=true` 后，fake 条目会被视为错误，health 会显示为 `degraded`。HTTP 状态码仍为 200；只有 ClickHouse 中断时才返回 503。

使用 fake 的 LLM 功能会返回确定性的回显内容；有些功能会直接跳过（对话标题生成器）。

<a id="llm-driven-and-deterministic-parts"></a>

## LLM 驱动与确定性的部分

所有决定存储什么内容或谁可以读取的关卡都是确定性的：授权、黑名单、敏感信息检测、冲突与重复判定、排序以及弃答下限。LLM 阶段负责生成或丰富内容（事实、回答、摘要、标题）。每个 LLM 阶段都是可选的、有界的，并且在失败时回退到确定性路径。

**原因。** 由 LLM 决定的关卡无法复现、审计或做单元测试，还会给每次写入增加成本和延迟。确定性关卡在 CI 中通过生产代码路径运行在 fake provider 上。代价是：余弦阈值无法区分修订与矛盾，这正是冲突解析器存在的原因。它在写入之后、在 leader 上做出判断，因此写入本身的判定仍然是确定性的。

| 功能                                          | 是否使用 LLM？ | 开关（默认值）                                                                                                                                                                                                                                                                                               |
| --------------------------------------------- | ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `memory.set`、`memory.search` 核心            | 否            | —                                                                                                                                                                                                                                                                                                            |
| 冲突与重复判定                                | 否，余弦      | `LOOM_CONFLICT_THRESHOLD`                                                                                                                                                                                                                                                                                    |
| 冲突解析器（写入之后）                        | 是            | `conflict_resolver` worker，`LOOM_MEMORY_CONFLICT_RESOLVER_ENABLED`（默认开启，除非 LLM provider 或解析器模型的 provider 是 fake）；`LOOM_CONFLICT_RESOLVER_MODEL`（`gpt-6.1-sol`）                                                                                                                        |
| `memory.set_from_messages` 提取               | 是            | 始终启用；延迟执行通过 `extraction_queue` worker 运行                                                                                                                                                                                                                                                        |
| 提取校验器（编码捕获）                        | 是            | `LOOM_MEMORY_EXTRACT_VERIFY_MODELS`（`claude-opus-5-5`；`""` 表示关闭）；失败时放行                                                                                                                                                                                                                          |
| `memory.ask` 合成                             | 是            | `LOOM_MEMORY_ASK_SYNTH_MODEL`、`LOOM_MEMORY_ASK_SYNTH_MODEL_CHEAP`、`LOOM_MEMORY_ASK_MID_MODEL`                                                                                                                                                                                                              |
| `memory.search` HyDE 与回退辅助               | 是，有界      | 每次请求的 `hyde`；`LOOM_MEMORY_SEARCH_FALLBACK_MAX_LLM_CALLS`（2）；在 `retrieval_budget: "fast"`、`recipe: "fast"` 下或在 `context.pack` 中[没有](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md#modes-and-recipes)                                                                                                  |
| `memory.search` 重排                          | 是，需主动启用 | 命名空间的 `rerank_endpoint` 或每次请求的 `rerank`（`builtin:openai[:model]`、`builtin:cohere[:model]`、`LOOM_RERANK_ENDPOINTS_JSON` 中的名称）；在 `retrieval_budget: "fast"`、`recipe: "fast"` 下或在 `context.pack` 中，`builtin:openai` 会使其[没有](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md#modes-and-recipes)重排 |
| `knowledge.search` 重排                       | 是，需主动启用 | 每次请求的 `rerank` 或 `LOOM_KNOWLEDGE_SEARCH_RERANK_DEFAULT`；在 `memory.ask`、`context.pack` 或 `loom_search` 的仓库文档分支中没有                                                                                                                                                                        |
| 实体提取                                      | 是            | `entity_extractor` worker，`LOOM_ENTITY_EXTRACT_ENABLED`（开启）                                                                                                                                                                                                                                             |
| 对话标题                                      | 是            | `conversation_titler` worker（开启；在 fake LLM 上跳过）                                                                                                                                                                                                                                                     |
| 观察挖掘、反思、画像合并                      | 是            | `observe_extract`、`reflection_distill`、`profile_consolidation` worker（关闭）                                                                                                                                                                                                                              |
| 结构化画像                                    | 是            | `structured_profile` worker（开启；仅对带有画像 schema 的命名空间生效），`LOOM_STRUCTURED_PROFILE_MODEL`                                                                                                                                                                                                     |
| 代码摘要                                      | 是            | `code_summary` worker（关闭），`LOOM_CODE_SUMMARY_MODEL`                                                                                                                                                                                                                                                     |
| `code.ask`                                    | 是            | `LOOM_CODE_ASK_LLM_MODEL`                                                                                                                                                                                                                                                                                    |
| 会话简报（`session.brief_save`）              | 是，可选      | `LOOM_SESSION_BRIEF_LLM`（开启），`LOOM_SESSION_BRIEF_LLM_MODEL`                                                                                                                                                                                                                                             |
| 检索质量评判                                  | 是，抽样      | `LOOM_RETRIEVAL_JUDGE_SAMPLE_RATE`（0）                                                                                                                                                                                                                                                                      |
| 嵌入恢复、校准                                | 否            | —                                                                                                                                                                                                                                                                                                            |

agent 是否调用 `memory.set` 完全由客户端模型决定。MCP 工具描述（`server/transport/mcp_descriptions.py`）描述的是意图，而不是语法。

<a id="llm-free-reads"></a>

### 不使用 LLM 的读取

`LOOM_READS_LLM_FREE=true` 会让每个读操作（`Operation.read_only`）都在不调用 LLM 的情况下运行，因此部署方可以承诺读取绝不会访问模型。HTTP 和 MCP 传输层会在处理器运行期间标记请求（`server/core/llm_free.py`）；记忆搜索通过与快速姿态和 `context.pack` 相同的关卡（`_is_llm_free`）读取该标记，其他每个可能调用模型的读取步骤则读取 `reads_llm_free()`：

- `memory.search`、`search.query`（MCP 的 `loom_search` 工具）、`context.pack` 以及所有构建在它们之上的读取：不使用 HyDE、图三元组、重试评判、LLM 计数意图或动词同义词。拼写纠正和集合扩展的嵌入也会被跳过，因此一次普通的搜索或 pack 只对查询做一次嵌入。
- `code.search`：不使用 HyDE、以模型命名的概念扩展或查询扩展；其备忘缓存将不使用 LLM 的回答单独存放。
- 重排：重排器就是一次模型调用，不使用 LLM 的读取只运行请求中明确指定的重排器。命名空间的 `rerank_endpoint` 以及代码和知识搜索的默认值（`LOOM_CODE_SEARCH_RERANK_DEFAULT`、`LOOM_KNOWLEDGE_SEARCH_RERANK_DEFAULT`）不会运行；请求中指定的重排器会运行，但 `builtin:openai` 除外，它会保留融合后的顺序。
- `tracker.duplicate_check` 和 `tracker.answer`：不使用重排或不确定性评判；`code.bisect_candidates`：不使用重排。它们各自保留原本就会回退到的确定性答案。
- `memory.ask` 和 `code.ask` 会返回 409 `READS_LLM_FREE` 并指明该设置，除非请求设置了 `allow_synthesis=false` / `synthesize=false`，即使确定性路由本可以回答；设置该标志后，它们会返回召回的证据，不调用 LLM。这些调用的目的就是得到模型的回答，所以该设置选择拒绝，而不是在没有模型的情况下作答。`memory.brief` 只要求证据，并连同未知项一起返回，不给出回答。
- 抽样的检索质量评判不会排入任何任务：两种传输层都在同一个标记内记录搜索的质量。

写入（`memory.set_from_messages` 提取、冲突评判、`knowledge.synthesize`、会话简报）和后台 worker 仍然会调用模型。

<a id="prompts"></a>

## 提示词

提示词是 Python 常量，与代码一同进行版本管理：

| 提示词类别                                                                     | 位置                                                                                                                          |
| ------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------- |
| 事实提取、判定、重试附加提示                                                   | `server/memory/domain/extraction_prompts.py`；在 `server/memory/app/extract/prompts.py` 中组装                                |
| 搜索辅助（HyDE、迭代判定、派生意图、三元组、动词同义词）                       | `server/memory/domain/search_prompts.py`                                                                                      |
| 重排序                                                                         | `server/memory/app/rerank.py`                                                                                                 |
| `memory.ask` 综合生成                                                          | `server/memory/app/ask/prompts.py`                                                                                            |
| Worker 提示词                                                                  | 各 worker 模块（`code_summary`、`conversation_titler`、`profile_consolidation`、`structured_profile`、`entity_extractor`）   |

每个任务使用哪个模型，统一在 `server/settings/catalog.py` 的 `Models` 部分设置（[各任务的模型](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#models-per-job)）。提示词中召回的记忆会被作为数据围起来（[安全](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#prompt-safety)）。

**提取输出修复**（`server/memory/app/extract/extract.py` 中的 `extract_from_messages`）：

- 如果解析失败看起来是由截断引起的，则以两倍的 token 预算重试（最高 8192），然后保留完整的事实对象；
- 其他任何解析失败，都会附加更严格的提示重试一次；
- 内容不算简短的输入若没有产出任何事实，则再重试一次。

**Langfuse。** `scripts/codegen/push_langfuse_prompts.py --write`（配合 `LOOM_LANGFUSE_URL` 和 `LOOM_LANGFUSE_ADMIN_TOKEN`）会以固定名称发布这些提示词常量，并把每个提示词的文本哈希和版本记录在 `server/observability/langfuse_prompts.lock.json` 中。运行时不会获取任何提示词。借助该锁文件，生成 span 可以携带提示词的名称、版本和哈希（`server/observability/genai.py`）。

**为什么提示词是代码。** 提示词的变更就是行为的变更，并由基准测试来衡量。它会出现在 diff 中，与解析其输出的代码一起被评审、跑基准测试以及回滚。修改提示词需要重新部署。

<a id="cost-accounting"></a>

## 成本核算

每次 provider 调用都会向每个 op 的累加器（`server/observability/cost_accumulator.py`）上报用量。两种传输方式都在每个请求中绑定该累加器，worker supervisor 则在每一轮处理中绑定。价格来自同一模块中的 `PRICE_PER_1K_TOKENS`，并带有缓存读取和缓存写入的倍率。未列出的模型按 $0 计费，并记录一条警告日志。Gemini embedding 不报告用量，按 $0 计费。

op 在 `loom.agent_events` 中的记录包含 `upstream_cost_usd`、`embedding_tokens`、`input_tokens`、`output_tokens`、`llm_calls` 以及主要使用的模型（见 `server/observability/ledger.py` 中的 `emit_agent_event`）。缓存节省的费用会体现在每次回答中（`memory.ask` 的 `llm_cache_saved_usd`），也会出现在事件的 `args` 中（`out_llm_cache_saved_usd`）。参见[成本与用量](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)。

<a id="changing-a-default"></a>

## 更改默认值

开发环境的默认值是模拟实现（fake）。生产环境需要显式设置 `LOOM_EMBEDDING_MODEL` 和 `LOOM_LLM_MODEL`；`.env.example` 中被注释掉的提供商配置块使用 `text-embedding-3-small`（1536 维）和 `gpt-6-luna`。

**在更改这两个默认值之前，请先运行 `make bench-quick`**，在选择使用 Anthropic 提供商之前也是如此。它会针对正在运行的服务器，使用答案评判器对长期记忆基准测试（`bench/longmemeval.py`）中的 30 个问题样本进行测试，并在 `/tmp` 下写入一个 CSV 文件。它需要真实的提供商。参见[运行基准测试](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)。

<a id="invariants"></a>

## 不变量

- 一个进程在启动时选定，并只使用一个 embedder 和一个默认 LLM 客户端。
- 不同 `embedding_model` 值的向量之间永远不会相互比较。
- 只有在缺少密钥时，所服务的 embedder 才会回退到 fake，并会在 `/v1/health` 上说明。次级 embedder 从不回退。
- 搜索始终有阈值：优先使用命名空间校准值，其次是启动校准值，最后是标量设置。
- 任何决定存储或访问的关卡都不会调用 LLM。
- 回退模型的答案从不缓存。
- 每次提供方调用的用量都会记录在发起该调用的 op 或 pass 所对应的 `agent_events` 行上。

<a id="source-entry-points"></a>

## 源码入口

| 内容                      | 位置                                                                                                                          |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| Embedder 选择             | `server/embedding/embedder.py` (`provider_for`, `get_embedder`, `probe_embedding_dim`)                                        |
| Embedding 缓存            | `server/embedding/_cache.py`                                                                                                  |
| Provider 重试与防护       | `server/embedding/_provider.py`, `server/llm/_retry.py`                                                                       |
| LLM 选择                  | `server/core/llm_provider.py` (`resolve_llm_provider`), `server/llm/client.py` (`get_llm_client`, `get_llm_client_for_model`) |
| 回退模型                  | `server/llm/fallback.py` (`FallbackLLMClient`)                                                                                |
| 响应缓存                  | `server/llm/cache.py` (`CachedLLMClient`)                                                                                     |
| 校准                      | `server/memory/app/embedder_calibration.py`, `server/memory/workers/recalibration.py`                                         |
| 重新嵌入                  | `server/memory/app/reembed.py` (`reembed_all`)                                                                                |
| 提示词                    | `server/memory/domain/extraction_prompts.py`, `server/memory/domain/search_prompts.py`                                        |
| 提示词发布                | `scripts/codegen/push_langfuse_prompts.py`                                                                                    |
| 成本                      | `server/observability/cost_accumulator.py`, `server/observability/ledger.py`                                                  |
| Provider 问题             | `server/observability/problems.py`                                                                                            |

<a id="related"></a>

## 相关内容

- [提取](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md)：提取器产生的内容。
- [检索与排序](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)：阈值发挥作用的位置。
- [Langfuse](../features/integrations/langfuse.md)：追踪与提示词链接。
- [基准测试](../benchmarks/overview.md)：如何衡量变更。
