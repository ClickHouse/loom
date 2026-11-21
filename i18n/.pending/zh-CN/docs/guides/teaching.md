<!-- i18n: source=docs/guides/teaching.md blob=5ff57fa6e6aa684faaf742b9ce4b6c698eda6682 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/guides/teaching.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/teaching.md), which is authoritative.

<a id="teaching-with-loom"></a>

# 用 Loom 教学

Loom 可以作为 AI agent、信息检索、数据库或软件工程课程的实验环境。每位学生运行各自的单用户堆栈，可以在装有 Docker 的笔记本电脑上运行，也可以在 GitHub codespace 中运行，无需在本机安装任何软件。每个实验都链接到完成相应工作的页面。

<a id="setup"></a>

## 环境准备

| 学生具备的条件                 | 起步方式                                                                                    |
| ------------------------------ | ------------------------------------------------------------------------------------------- |
| GitHub 账号和浏览器            | [在 GitHub Codespaces 中试用](../getting-started/quickstart.md#try-it-in-github-codespaces) |
| 自己的机器上装有 Docker        | [快速入门：快速路径](../getting-started/quickstart.md#fast-path-released-image-no-clone)    |

Codespace 在运行期间以及保留其磁盘期间，都会占用学生的 Codespaces 配额；如果闲置超过保留期，GitHub 会将其连同数据一并删除（[计费](https://docs.github.com/en/billing/concepts/product-billing/github-codespaces)，[自动删除](https://docs.github.com/en/codespaces/setting-your-user-preferences/configuring-automatic-deletion-of-your-codespaces)）。开发容器的 `hostRequirements` 规定了它可运行的最小机器配置（[`.devcontainer/devcontainer.json`](https://github.com/ClickHouse/loom/blob/main/.devcontainer/devcontainer.json)）。

默认安装不需要 API 密钥：它使用内置的本地模型生成嵌入，因此搜索可以离线进行，并且没有任何写入判定会调用 LLM。提取、摘要和生成式回答（`memory.ask`、`knowledge.synthesize`）需要真正的 LLM（[模型与 API 密钥](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)）；下面的练习只用到写入、读取和搜索。

实验 1 以及实验 2 中的 SDK 方式，需要在虚拟环境中安装 Python SDK，可在笔记本电脑的终端或 codespace 中进行。在 Debian 或 Ubuntu 上（包括 codespace 和 WSL），请先运行 `sudo apt-get update && sudo apt-get install -y python3-venv`，再执行下面的命令。SDK 需要 Python 3.11 或更高版本；运行 `python3 --version` 可查看已安装的版本。没有 Python 3.11 或更高版本的学生可以使用 codespace，这一步会在其中安装足够新的 Python。

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install clickhouse-loom
```

实验中的 `docker compose` 命令在同一个终端中运行，作用于该技术栈的容器，并以 `<dir>` 表示其安装目录：在笔记本电脑上是 `~/.loom`，在 codespace 中是 `/workspaces/.loom`。

<a id="hand-out-the-labs-as-an-assignment"></a>

## 将实验作为作业布置

[课程入门项目](https://github.com/ClickHouse/loom/blob/main/examples/course-starter/README.md)是为每位学生准备的仓库，包含实验 1 和实验 2 的代码以及实验 4 的查询，其中的空缺处以 `TODO` 标出；此外还有用于书面练习的答案页、在 codespace 中安装最新版本并带有 Python SDK 和 Claude Code 的 dev container，以及评分工作流。将它复制到你的账户或学校组织下的新仓库中，并把该仓库设为模板（需要 `curl`、`tar`、`git` 和已登录的 [GitHub CLI](https://cli.github.com/)）：

```bash
mkdir loom-labs && cd loom-labs
curl -fsSL https://github.com/ClickHouse/loom/archive/refs/heads/main.tar.gz \
  | tar -xz --strip-components=3 loom-main/examples/course-starter
git init -b main && git add . && git commit -m "Loom labs"
gh repo create <owner>/loom-labs --public --source . --push
gh repo edit <owner>/loom-labs --template
```

推送会在仓库成为模板之前运行一次评分工作流，这次运行按设计会失败：每项检查的输出行都会指出入门项目中仍待编写的部分。复制下来的是 `main` 上的入门项目，并针对 `main` 进行测试，而每次运行安装的都是最新版本，因此若因其他原因失败，说明两者存在差异：请在下一个版本发布后重新复制入门项目。之后对模板的推送不会被评分。

随后，每位学生在由该模板创建的仓库中工作，可以使用模板页面上的 **Use this template**，或使用你布置作业所用的工具，然后在 codespace 中打开，或克隆到笔记本电脑上。学生需要有模板的读取权限才能使用它：公开模板不含答案，私有模板则适用于其组织的成员或你添加的协作者。

`python grading/grade.py` 会针对学生正在运行的堆栈执行实验代码，检查其写入和返回的内容，而不是实现过程，因此也请阅读代码。`Grade` 工作流会在每次推送时运行同样的检查，使用 GitHub Actions runner 上全新的堆栈，并将检查结果表写入该次运行的摘要；只要有检查未通过，运行就会失败。每项检查在未改动的入门项目上都会失败，且不需要 API key。实验 3 和答案页由人工评分：它们的练习要求给出解释。

一次评分运行约需两分钟，大部分时间用于启动堆栈。在私有仓库中，这些分钟数计入拥有该仓库的账户或组织的 Actions 配额（[Actions 计费](https://docs.github.com/en/billing/concepts/product-billing/github-actions)）；codespace 的费用见[安装](#setup)中的说明。

<a id="lab-1-an-agent-that-remembers"></a>

## 实验 1：会记忆的 agent

使用 Python SDK 构建一个小型 agent：包含一个身份、它保存并读回的事实、一个会话和一次运行。请按照[你的第一个 agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)进行操作。

练习：

- 用 `memory.search` 搜索一个没有任何已保存事实能回答的问题，并解释回复中携带的 `abstain_code`（[搜索与提问](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)）。
- 保存一个事实，再在另一个键下保存一个相近但不同的值，并查看写入判定结果（[写入如何被判定](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md#how-a-write-is-judged)）。
- 修订一个事实，并用 `memory.history` 读取它的各个版本（[历史与来源](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md#versions-memoryhistory)）。

<a id="lab-2-document-search-with-citations"></a>

## Lab 2：带引用的文档搜索

用 `loom demo` 摄取几份文档。在默认的堆栈（fake LLM）上，它会针对问题打印 `knowledge.search` 的前几条命中结果，而不是给出答案（见[就文档提问](../getting-started/quickstart.md#optional-ask-a-question-about-a-document)）。`--file` 路径位于容器内部，容器的 `/app/docs/` 下存放着这些文档。先把你自己的文件复制进去（这里是当前目录下的 `notes.md`），并使用能标识该文件的名称，因为 `loom demo` 按路径给文档命名：复制到相同路径的第二个文件会替换第一个文件，成为它的下一个修订版本。

```bash
docker compose --project-directory <dir> exec loom loom demo --file /app/docs/getting-started/quickstart.md --question "How do I get a token?"
docker compose --project-directory <dir> exec -T loom sh -c 'cat > /tmp/notes.md' < ./notes.md
docker compose --project-directory <dir> exec loom loom demo --file /tmp/notes.md --question "<a question the file answers>"
```

然后通过 HTTP（在单用户堆栈上不需要 `Authorization` 请求头）或 SDK，用 `knowledge.search` 搜索该命名空间的文档（见[知识文档](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md)）。SDK 的 `knowledge.ingest_file` 可以直接发布本地文件，无需先复制到容器中；课程起始项目中的 `labs/lab2.py` 会用到它。

练习：

- 针对十个问题，标注能回答每个问题的文档和段落，并在 `knowledge.search` 上度量 recall@k（见[评估你的工作负载](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md#memory-and-rag)）。
- 加入文档无法回答的问题。`knowledge.search` 仍然会为这些问题返回命中结果，因此请比较可回答问题和不可回答问题的命中结果中最高的 `similarity`（即 1 减去余弦距离），并选择一个能将二者区分开的截断值。在默认的混合模式下，命中结果的 `score` 是融合后的排名，无法区分它们。

<a id="lab-3-a-code-index"></a>

## 实验 3：代码索引

在栈上为一个仓库建立索引
（[为仓库建立索引](../getting-started/quickstart.md#optional-index-a-repository-on-the-released-stack)）。
然后通过 HTTP，使用 `POST /v1/code.<op>`，询问某个符号定义在哪里、谁调用了它，以及对它的修改会影响哪些部分（[代码索引能回答什么](../features/code-index/overview.md#what-it-answers)）。单用户栈不会自动补全 `code.*` 调用，因此每个请求体都要写明 `"org": "dev"` 以及仓库的 `"namespace"`（快速入门示例中为 `code-myrepo`）。

连接之后，coding agent 也可以提出同样的问题
（[连接 coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)）。它运行在其自身提供方的模型上，因此需要在那里登录或使用 API key；该栈不提供这些。

练习：

- 选取五个函数，将 `code.callers` 的结果与 `grep` 的结果进行比较，并解释每一处差异（[如何解读答案](../features/code-index/overview.md#reading-an-answer)）。
- 在已建立索引的仓库的克隆中修改一个函数，并将 diff 作为 `diff` 发送给 `code.impact`
  （[agent 如何使用它](../features/code-index/overview.md#how-an-agent-uses-it)）。该命令需要 `jq`（在 Debian 或 Ubuntu 上使用 `sudo apt-get install -y jq` 安装）：

  ```bash
  git diff | jq -Rs '{org: "dev", namespace: "code-myrepo", diff: .}' \
    | curl -s -X POST http://localhost:7777/v1/code.impact -H 'Content-Type: application/json' -d @-
  ```

<a id="lab-4-memory-as-a-database"></a>

## Lab 4：作为数据库的记忆

Loom 将数据以行的形式保存在 ClickHouse 表中，只读的 ClickHouse 用户通过视图读取某个命名空间（[SQL 接口](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)）。为该栈的命名空间 `local` 创建一个这样的用户（第一条命令会打印其密码），然后以该用户身份查询：

```bash
docker compose --project-directory <dir> exec loom loom sql-access grant --user student1 --org dev --namespaces local
docker compose --project-directory <dir> exec clickhouse clickhouse-client --user student1 --password <password>
```

练习：

- 针对 `loom.sql_v1_memories` 编写一条查询，统计每个 agent 在最近一小时内创建的事实数量（按 `created_at` 统计，修订会保留该字段）。
- 比较 `memory.search` 的结果与针对相同行的 SQL 查询结果，并解释排序带来了什么。

<a id="which-labs-fit-which-course"></a>

## 哪些实验适合哪门课程

| 课程      | 实验    | 接下来阅读                                                                                                |
| --------- | ------- | --------------------------------------------------------------------------------------------------------- |
| AI 智能体 | 1, 2, 3 | [将 Loom 集成到 agent 中](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md)，[记忆](../concepts/memory.md) |
| 信息检索  | 1, 2    | [搜索与提问](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)，[评估 Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md) |
| 数据库    | 1, 4    | [存储权威](../architecture/storage-authority.md)，[SQL 接口](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md) |
| 软件工程  | 3       | [代码索引](../features/code-index/overview.md)，[PR 历史](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) |
