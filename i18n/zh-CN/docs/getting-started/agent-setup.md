<!-- i18n: source=docs/getting-started/agent-setup.md blob=eb3a5968c01a25b666c59b8907efe2f2be226078 model=claude-sonnet-5-5 -->
> 本页由机器从[英文原文](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/agent-setup.md)翻译，内容以英文原文为准。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/agent-setup.md), which is authoritative.

<a id="set-up-loom-with-your-coding-agent"></a>

# 使用你的编码 agent 安装 Loom

本页面向编码 agent（Claude Code 或 Codex），当它被要求在自己所运行的机器上安装 Loom 并证明安装可用时，可按本页操作。本页说明要运行什么、不要做什么、每项检查必须看到什么，以及需要给出的报告。每一步都可以直接照着运行；需要由人完成的步骤会有明确标记。

每个发布版本都会以纯文本形式附带本页，并填入对应的版本号和提交：

```text
https://github.com/ClickHouse/loom/releases/latest/download/agent-setup.txt
https://github.com/ClickHouse/loom/releases/download/v<version>/agent-setup.txt
```

<a id="pins"></a>

## 固定版本

<!-- pins -->

此副本未指明任何发行版本，因此其中的命令无法直接运行。请按照你被要求安装的发行版本中的 `agent-setup.txt` 操作，其中下面所有的 `<version>` 和 `<commit>` 均已填好；如果没有指明发行版本，请按照最新版本的操作：`https://github.com/ClickHouse/loom/releases/latest/download/agent-setup.txt`。

<!-- /pins -->

<a id="rules"></a>

## 规则

- 不要使用 `sudo`，也不要安装或更改系统软件包。带 compose 插件的 Docker 必须已能在你的用户下运行，并且 `curl`、`tar`、`python3`、Node.js 18 或更高版本（插件的 hook 在其上运行）以及你自己的 CLI（`claude` 或 `codex`）必须在 `PATH` 中；如果缺少其中任何一项，请停止并报告。
- 不要设置任何提供商密钥，也不要索要。没有密钥时，该技术栈也能启动，下面的每项检查都能通过。
- 在第 1 步之前，如果你的环境中设置了任何 `LOOM_*` 变量（安装程序、插件和 SDK 都会读取它们：端口、绑定地址、另一台服务器），如果 `~/.loom` 已存在，或者你的 CLI 已经认识某个 `loom`：插件（`claude plugin list`、`codex plugin list`）、插件市场（`claude plugin marketplace list`、`codex plugin marketplace list`）或 MCP 服务器（`claude mcp list`、`codex mcp list`），请停止并报告。是否更改其中任何一项，由使用者决定。如果以上都没有，该技术栈只在 `localhost:7777` 上响应，插件会连接到它。
- 不要为了让检查通过而削弱检查：不要编辑检查脚本、跳过某个步骤、更改检查的预期内容，也不要为未运行的步骤报告 `PASS`。一行 `FAIL` 就是一份正确的报告。

<a id="1-install-the-release"></a>

## 1. 安装发布版本

```sh
mkdir -p ~/loom-agent-setup
curl -fsSL "https://github.com/ClickHouse/loom/releases/download/v<version>/install.sh" \
  -o ~/loom-agent-setup/loom-install.sh && sh ~/loom-agent-setup/loom-install.sh \
  && rm ~/loom-agent-setup/loom-install.sh
```

