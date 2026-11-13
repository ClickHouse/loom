<!-- i18n: source=docs/getting-started/quickstart.md blob=2dcc795de559021866a2affe24bef8e4e8f41f81 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md), which is authoritative.

<a id="quickstart"></a>

# Quickstart

本页介绍如何在笔记本电脑上启动 Loom，并通过 HTTP、Python 和 MCP 保存第一条记忆。整个过程不需要 API key，也不需要账号。全新安装的发布版技术栈使用镜像自带的小型本地模型生成 embedding，因此无需联网，搜索也能返回有意义的结果（升级安装则沿用其 `.env` 中指定的 embedder）；其 LLM 是模拟的，所以提取和 `memory.ask` 不会产生任何结果。checkout 中的 `.env` 对两者都使用模拟 provider。

**默认的 Docker 安装是单用户的：** 无需登录，仅限本机（localhost）访问，并使用默认的 `dev/local` 命名空间。一个人无需启用多用户模式，就可以连接多个 agent。**OSS 版本同样支持团队部署**，提供经过身份验证的账号和命名空间访问控制；请[以多用户模式启动](#start-in-multi-user-mode)，或参阅 [Deployment → Identity modes](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)。两种模式支持相同的 Loom 功能；无论哪种模式，使用真实的 LLM 和 embedding 都需要配置 provider。

<a id="prerequisites"></a>

## 前提条件

| 需要                           | 用途                                                                      |
| ------------------------------ | ------------------------------------------------------------------------ |
| 带 compose 插件的 Docker       | 运行整套服务：ClickHouse 和 Loom 服务器。                                |
| Python 3.12 或更高版本         | 仅供贡献者使用的 `loom` CLI。SDK 支持 3.11+。                            |
| Node.js 20 或更高版本          | TypeScript SDK（可选）。                                                 |
| 空闲端口 `7777`                | 供 Loom 使用。从源码检出进行开发时，还会在 `8123` 和 `9000` 上暴露 ClickHouse。 |
| `jq`                           | 在第 4 步中读取 `curl` 的输出（可选）。                                  |

<a id="1-boot-the-stack"></a>

## 1. 启动堆栈

<a id="fast-path-released-image-no-clone"></a>

### 快速路径：使用已发布的镜像，无需克隆

前置条件：Docker Desktop 或 Docker Engine，且 Compose 为 2.20 或更高版本。

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

该命令会把安装脚本保存到当前目录，然后运行该文件，因此请在你的主目录或其他只有你能写入的目录中运行：在共享目录中，其他人可能在脚本运行之前替换 `loom-install.sh`，或将其指向另一个文件。下载失败时会停止，并给出 curl 的错误信息和非零退出码；安装失败后，请先修复其报告的问题，再完整地重新运行整条命令。安装脚本会从最新的 GitHub Release 下载 `loom-stack.tar.gz`，校验其 SHA-256，把 Compose 和 ClickHouse 配置安装到 `~/.loom` 下，拉取对应的 `ghcr.io/clickhouse/loom:X.Y.Z` 和 `ghcr.io/clickhouse/loom-extractor:X.Y.Z`（文档提取服务）镜像，并等待 `http://localhost:7777/v1/health` 就绪。服务器镜像包含 API、worker、迁移，以及位于服务器根路径的已构建 React 控制台；无需安装 Python 或 Node。Python、TypeScript 和 Go 编译器索引器已内置并默认启用。使用 TypeScript 索引器索引的仓库会自动安装其依赖。两个镜像均支持 Linux `amd64` 和 `arm64`，包括 Apple Silicon 上的 Docker Desktop。

该堆栈会运行自己的 ClickHouse 容器。若要使用你已在运行的 ClickHouse（自托管服务器，或 26.4 及以上版本、带 Keeper 的 ClickHouse Cloud 服务），请传入它的 URL；如果 URL 中没有密码，安装脚本会询问密码，然后创建 `loom` 数据库和一个只读 reader 用户，或者打印出需要管理员执行的 SQL（[选择 ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#choosing-the-clickhouse)）：

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh \
  && sh loom-install.sh --clickhouse https://loom@abc123.us-east-1.aws.clickhouse.cloud:8443 && rm loom-install.sh
```

每个版本的 `install.sh` 安装的都是该版本，因此 `latest` 提供的副本安装最新版本，而下面各版本自己的 URL 每次安装的都是该版本。在较新的安装之上，较旧版本的副本会拒绝安装，而不是在较新的数据上启动较旧的 Loom 和 ClickHouse；如需有意降级，请参照[降级通过安装脚本安装的实例](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#downgrading-an-installer-install)。要固定某个版本，请使用它自己的 URL：

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/download/v1.2.3/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

如果想在启动前检查所有内容，请下载这些文件并进行校验：

```bash
curl -fLO https://github.com/ClickHouse/loom/releases/latest/download/loom-stack.tar.gz
curl -fLO https://github.com/ClickHouse/loom/releases/latest/download/loom-stack.tar.gz.sha256
curl -fL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh
sha256sum -c loom-stack.tar.gz.sha256
tar -tzf loom-stack.tar.gz
```

确认输出无误后，再从已检查的安装包进行安装：

```bash
LOOM_INSTALL_BUNDLE="$PWD/loom-stack.tar.gz" \
  LOOM_INSTALL_CHECKSUM="$PWD/loom-stack.tar.gz.sha256" sh loom-install.sh && rm loom-install.sh
```

传给安装脚本的 `LOOM_PORT`、`LOOM_BIND_HOST`、`LOOM_IMAGE_REPOSITORY`、`LOOM_EXTRACTOR_IMAGE_REPOSITORY` 和 `LOOM_CLICKHOUSE_IMAGE` 会被写入安装目录的 `.env`，因此之后在该目录中执行的 `docker compose` 命令以及之后运行的安装脚本会使用相同的端口和镜像。将 `LOOM_BIND_HOST` 设为非回环地址会把 Loom 发布到本机之外，此时如果没有 `LOOM_PUBLIC_BASE_URL` 和登录功能，服务器会拒绝启动：请使用 `LOOM_SKIP_START=1` 安装，在 `.env` 中设置它们（见下文），然后不带 `LOOM_SKIP_START=1` 再次运行安装命令。

每个版本运行的是其构建时最新的 ClickHouse 版本，因此对同一个 Loom 版本再次运行安装命令会保持其 ClickHouse 不变；安装更新的 Loom 版本会随之升级 ClickHouse，除非 `.env` 固定了 `LOOM_CLICKHOUSE_IMAGE`（删除该行即可重新跟随各版本）。再次运行安装命令会更新受管的 Compose/配置文件并协调容器。它会保留 `~/.loom/.env`（若其中没有提取服务的 URL 和令牌，会为其补上）以及具名 Docker 卷。要在不删除数据的情况下停止：

```bash
docker compose --project-directory "$HOME/.loom" down
```

仅在有意进行不可逆的重置时才添加 `--volumes`。

<a id="try-it-in-github-codespaces"></a>

### 在 GitHub Codespaces 中试用

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/ClickHouse/loom?quickstart=1)

仓库的 dev container 会在 codespace 中运行最新版本，你的本机无需安装任何东西：它使用上述安装脚本安装 Loom，启动它，并把 Claude Code 连接到它。首次启动会下载约 3 GB 的镜像，需要几分钟。请在创建日志最后几行打印的地址打开控制台（`Loom is running in the
codespace. Dashboard: https://<codespace name>-7777.app.github.dev`，即 **Ports** 标签页中端口 7777 的转发地址）；其上方安装脚本打印的 `http://127.0.0.1:7777/` 是 codespace 自身的地址。在 codespace 的终端中运行 `cd ~ && claude`（在检出目录之外，因为其中的说明是用于开发 Loom 本身的），登录，然后说 “Remember
that I deploy only on Tuesdays”；在新的 `claude` 会话中，问 “When do I deploy?”。

该试用以单用户方式运行，没有登录。在浏览器中，端口 7777 是私有转发的：其地址只响应你的 GitHub 账号。请保持私有：设为公开或对组织私有，会让没有登录功能的服务器向所有被共享者开放。codespace 内的 agent（例如它的 `claude`）使用 `http://localhost:7777`；控制台的连接说明中写的是转发地址，而它需要 GitHub 登录，codespace 中的 agent 并不具备。在桌面版 VS Code 中打开时，codespace 还会把该端口转发到你笔记本的 `localhost:7777`，在那里它与本地安装一样对笔记本上的所有程序开放，笔记本上的 agent 也能访问到它；该转发需要本地端口空闲，因此请先停止本地的 Loom。请通过浏览器地址使用控制台：从笔记本的 `localhost:7777` 可以打开页面，但服务器会拒绝其请求，所以不会显示数据；只有转发地址才是该服务器自己的页面。

安装位于 `/workspaces/.loom`，因此重建后的 codespace 会保留其 `.env` 和 Compose 项目，不过其记忆存放在 Docker 卷中，重建时可能不会保留；重建还会在其上运行最新版本的安装脚本，从而把它升级到该版本。文档中凡是提到 `~/.loom` 或 `"$HOME/.loom"` 的地方，codespace 使用的是 `/workspaces/.loom`。Codespaces 会在端口 7777 开始监听时立即提示打开它；控制台要等到创建日志打印出 “Loom is running in the codespace” 后才会响应。如果日志以错误结束，请在 codespace 的终端中运行 `sh .devcontainer/start-loom.sh`：它会保留首次运行已完成的设置。

<a id="start-in-multi-user-mode"></a>

### 以多用户模式启动

若要全新安装已发布镜像，并从首次启动起就具备账号和登录功能，请先准备堆栈文件，但不启动容器：

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh \
  && LOOM_SKIP_START=1 sh loom-install.sh && rm loom-install.sh
```

在启动 Docker 之前编辑 `~/.loom/.env`。替换现有的身份和认证条目，并添加注册设置：

```dotenv
LOOM_IDENTITY_PROVIDER=trust-network
LOOM_AUTH_REQUIRED=true
LOOM_SELF_SIGNUP=open
```

保留生成的 `COMPOSE_PROJECT_NAME` 和 `LOOM_JWT_SECRET`。然后拉取并启动堆栈（如果使用自己的 ClickHouse，请先按安装脚本的提示运行 `ch-setup`）：

```bash
docker compose --project-directory "$HOME/.loom" pull
docker compose --project-directory "$HOME/.loom" run --rm loom loom ch-setup  # your own ClickHouse only
docker compose --project-directory "$HOME/.loom" up -d
```

[健康检查](#check-the-stack)通过后，打开 `http://localhost:7777/` 并注册。SDK 和 MCP 客户端需要令牌；请使用授予你账号的组织和命名空间，而不是下面示例中单用户的 `dev/local` 默认值。

这启用的是多用户认证，而不是公网访问：堆栈仍然绑定到 localhost。在与团队共享之前，请配置[运维访问](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)和 [TLS](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#proxy-and-tls)。同时也要配置[真实的模型和 API 密钥](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)；选择多用户模式并不会取代 fake LLM。

对于已在运行的单用户堆栈，请改为按照[切换说明](#2-open-the-dashboard)操作；容器重建会保留现有的数据卷。

<a id="contributor-path-build-a-checkout"></a>

### 贡献者路径：构建检出的代码

前置条件：Docker Compose 2.34 或更高版本，检出代码中的 compose 文件需要它才能每日拉取 ClickHouse。

```bash
git clone https://github.com/ClickHouse/loom && cd loom
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
loom up
```

`loom up` 做三件事：

1. 创建 git 不跟踪的两个文件：由 `.env.example` 生成的 `.env`，以及由模板生成的 `deploy/clickhouse-config/named_collections.xml`。它从不覆盖这两个文件；对于没有提取服务令牌的现有 `.env`，它会补上一个。
2. 使用 `--build` 运行开发用的 compose 堆栈。
3. 轮询 `http://localhost:7777/v1/health`，直到服务器就绪，并打印待执行迁移的数量。超过 `--timeout`（300 秒）后它会放弃，并给出最后一次的健康检查响应体和日志行。

首次贡献者启动会构建镜像并应用所有 schema 迁移，因此请预留几分钟。随后 `loom up` 会打印控制台 URL（`http://localhost:7777/`）、一条保存第一条记忆的 `curl` 调用和一段 Python 代码片段，以及一行用于接入 Claude Code 的命令。启用认证时，它还会生成一个 writer 令牌并放入这些片段中。

| 标志         | 作用                         |
| ------------ | ---------------------------- |
| `--rebuild`  | 代码更改后重新构建。         |
| `--no-build` | 复用缓存的镜像。             |
| `--open`     | 打开控制台。                 |
| `--port`     | 使用另一个 Loom 端口。       |
| `--env-file` | 指向另一个 env 文件。        |

对正在运行的堆栈重新运行 `loom up` 只会重新检查健康状态。`make dev` 不经过 CLI 完成同样的事情，并运行：

```bash
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --build
```

由于 `compose.yaml` 排在最前，Compose 会读取仓库根目录的 `.env` 来完成 `${VAR:-default}` 替换；`--env-file .env` 只是把这一点显式写出来。`loom down` 会停止堆栈并保留其卷。`loom down --delete-data` 只有在你输入生成的 Compose 项目名称之后，才会永久删除本次安装的具名卷；`--yes` 是非交互式的确认。Makefile 目标的作用相同：`make down` 保留数据，`make down-all` 删除数据。

<a id="what-the-stock-env-boots"></a>

### 默认 `.env` 启动的内容

检出代码和发布包的 `.env` 模板都选择单用户模式。发布包显式设置了 `LOOM_IDENTITY_PROVIDER=single-user`；检出代码将其留空，并根据 `LOOM_AUTH_REQUIRED=false` 推断出相同的模式。

| 设置                                                                         | 值            | 效果                                                                      |
| ---------------------------------------------------------------------------- | ------------- | ------------------------------------------------------------------------- |
| `LOOM_AUTH_REQUIRED=false`, `LOOM_IDENTITY_PROVIDER=single-user` (or empty)  | `single-user` | 无需登录。默认命名空间为 `dev/local`。SDK 和 MCP 调用不需要令牌。         |
| `LOOM_LLM_PROVIDER=fake`, `LOOM_LLM_MODEL=fake-echo-1`                       | fake LLM      | 提取、实体标注和 `memory.ask` 的合成不会产生任何结果。                    |
| `LOOM_EMBEDDING_MODEL=local`, `LOOM_EMBEDDING_DIM=384` (release bundle)      | bge-small     | 在 CPU 上进行语义搜索，使用镜像自带的模型文件。                           |
| `LOOM_EMBEDDING_MODEL=fake-ngram-3072`, `LOOM_EMBEDDING_DIM=3072` (checkout) | fake embedder | 搜索按字符 n-gram 排序。足以检查连通性，但不适合评估召回。                |

两种安装方式都会生成唯一的 `COMPOSE_PROJECT_NAME`、随机的 JWT 签名密钥、随机的提取服务令牌，以及 `LOOM_BIND_HOST=127.0.0.1`。因此，不同的克隆不会悄悄共享 ClickHouse 卷，并且 Loom 和 ClickHouse 只能从本机访问。

要使用真实的提供方，请打开 **Settings → Models & keys**，选择你的提供方和模型，输入它们的 API 密钥，然后点击 **Test & apply**。一个 OpenAI API 密钥可以同时用于 LLM 和 embedding。无需重启 Docker；embedding 的更改会触发经过验证的重建，控制台中会显示进度并支持重试。

如果改用基于环境变量的部署，请替换 `~/.loom/.env`（已发布的堆栈）或 `.env`（检出代码）中的提供方行。仅设置 `OPENAI_API_KEY` 不会改变任何东西：默认的提供方行显式指定了各自的提供方。从 local 或 fake embedder 切换出去会对已存储的内容重新做 embedding。

```bash
LOOM_EMBEDDING_MODEL=text-embedding-3-large
LOOM_EMBEDDING_DIM=3072
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-6-luna
OPENAI_API_KEY=sk-...
```

对于 Claude、OpenAI、托管的 embedding、仅限订阅的客户端，以及只使用一个 LLM 提供方的部署，请参照[配置模型和 API 密钥](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)。编辑已发布堆栈的 `.env` 之后，用 `docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom` 使其生效；单纯重启不会重新加载容器的环境变量。高级设置见[部署 → 提供方](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#providers)。

<a id="check-the-stack"></a>

### 检查堆栈

```bash
curl -fsS http://localhost:7777/v1/health | jq '{status, fake_embedding, fake_llm, providers_degraded, problems}'
docker compose --project-directory "$HOME/.loom" exec loom loom doctor
```

- 当 ClickHouse 可访问，且除 `code_tier_failing`（某个仓库的代码索引层失败）之外没有其他问题属于错误时，`/v1/health` 返回 `status: ok`。`fake_embedding`、`fake_llm` 和 `providers_degraded` 表明你当前所处的模式；`problems` 列出每个问题及其修复方法；`schema.pending` 列出尚未应用的迁移。
- `loom doctor` 对每项服务器端检查打印一行。`--json` 输出 JSON。对于源码检出的堆栈，请运行 `docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml exec loom loom doctor`。

<a id="2-open-the-dashboard"></a>

## 2. 打开控制台

在 `single-user` 模式下，`http://localhost:7777/` 会直接打开控制台；
在多用户模式下，需要先注册或登录。在 `single-user` 模式下，
控制台和 agent 的默认值使用 `dev/local`（`LOOM_DEFAULT_NAMESPACE` 可以重命名该
命名空间）。这是一种便利模式，而不是用户隔离边界：任何能够
访问该服务器的人都可以访问其数据并进行管理。请仅限本机（localhost）访问。

如果想改为尝试注册流程（`trust-network`：使用邮箱和密码，第一个
声明某个命名空间名称的人会成为它的管理员），请替换 `~/.loom/.env`
（正式发布的栈）或 `.env`（源码检出）中的这些条目：

```dotenv
LOOM_IDENTITY_PROVIDER=trust-network
LOOM_AUTH_REQUIRED=true
LOOM_SELF_SIGNUP=open
```

当明确选择了 `single-user` 时，仅设置 `LOOM_AUTH_REQUIRED=true` 是不够的：
该模式会保持认证关闭。请保留安装程序生成的
`LOOM_JWT_SECRET` 和现有的卷，然后重新创建服务器以加载环境变量：

```bash
# Released stack:
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
# Checkout stack (run from the repository root):
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --force-recreate loom
```

此后控制台会打开登录页面，页面上有一个 **Sign up** 标签页，并且每一次 SDK
和 MCP 调用都需要令牌。各种身份模式请参见[身份与
命名空间](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md#identity-modes)。指定
`trust-network` 也使得非回环地址的 `LOOM_BIND_HOST` 可以在开放注册的情况下启动，
这意味着任何能够访问该端口的人都可以注册。在让团队可以访问该服务器之前，
请按照[部署指南](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)配置
运维访问和 [TLS](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#proxy-and-tls)。

<a id="3-get-a-token"></a>

## 3. 获取令牌

在单用户模式下，令牌是可选的。即使如此，也建议生成一个，这样在开启认证后你的脚本仍能正常工作，前提是脚本指明了 `org`（或将其留空）、`namespace` 和 `agent`：如果一次调用携带了凭据（SDK 在找到任何凭据时就会这样做，见 [Python SDK → Credentials](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md#credentials)），这三项都不会被自动填充，而留空的 `org` 即为令牌自身所属的 org。

- **控制台：** **Settings → Tokens → New token**。默认情况下，该令牌可以读取你所属的每个命名空间和仓库（受限仓库只有在你添加后才可读取），但不能读取任何 collection，并且会保存到你当前所在的命名空间。_Only what you choose_ 会把范围限制在你勾选的命名空间、collection 和仓库内（起初一个都不选）；被勾选的项目按列出的顺序进行搜索，你可以用行内的箭头调整顺序，在桌面端也可以拖动该行的手柄。生成结果只会显示一次令牌，并附带适用于你的 MCP 客户端或脚本的配置方法。
- **CLI，以操作员身份：** 在持有签名密钥的服务器容器内运行：

  ```bash
  docker compose --project-directory "$HOME/.loom" exec loom loom mint-token --subject my-bot --org dev --namespaces local --roles writer
  ```

  凡是会离开你本机的令牌，都请加上 `--ttl-days 30`。`loom mint-token --help` 会列出各项范围选项（`--namespace-role`、`--read-scope`、`--traffic-class`）。

- **自行注册的 bot**（仅限信任网络）：
  `loom init --agent nightly-triage --server-url http://localhost:7777` 会创建命名空间 `agent-nightly-triage`，把一个 writer 令牌保存到 `~/.loom/token`，把 URL 保存到 `~/.loom/config.json`，并打印一个认领码。由一个人在 `/oauth/agent-claim` 页面输入一次该认领码，并设置自己的密码，即可取得所有权。

请把令牌保存在 SDK、CLI 和插件会查找的位置：

```bash
mkdir -p ~/.loom && (umask 077; printf '%s' "<token>" > ~/.loom/token)
export LOOM_API_KEY="<token>"
```

关于 claim、角色和授权，请参阅 [Tokens and grants](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)。

<a id="4-first-memory-over-http"></a>

## 4. 通过 HTTP 写入第一条记忆

每个操作都是一个带 JSON 请求体的 `POST /v1/<op>`。使用默认 `.env` 的全新安装（无论来自安装程序还是源码检出）是单用户模式：无需令牌，日常的记忆或搜索调用（`memory.set`、`memory.get`、`memory.search` 等）如果没有指定 `org` 或 `namespace`，就会在安装自带的 org `dev` 和命名空间 `local` 中运行，MCP 和控制台使用的也是它们；没有指定 `agent` 的 `memory.set` 会记录为 agent `http`，而搜索会读取所有 agent 的记忆。因此下面的调用只需填写各自的字段（[哪些操作适用](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#request)）。如果不带令牌的调用返回 401 `AUTH_INVALID`，说明已启用认证：请添加 `-H "Authorization: Bearer $LOOM_API_KEY"`，并填写你账户的 `org`、`namespace` 和 `agent`；令牌为空的请求头会被拒绝。

```bash
curl -sX POST http://localhost:7777/v1/memory.set -H 'Content-Type: application/json' \
  -d '{"memory_key":"deploy_window","value":"Tue and Thu only; never Friday."}'

curl -sX POST http://localhost:7777/v1/memory.get -H 'Content-Type: application/json' \
  -d '{"memory_key":"deploy_window"}' | jq '{found, value}'

curl -sX POST http://localhost:7777/v1/memory.search -H 'Content-Type: application/json' \
  -d '{"query":"when can we deploy?"}' | jq '.results[0].content_excerpt'
```

`memory.set` 的响应包含 `memory_id`、`content_sha256` 和 `status`。
`memory.get` 读回单个键：`found` 表示该键是否持有有效值，`value` 是写入时的原始值。搜索响应包含 `results` 和 `abstain`，后者表示最佳命中是否足够可靠，可以据此作答。搜索会读取命名空间内所有 agent 的记忆，包括在控制台中添加的记忆；若指定 `agent`，则只读取该 agent 的记忆。
`GET /v1/operations` 会列出所有操作，在 <http://localhost:7777/docs> 中可以直接通过浏览器试用每个操作。各接口的数据结构见 [HTTP API 参考](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md)。

<a id="5-first-memory-from-python"></a>

## 5. 用 Python 写入第一条记忆

```bash
pip install clickhouse-loom    # or, from a clone: pip install ./sdk/python
```

尚未发布到 PyPI 的版本可以从其发布资源中安装
（[从 GitHub Release 安装](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md#installing-from-a-github-release)）。

示例中指定了 `org` 和 `namespace`，因此无论是否使用第 3 步中的令牌都能运行；SDK 找到令牌时会自动发送。在单用户服务器上，不带令牌的调用可以省略这两个参数：`Agent(agent="quickstart", base_url=...)` 同样会在 `dev`/`local` 中运行。

```python
import asyncio

from loom import Agent


async def main():
    async with Agent(
        org="dev",
        namespace="local",
        agent="quickstart",  # names the writer in the audit log
        base_url="http://localhost:7777",
    ) as a:  # sends a credential it finds; none needed in single-user mode
        await a.memory.set("standup_time", "Daily standup is at 9:30.")
        row = await a.memory.get("standup_time")  # None when the key is unset
        print(row["value"] if row else "unset")
        for hit in await a.memory.search("when is standup?"):
            print(hit.content_excerpt)


asyncio.run(main())
```

SDK 如何查找服务器和凭据，请参见
[Python SDK → Credentials](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md#credentials)。
TypeScript SDK 通过 `npm install @clickhouse/loom` 安装
（[从克隆的仓库安装](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md#install)）。

客户端接口详见 [Python SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md) 和
[TypeScript SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md) 参考文档。

<a id="6-first-memory-from-a-coding-agent"></a>

## 6. 通过编码 agent 创建第一条记忆

对于 Claude Code，请安装 Loom 插件。它在 MCP 工具之上增加了 hook（会话开始时加载上下文，结束时捕获内容）和 skill：

```bash
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom
```

如果服务器是本机上的单用户服务器，Claude Code 询问插件设置时，保留默认的服务器 URL，并将令牌留空。

不使用插件时，可以直接添加 MCP 条目：

```bash
claude mcp add --transport http loom "http://localhost:7777/mcp?toolset=coding" -s user
```

两种方式都无需你提供令牌，即可连接本机上的单用户服务器。

开启认证后，请在 **Settings → Tokens** 中创建令牌，并在 **Connect** 下选择 **Claude Code**：对话框会显示插件和 `claude mcp add` 条目的配置方法，其中已包含该令牌。然后：

1. 用对话框中的 `config.json` 代码块，加上该令牌保存记忆所用的命名空间（`"namespace": "<namespace>"`），覆盖 `~/.loom/config.json`：单用户会话遗留在该文件中的 `org` 或 `namespace` 优先级高于令牌；如果没有设置，插件会写入该令牌除 `*` 以外的第一个命名空间，或 `coding`。请把你之前设置的 `code_repo` 或 `agent` 加回去。
2. 将 `LOOM_API_KEY`（第 3 步可能已导出过一个：`unset LOOM_API_KEY`）和插件的 `token` 选项留空，或设置为新令牌：插件会先读取这两项，再读取该文件（[配置](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#configuration)）。
3. 如果之前创建过不带令牌的 `claude mcp add` 条目，请先删除它（`claude mcp remove loom -s user`），再运行对话框中的命令，因为该命令会拒绝已存在的名称。

`loom install --client <name>` 会为 Claude Desktop、Claude Code、Codex、Cursor、VS Code、Cline、Windsurf 和 Zed 写入 MCP 条目；不带该标志时，会为找到的所有客户端完成配置。[连接编码 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)介绍了 Codex 和 Cursor。

开启一个新会话，然后说：_"Remember that I never deploy on Friday."_ agent 会调用 `loom_memory_set`。在一个全新的会话中询问：_"When can I deploy?"_ agent 会调用 `loom_search`，并根据已存储的事实作答。模型根据工具描述决定何时保存和搜索，没有触发词。

<a id="7-see-it-in-the-dashboard"></a>

## 7. 在控制台中查看

| 页面              | 显示内容                                    |
| ----------------- | ------------------------------------------- |
| `/memories`       | 命名空间中的记忆，包括写入者和时间。        |
| `/admin/activity` | 审计日志：哪个 agent 在何时写入了哪个键。   |
| `/admin/usage`    | 每个操作和每个 agent 的成本。               |

其余内容见[控制台](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md)。

<a id="optional-index-a-repository-on-the-released-stack"></a>

## 可选：在已发布的 stack 上为仓库建立索引

在此 stack 上，从控制台添加仓库会被拒绝，并返回 `SANDBOX_UNAVAILABLE`：
它需要[仓库任务沙箱](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#repository-job-sandbox)，
而该 stack 将其关闭，因为 Docker 的默认隔离会阻止它。请只列出你信任的仓库：它们的构建步骤（TypeScript 安装、Go module 下载、
CMake）在没有沙箱的情况下运行，运行位置是 `loom` 容器，其环境中保存着 JWT
密钥和 provider 密钥。

`LOOM_CODE_REPOS_JSON` 是一行内容，以 JSON 数组的形式包含所有仓库
（[配置条目](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#configure-the-entry)）。
在 `~/.loom/.env` 中取消该行的注释（如果没有就添加），然后编辑它。
`dev` 是单用户 stack 运行时使用的 org，也是多用户账户注册时加入的 org
（`LOOM_SELF_SIGNUP_ORG`）；如果你修改过它，请在这里以及下面的
`--org` 中使用你账户所在的 org：

```dotenv
LOOM_CODE_REPOS_JSON=[{"org":"dev","namespace":"code-myrepo","repo":"acme/myrepo","git_branch":"main","public":true}]
```

- Loom 会根据仓库的构建文件（`go.mod`、
  `package.json`、`pyproject.toml`、根目录的 `CMakeLists.txt` 等）选择编译器层的索引器；也可以用 `scip_producer` 指定
  一个（[编译器层](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier)）。
  C/C++ 还需要已配置好的构建目录，因此没有构建目录时，C/C++ 仓库只会得到
  解析器层的索引。
- 对于私有仓库，请添加 GitHub 令牌（`.env` 中的 `LOOM_CODE_GITHUB_TOKEN`，或条目上的
  `github_token`），并去掉 `"public":true`：该选项适用于在其托管平台上公开的仓库，它会让 org 中的任何人立即从
  **Repositories** 以读者身份加入索引。请在首次运行 `loom code init` 之前决定：它会根据 `public` 设置命名空间的加入
  策略，之后再修改 `public` 不会改变该策略（维护者
  可以用 `code.repo.set_access` 修改）。

然后重新创建 `loom` 容器并运行首次索引：

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
docker compose --project-directory "$HOME/.loom" exec loom loom code init --namespace code-myrepo --org dev
docker compose --project-directory "$HOME/.loom" exec loom loom code backfill --namespace code-myrepo --org dev --embed
```

`backfill --embed` 使用已发布 stack 的本地 embedder 运行。如果使用的是 fake embedder（源码检出目录的 `.env`，
或仍然指定 `fake-ngram-*` 的升级安装），它会拒绝并退出；
若仍想试用这一流程，请添加 `--allow-fake-embedder`。之后在
**Settings → Models & keys** 中选择模型会对代码索引重新做 embedding；修改 `.env` 后则需要
单独重新做 embedding，详见[模型与密钥](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)。

在多用户模式下，列出的仓库没有维护者，因此若没有 `"public":true`，读者的
加入请求会一直等待，没有人批准。运行 `loom code init` 之后，请以运营者身份用容器中签发的
令牌给某人授权：`admin` 使其成为维护者，此后由其批准读者，`viewer` 则允许其读取。
subject 是该账户注册时使用的命名空间（即其登录名，也是
其令牌的 subject）；自助注册的邮箱从未经过验证，因此向邮箱授权不会让任何人获得访问权限：

```bash
ADMIN_TOKEN=$(docker compose --project-directory "$HOME/.loom" exec -T loom loom mint-token --subject operator --org dev --namespaces code-myrepo --roles admin --ttl-days 1)
curl -s http://localhost:7777/v1/grants.create -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"org":"dev","namespace":"code-myrepo","subject":"your-namespace","role":"admin"}'
```

<a id="optional-ask-a-question-about-a-document"></a>

## 可选：针对文档提问

```bash
loom demo
```

`loom demo` 会上传本仓库的 `AGENTS.md`，用 `knowledge.ingest` 对其进行摄取（由该技术栈的参考提取器负责解析），并用 `knowledge.synthesize` 针对它提出一个问题，然后打印答案以及答案所引用的分块。如果使用 fake LLM，则改为打印 `knowledge.search` 返回的最相关结果。`--file` 和 `--question` 可以让它指向你自己的文档和问题。

<a id="common-problems"></a>

## 常见问题

| 现象                                              | 原因与解决办法                                                                                                                                                                                  |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `set_from_messages` 没有写入任何事实               | 当前使用的是模拟 LLM。请按照[模型与密钥](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)配置真实的 LLM。                                                                                                      |
| 搜索结果看起来杂乱无章                             | 当前使用的是模拟 embedder（`.env` 中的 `fake-ngram-*`）。请按[模型与密钥](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)的说明切换，已存储的内容会被重新嵌入。不要为了更换模型而删除你的 volume。 |
| 开启认证后返回 401 `AUTH_INVALID`                  | 没有提供令牌，或令牌由另一个 `LOOM_JWT_SECRET` 签发。请使用[获取令牌](#3-get-a-token)中的 Compose 命令重新签发。                                                                     |
| 控制台返回 503                                     | 服务器从源码运行（`make server`），但没有构建前端。请运行 `make frontend-build`。                                                                                               |
| 正式发布版的栈一直不健康                           | 运行 `docker compose --project-directory "$HOME/.loom" logs --tail 100 loom clickhouse`。参见[故障排查](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md)。                                               |
| 添加仓库时返回 `SANDBOX_UNAVAILABLE`               | 该栈运行时没有仓库任务沙箱。请通过 `LOOM_CODE_REPOS_JSON` 为仓库建立索引（[为仓库建立索引](#optional-index-a-repository-on-the-released-stack)）。             |
| 安装程序提示端口 `7777` 已被占用                   | 另一个程序或另一个 Loom 正在监听该端口。请停止它，或在 `sh loom-install.sh` 前加上 `LOOM_PORT=7778` 重新运行安装命令；`.env` 会保留该端口。                      |
| 安装程序提示端口 `7777` 没有响应                   | 有程序占用了该端口但不响应，或者 `LOOM_BIND_HOST`、`LOOM_PORT` 或防火墙使本机无法访问该端口。                                                                         |
| 安装程序无法检查端口 `7777`                        | Curl 失败，原因见其退出码；请检查 `LOOM_BIND_HOST` 和 `LOOM_PORT`。                                                                                                        |
| 克隆仓库后，端口 `8123` 或 `9000` 被占用            | 重新映射 ClickHouse 的主机端口（[本地栈](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#local-stack-from-a-clone)）。                                                                                         |
| 安装程序无法拉取镜像                               | Docker 需要自行访问镜像仓库：如果处于代理之后，请为 Docker 守护进程配置[代理设置](https://docs.docker.com/engine/daemon/proxy/)。                                                 |

<a id="next"></a>

## 下一步

- [Evaluate Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)：将这次冒烟测试扩展为针对具体工作负载的质量、延迟和成本评估。
- [Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)：在此技术栈上使用 Claude Code、Codex 或 Cursor。
- [Your first agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)：通过 SDK 使用身份、弃答处理、会话、上下文包和运行。
- [Deployment](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)：为团队运行 Loom。
