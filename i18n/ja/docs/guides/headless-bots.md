<!-- i18n: source=docs/guides/headless-bots.md blob=7e255a8b2f49ab5ea38bb7947e4bde837a8a6323 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md), which is authoritative.

<a id="headless-bots"></a>

# ヘッドレスボット

ヘッドレスボットは、トークンを貼り付ける人も、サイレントな失敗に気づく人も、「これを覚えておいて」と言う人もいない状態で動作します。このガイドでは、ボットの ID、接続の確認、3 つのランタイム、ボットが書き込む内容、そして動作が止まってしまったボットの診断方法を説明します。

<a id="1-give-the-bot-an-identity"></a>

## 1. ボットにアイデンティティを与える

`loom` CLI はサーバーイメージに同梱されており、クローンから `pip install -e .` でインストールできます。SDK パッケージ（`clickhouse-loom`）には CLI がありません。

**セルフサインアップの場合。** `LOOM_SELF_SIGNUP=open` のサーバーでは、ボットが自分でサインアップします。

```bash
loom init --agent nightly-triage --server-url https://<loom> --purpose "Files the nightly failures."
```

`loom init` は次の処理を行います。

1. 名前空間 `agent-nightly-triage` を作成します（`--purpose` がその説明になります）。
2. サービスアカウントのサブジェクトに、その名前空間の `writer` を付与します。
3. トークンを `~/.loom/token`（モード 0600）に、URL、名前空間、org を `~/.loom/config.json` に書き込みます（`--token-path` と `--config-path` で保存先を変更できます）。
4. クレームコードと、クレームページのアドレス（`/oauth/agent-claim`）を表示します。担当者がそこでコードを一度入力し、自分のパスワードを設定します。以後は、名前空間名とそのパスワードで、その名前空間の管理者としてサインインします。コード自体ではサインインできず、同じコードによる 2 回目のクレームは拒否されます。ボットのトークンは引き続き使えます。

`--ttl-days` はトークンの有効期間を設定します（デフォルトは 365）。`--org` は、サーバーのセルフサインアップ用 org 以外の org を指定します。

**CI では**、ファイルを書き込む代わりにトークンを出力し、シークレットとして保存します。`--no-files` を付けると、出力の最初の行がトークンになります。

```bash
loom init --agent ci-reviewer --server-url https://<loom> --no-files | head -n 1 > token.txt
```

`--json` を付けると、サインアップ結果がトークンなしの JSON として出力されます。`--json --no-files` を併用すると、トークンはその JSON の中に含まれ、そこだけに出力されます。

**セルフサインアップを使わない場合**は、オペレーターがトークンを発行するか、担当者が **Settings → Tokens** で、ボットの名前空間のみを読み取れるトークンを作成します。

```bash
loom mint-token --subject ci-reviewer --org <org> --namespaces agent-ci --roles writer \
  --service-account --ttl-days 30 --save
```

ロール、有効期間、ローテーションについては、[トークンとグラント](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)を参照してください。

<a id="2-check-the-connection-before-the-first-run"></a>

## 2. 初回実行の前に接続を確認する

```bash
LOOM_BASE_URL=https://<loom> LOOM_API_KEY="$(cat token.txt)" loom doctor --client
```

`loom doctor --client` は、プラグインのフックや SDK と同じ方法でサーバーとトークンを解決し、それぞれの取得元を表示します。`/v1/health` と `namespace.list_mine` を呼び出し、トークンがボットの名前空間に書き込めることも確認します。問題はそれぞれ修正方法を示す `FAIL` 行として出力され、終了コードは 0 以外になるため、このコマンドは CI のゲートとして使えます。

```yaml
- run: loom doctor --client --server-url ${{ vars.LOOM_BASE_URL }} --token ${{ secrets.LOOM_API_KEY }}
```

`--json` を指定すると、チェック結果と問題の一覧を JSON で出力します。これを解析するパイプラインで利用できます。

<a id="3-connect-the-runtime"></a>

## 3. ランタイムを接続する

<a id="claude-agent-sdk-or-claude--p"></a>

### Claude Agent SDK または `claude -p`

Loom をファイルから MCP サーバーとしてマウントし、その tools だけを許可します。ヘッドレス実行には対話的な OAuth がないため、bearer はヘッダーで渡します。

```bash
cat > loom.mcp.json <<EOF
{"mcpServers": {"loom": {"type": "http",
  "url": "https://<loom>/mcp?toolset=coding",
  "headers": {"Authorization": "Bearer $LOOM_API_KEY"}}}}
EOF
claude -p --mcp-config loom.mcp.json --strict-mcp-config \
  --allowedTools "mcp__loom__*" \
  --output-format stream-json --verbose \
  "Triage the failures in the last nightly run; record each root cause you find."
```

| Flag                            | 理由                                                                                                 |
| ------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `--strict-mcp-config`           | ボットを、ユーザーの設定ではなくファイルに記載されたサーバーだけに限定します。                       |
| `--allowedTools "mcp__loom__*"` | すべての Loom tools を事前承認し、権限の確認プロンプトで実行が止まらないようにします。               |
| `--output-format stream-json`   | 最初のメッセージはセッションの `system/init` です（下記参照）。`claude -p` で使うには `--verbose` が必要です。 |
| `--bare`                        | ユーザーの hooks、プラグイン、`CLAUDE.md` をスキップし、動作を同一にします。                         |