安装程序会校验安装包的 SHA-256，将其安装到 `~/.loom` 下，启动整个服务栈，并等待服务器的 `/v1/health` 就绪。如果命令失败，报告中只需给出其 `install` 一行，不要继续往下进行。[快速入门](quickstart.md#1-boot-the-stack)中完整说明了安装程序所做的事情。

<a id="2-install-the-plugin"></a>

## 2. 安装插件

插件来自同一版本的源码，因此与服务器版本一致：

```sh
curl -fsSL "https://github.com/ClickHouse/loom/archive/refs/tags/v<version>.tar.gz" \
  | tar -xz -C ~/loom-agent-setup
```

请保留 `~/loom-agent-setup`：它是 marketplace 的源。然后，对于 Claude Code：

```sh
claude plugin marketplace add ~/loom-agent-setup/loom-<version>
claude plugin install loom@loom
```

对于 Codex：

```sh
codex plugin marketplace add ~/loom-agent-setup/loom-<version>
codex plugin add loom@loom
```

已安装的技术栈是单用户的，因此插件连接到 `http://localhost:7777`，无需令牌。**用户**现在需要启动一个新的 agent 会话（Codex：运行交互式 `codex`，并在其中批准插件的 hooks），再次把这段文字交给它，并说"从第 3 步继续"。第 3 步和第 4 步在该会话中运行。

<a id="3-run-the-check"></a>

## 3. 运行检查

将此脚本保存为 `loom-setup-check.py`，然后运行 `python3 loom-setup-check.py`。它只使用 Python 标准库，并通过 HTTP 与 Loom 通信。它写入命名空间 `agent-setup-check`，用本次运行自己的标签标记所写入的内容，只搜索本次运行写入的行，并在结束时删除它们。

```python title="loom-setup-check.py"
import json
import os
import pathlib
import random
import sys
import urllib.error
import urllib.parse
import urllib.request

PIN_VERSION = "<version>"
PIN_COMMIT = "<commit>"
# Loom's own tests point the check elsewhere; an agent sets neither.
BASE = os.environ.get("SETUP_CHECK_URL", "http://localhost:7777")
WHO = {"org": os.environ.get("SETUP_CHECK_ORG", "dev"), "namespace": "agent-setup-check"}
AGENT = "agent-setup"
SESSION = "setup-1"
RUN = f"{random.randrange(100_000):05d}"
TAGS = [f"setup-check:{RUN}"]
FACT = f"The setup check orchard has {RUN} pear trees."
KEY = f"setup/check/orchard-{RUN}"
DOC = f"setup-check://irrigation-{RUN}"
NOTE = f"Irrigation valve {RUN} opens at five in the morning, every second day."
LOOPBACK = ("localhost", "127.0.0.1", "::1")
checks = []


def call(op, **body):
    req = urllib.request.Request(
        f"{BASE}/v1/{op}",
        data=json.dumps({**WHO, **body}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{op} answered {e.code}: {e.read()[:200].decode(errors='replace')}")


def check(name, probe):
    try:
        grade, detail = probe()
    except Exception as e:  # any failure is this check's FAIL line
        grade, detail = "FAIL", f"{type(e).__name__}: {e}"
    checks.append((name, grade, detail))
    return grade == "PASS"


def hit_problem(hits, field, wanted, text_field):
    """Why the first hit is not the row this run wrote, or None when it is."""
    if not hits["results"]:
        return "no hits"
    top = hits["results"][0]
    if top[field] != wanted:
        return f"the first hit is {top[field]}, not {wanted}"
    if RUN not in top[text_field]:
        return f"{wanted} came back without its text"
    return None


def bind_host():
    """LOOM_BIND_HOST from the installed stack's .env, or "" when it sets none."""
    env = pathlib.Path.home() / ".loom" / ".env"
    lines = env.read_text().splitlines() if env.exists() else []
    values = [line.split("=", 1)[1].strip() for line in lines if line.startswith("LOOM_BIND_HOST=")]
    return values[-1] if values else ""


def server():
    if PIN_VERSION.startswith("<"):
        return "FAIL", "this copy names no release: use the release's agent-setup.txt"
    if urllib.parse.urlsplit(BASE).hostname not in LOOPBACK:
        return "FAIL", f"{BASE} is not a loopback address"
    host = bind_host()
    if host not in ("", *LOOPBACK):
        return "FAIL", f"~/.loom/.env publishes Loom on {host}"
    with urllib.request.urlopen(f"{BASE}/v1/health", timeout=30) as resp:
        health = json.load(resp)
    version, commit = health.get("version"), health.get("build_commit")
    # A release candidate's server reports the final version it leads to.
    final = PIN_VERSION.split("-")[0]
    if (version, commit) != (final, PIN_COMMIT):
        return "FAIL", f"serves {version} at {commit}; the pins name {final} at {PIN_COMMIT}"
    return "PASS", f"{BASE} serves {version} at {commit[:12]}"


def memory():
    call("memory.set", agent=AGENT, memory_key=KEY, value=FACT, tags=TAGS, session_id=SESSION)
    # A new session reads with no session_id: it sees what the last one wrote.
    hits = call(
        "memory.search", query="how many pear trees does the setup check orchard have", tags=TAGS
    )
    problem = hit_problem(hits, "memory_key", KEY, "content_excerpt")
    if problem:
        return "FAIL", problem
    similarity = float(hits["results"][0]["similarity"])
    return "PASS", f"a new session finds {KEY} (similarity {similarity:.2f})"


def knowledge():
    call("knowledge.add", agent=AGENT, source_uri=DOC, title="Irrigation", content=NOTE, tags=TAGS)
    hits = call("knowledge.search", query="when does the irrigation valve open", tags=TAGS)
    problem = hit_problem(hits, "source_uri", DOC, "content")
    if problem:
        return "FAIL", problem
    similarity = float(hits["results"][0]["similarity"])
    return "PASS", f"knowledge.search finds {DOC} (similarity {similarity:.2f})"


def cleanup():
    gone = call("memory.delete", agent=AGENT, memory_key=KEY, session_id=SESSION)["found"]
    dropped = call("knowledge.delete", source_uri=DOC)["deleted"]
    if not (gone and dropped):
        return "FAIL", f"memory deleted: {gone}, document deleted: {dropped}"
    return "PASS", "deleted the memory and the document it wrote"


# Nothing is written to a server the pins or the loopback rule refuse.
if check("server", server):
    check("memory", memory)
    check("knowledge", knowledge)
    check("cleanup", cleanup)
width = max(len(name) for name, _, _ in checks)
for name, grade, detail in checks:
    print(f"  [{grade}]  {name:<{width}}  {detail}")
failed = [name for name, grade, _ in checks if grade == "FAIL"]
if failed:
    print(f"\n{len(failed)} check(s) failed: {', '.join(failed)}")
    sys.exit(1)
print(f"\nAll {len(checks)} checks passed.")
```

每项检查必须看到的结果：

| 检查          | 通过条件                                                                                                                                                      |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `server`    | URL 和 `~/.loom/.env` 中的 `LOOM_BIND_HOST` 均为本地回环地址，并且 `/v1/health` 返回固定的版本（候选版本则为其最终版本）和提交。               |
| `memory`    | 在一个会话中写入的事实，是不指定会话的搜索（如下一个会话所做的搜索）的第一条命中，带有其自身的文本和数值相似度。 |
| `knowledge` | 用 `knowledge.add` 添加的文档是 `knowledge.search` 的第一条命中，带有其自身的文本和数值相似度。                                     |
| `cleanup`   | 脚本写入的记忆和文档均已删除。                                                                                                     |

当 `server` 检查失败时，脚本不会写入任何内容，也不会运行其他检查。

<a id="4-check-the-agents-connection"></a>

## 4. 检查 agent 的连接

使用查询 `agent setup check` 调用一次 `loom_search` 工具，并运行 `claude plugin list`（或 `codex plugin list`）。当该工具应答且没有报错，并且列表中显示 `loom@loom` 已启用时，输出的行是 `[PASS]  agent  loom_search answered`，否则是 `[FAIL]  agent  <the tool's error, or the plugin's status>`。

<a id="report"></a>

## 报告

先给出脚本输出的各行，再给出 `agent` 行，然后给出一条汇总，汇总中也要把 `agent` 行计算在内，格式与 `loom doctor` 相同：

```text
  [PASS]  server     http://localhost:7777 serves 1.2.3 at 0123456789ab
  [PASS]  memory     a new session finds setup/check/orchard-04211 (similarity 0.91)
  [PASS]  knowledge  knowledge.search finds setup-check://irrigation-04211 (similarity 0.77)
  [PASS]  cleanup    deleted the memory and the document it wrote
  [PASS]  agent      loom_search answered

All 5 checks passed.
```

出现失败时，汇总写作 `<n> check(s) failed: <names>`，需要修复的是第一条 `FAIL` 行。中止运行的步骤只输出一行及其汇总：

- 在第 1 步之前就中止的规则：`[FAIL]  setup  <what was missing or found>`；
- 安装失败：`[FAIL]  install  <the command's last line>`；
- 第 2 步失败：`[FAIL]  plugin  <the failed command's last line>`；

每种情况后面都跟一行 `1 check(s) failed: <its name>`。
