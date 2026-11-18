<!-- i18n: source=docs/getting-started/agent-setup.md blob=eb3a5968c01a25b666c59b8907efe2f2be226078 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/agent-setup.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/agent-setup.md), which is authoritative.

<a id="set-up-loom-with-your-coding-agent"></a>

# コーディングエージェントで Loom をセットアップする

このページは、Claude Code や Codex などのコーディングエージェントが、実行中のマシンに Loom をインストールし、そのインストールが正しく動作することを確認するためのものです。実行するコマンド、してはいけないこと、各チェックで確認すべき内容、そして報告する内容を示します。各ステップはそのまま実行できるように書かれており、人が行う必要があるステップには印が付いています。

各リリースには、そのバージョンとコミットが埋め込まれたこのページがプレーンテキストとして添付されています。

```text
https://github.com/ClickHouse/loom/releases/latest/download/agent-setup.txt
https://github.com/ClickHouse/loom/releases/download/v<version>/agent-setup.txt
```

<a id="pins"></a>

## ピン

<!-- pins -->

このコピーにはリリースが指定されていないため、記載のコマンドはそのままでは実行できません。インストールを依頼されたリリースの `agent-setup.txt` に従ってください。そこでは、以下の `<version>` と `<commit>` がすべて埋められています。リリースが指定されていない場合は、最新リリースのものに従ってください：`https://github.com/ClickHouse/loom/releases/latest/download/agent-setup.txt`。

<!-- /pins -->

<a id="rules"></a>

## ルール

- `sudo` を使わず、システムパッケージのインストールや変更も行わないでください。compose プラグイン付きの Docker が、あなたのユーザーですでに動作している必要があります。また、`curl`、`tar`、`python3`、Node.js 18 以降（プラグインのフックがこの上で動作します）、および使用する CLI（`claude` または `codex`）が `PATH` 上にある必要があります。どれかが欠けている場合は、そこで止めて報告してください。
- プロバイダーキーは設定せず、要求もしないでください。キーがなくてもスタックは起動し、以下のすべてのチェックが通ります。
- 手順 1 の前に、環境に `LOOM_*` 変数が設定されている場合（インストーラー、プラグイン、SDK はすべてこれらを読み取ります：ポート、バインドアドレス、別のサーバーなど）、`~/.loom` が存在する場合、または使用する CLI がすでに `loom` を認識している場合は、止めて報告してください。認識しているものとは、プラグイン（`claude plugin list`、`codex plugin list`）、マーケットプレイス（`claude plugin marketplace list`、`codex plugin marketplace list`）、MCP サーバー（`claude mcp list`、`codex mcp list`）のいずれかです。これらを変更するかどうかは、本人が判断します。いずれにも該当しない場合、スタックは `localhost:7777` のみで応答し、プラグインはそこに接続します。
- チェックを通すために弱めてはいけません。チェックスクリプトの編集、手順の省略、チェックが期待する内容の変更、実行していない手順に対する `PASS` の報告はいずれも行わないでください。`FAIL` の行は、正しい報告です。

<a id="1-install-the-release"></a>

## 1. リリースのインストール

```sh
mkdir -p ~/loom-agent-setup
curl -fsSL "https://github.com/ClickHouse/loom/releases/download/v<version>/install.sh" \
  -o ~/loom-agent-setup/loom-install.sh && sh ~/loom-agent-setup/loom-install.sh \
  && rm ~/loom-agent-setup/loom-install.sh
```