> **Warning:** 接続の成否で実行を制御してください。`system/init` メッセージには `mcp_servers[]` が含まれ、それぞれに `status` があります。`loom` が `connected` の場合にのみ、その実行を信頼してください。そうでない場合、モデルはメモリなしで実行されています。

tool の一覧は `?toolset=` で選びます。toolset の一覧は [MCP ツールセット](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets) を参照してください。

`claude -p` を実行するユーザーに Loom プラグインがインストールされている場合は、その hooks も実行されます（[Claude Code プラグイン](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md)）。そのメンターのヒントは、プラグイン自身のサーバーの tools（`mcp__plugin_loom_loom__*`）にのみ従い、`--mcp-config` の `loom` エントリには従いません。

<a id="codex-codex-exec"></a>

### Codex `codex exec`

```bash
export LOOM_API_KEY="$(cat token.txt)"
codex exec --json \
  -c 'mcp_servers.loom.url="https://<loom>/mcp?toolset=coding"' \
  -c 'mcp_servers.loom.bearer_token_env_var="LOOM_API_KEY"' \
  "Summarise what we decided about retries; record any decision that is missing."
```

Codex は、`codex` の起動時の環境から、エントリが指定する変数の bearer を読み取ります。Codex プラグインがインストールされていれば、ここでもその hooks が実行されます（[Codex プラグイン](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md)）。

<a id="the-sdk-in-the-bots-own-process"></a>

### ボット自身のプロセス内で SDK を使う

コーディングエージェントではなくコードで書かれたボットの場合は、MCP を使わずに op を直接呼び出します。

```python
from loom import Agent

async with Agent(org="acme", namespace="agent-nightly-triage", agent="nightly-triage") as a:
    hits = await a.memory.search("flaky tests in the ingest suite", top_k=5)
    await a.memory.set(
        "flaky/ingest_retry_test",
        "Times out against the S3 mock.",
        tags=["failure", "ingest"],
    )
    ans = await a.memory.ask("which suites flaked this week?")
```

`Agent` はサーバーを `base_url=` または `LOOM_BASE_URL` から、トークンを `api_key=`、`LOOM_API_KEY`、`~/.loom/token` の順に読み取ります。そのほかのエントリポイントは次のとおりです。

- `agent.tools()` は、Loom の verb（`loom_remember`、`loom_recall`、`loom_ask`、`loom_offload`、`loom_fetch`、`loom_note`）を、1 つの `execute()` を持つ tool 定義としてモデルに渡します。
- `agent.session(id)` は、エージェントフレームワークが保持する単位で、再開、追記、フラッシュ、クローズを行えます。
- LangGraph のグラフでは `LoomStore`（`loom.integrations.langgraph`）を `compile(store=...)` に渡します。Strands のエージェントでは `LoomMemoryStore`（`loom.integrations.strands`）を使います。
- [`examples/claude_agent_loop.py`](https://github.com/ClickHouse/loom/blob/main/examples/claude_agent_loop.py) は、Anthropic SDK を使った最小のループです（`ANTHROPIC_API_KEY` が必要です）。

[Python SDK リファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md)および[フレームワーク連携](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md)を参照してください。

<a id="4-decide-what-the-bot-writes"></a>

## 4. ボットが書き込む内容を決める

プラグインは、コーディングセッションを自動的にキャプチャします。プラグインの外側にあるボットは、何を書き込むかを自分で決めます。要点は次のとおりです。トランスクリプトではなく、決定事項と失敗を記録する。ステップごとではなく、タスクの前に一度だけ想起する。完了した実行のトランスクリプトは `memory.set_from_messages` に渡す。実行が依拠したものは `run.complete(memories_used=[...])` で報告する。それぞれの選択については、[エージェント連携](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md#3-decide-what-to-write)で説明しています。

<a id="5-when-the-bot-is-quiet"></a>

## 5. ボットが反応しないとき

ボットのマシンで `loom doctor --client` を実行します。よくある原因ごとに、それぞれ 1 行で報告されます。

| 症状                                  | 原因                                                                                                |
| ------------------------------------- | --------------------------------------------------------------------------------------------------- |
| 読み取りと書き込みが別のサーバーに送られる | `LOOM_BASE_URL` が `~/.loom/config.json` と一致していない                                          |
| すべての呼び出しで 401                | トークンの有効期限が切れたか、ローテーションされた                                                  |
| すべての書き込みで 403                | ボットが書き込む名前空間で、トークンが `viewer` になっている                                        |
| キャプチャが表示されない              | バックグラウンドのフラッシャーがキャプチャを配信できなかった：一時ディレクトリに `loom-flush-*.json` ファイルが残っている |

<a id="related"></a>

## 関連ドキュメント

- [トークンとグラント](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)
- [エージェント連携](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md)
- [MCP クライアント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)
- [リクエストのデバッグ](https://github.com/ClickHouse/loom/blob/main/docs/guides/debugging.md)