インストーラーはバンドルの SHA-256 を検証し、`~/.loom` 以下にインストールして、スタックを起動し、サーバーの `/v1/health` が応答するまで待機します。コマンドが失敗した場合は、報告するのは `install` の行だけです。それ以降の手順には進まないでください。インストーラーが行う処理の全容は、[クイックスタート](quickstart.md#1-boot-the-stack)を参照してください。

<a id="2-install-the-plugin"></a>

## 2. プラグインをインストールする

プラグインは同じリリースのソースから取得するため、サーバーと一致します。

```sh
curl -fsSL "https://github.com/ClickHouse/loom/archive/refs/tags/v<version>.tar.gz" \
  | tar -xz -C ~/loom-agent-setup
```

`~/loom-agent-setup` は残しておいてください。これがマーケットプレイスのソースになります。続いて、Claude Code の場合は次を実行します。

```sh
claude plugin marketplace add ~/loom-agent-setup/loom-<version>
claude plugin install loom@loom
```

Codex の場合は次を実行します。

```sh
codex plugin marketplace add ~/loom-agent-setup/loom-<version>
codex plugin add loom@loom
```

インストールされるスタックはシングルユーザー向けのため、プラグインはトークンなしで `http://localhost:7777` に接続します。**ユーザー本人**がエージェントの新しいセッションを開始し（Codex の場合は対話的な `codex` を実行し、そこでプラグインのフックも承認します）、このテキストをもう一度渡して「ステップ 3 から続けて」と伝えます。ステップ 3 と 4 はそのセッションで実行します。

<a id="3-run-the-check"></a>

## 3. チェックを実行する

次のスクリプトを `loom-setup-check.py` として保存し、`python3 loom-setup-check.py` を実行します。Python の標準ライブラリだけを使い、HTTP で Loom と通信します。名前空間 `agent-setup-check` に書き込み、書き込んだ内容に自身の実行を示すタグを付け、その実行の行だけを検索し、終了時にそれらを削除します。

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

各チェックで確認する内容は次のとおりです。

| チェック    | 合格する条件                                                                                                                                                  |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `server`    | URL と `~/.loom/.env` の `LOOM_BIND_HOST` がループバックであり、`/v1/health` が固定されたバージョン（リリース候補の場合は最終バージョン）とコミットを返す。 |
| `memory`    | あるセッションで書き込んだ事実が、セッションを指定しない検索（次のセッションが行う検索と同じ）の最初のヒットになり、その行自身のテキストと数値の類似度を持つ。 |
| `knowledge` | `knowledge.add` で追加したドキュメントが `knowledge.search` の最初のヒットになり、そのドキュメント自身のテキストと数値の類似度を持つ。                       |
| `cleanup`   | スクリプトが書き込んだメモリとドキュメントが削除される。                                                                                                      |

`server` が失敗した場合、スクリプトは何も書き込まず、他のチェックも実行しません。

<a id="4-check-the-agents-connection"></a>

## 4. エージェントの接続を確認する

`loom_search` ツールをクエリ `agent setup check` で1回呼び出し、`claude plugin list`（または `codex plugin list`）を実行します。ツールがエラーなしで応答し、リストに `loom@loom` が有効と表示されていれば、出力は `[PASS]  agent  loom_search answered` になります。それ以外の場合は `[FAIL]  agent  <the tool's error, or the plugin's status>` になります。

<a id="report"></a>

## レポート

スクリプトの出力行、続いて `agent` 行を示し、最後に `agent` 行も含めた集計を 1 つ、`loom doctor` の形式で示します。

```text
  [PASS]  server     http://localhost:7777 serves 1.2.3 at 0123456789ab
  [PASS]  memory     a new session finds setup/check/orchard-04211 (similarity 0.91)
  [PASS]  knowledge  knowledge.search finds setup-check://irrigation-04211 (similarity 0.77)
  [PASS]  cleanup    deleted the memory and the document it wrote
  [PASS]  agent      loom_search answered

All 5 checks passed.
```

失敗した場合、集計は `<n> check(s) failed: <names>` となり、最初の `FAIL` 行が修正すべき箇所です。実行を止めるステップは 1 行とその集計で示します。

- ステップ 1 の前で実行を止めるルール：`[FAIL]  setup  <what was missing or found>`
- インストールの失敗：`[FAIL]  install  <the command's last line>`
- ステップ 2 の失敗：`[FAIL]  plugin  <the failed command's last line>`

いずれも、その後に `1 check(s) failed: <its name>` が続きます。
