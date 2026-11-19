<!-- i18n: source=docs/features/integrations/codex.md blob=31b4bde9426699448af1249d56124eacf571d7b9 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md), which is authoritative.

<a id="codex-plugin"></a>

# Codex プラグイン

Codex プラグインは、Claude Code と同じ Loom 連携を Codex に提供します。MCP ツール、セッションを記録してプライマーを読み込むフック、コードシャントが含まれます。スクリプト、スキル、リスティング用アイコン（`assets/`）は Claude Code プラグインのものから生成したコピーであり、`integrations/codex-plugin/hooks/hooks.json` の配線だけが異なります。フックと設定項目については [claude-code.md](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) を参照してください。例外は [Claude Code との違い](#what-differs-from-claude-code) にまとめています。
プラグインが何をどこへ送信するかについては、[データの取り扱い](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#data-handling) を参照してください。

<a id="install"></a>

## インストール

```bash
codex plugin marketplace add ClickHouse/loom        # or a path to a local clone
codex plugin add loom@loom
```

プラグインは macOS、Linux（glibc または musl。WSL 2 を含む）、Windows の x86_64 と arm64 で動作します。macOS と Linux では、フックは `bin/loom-node` を通じて Node を見つけます。ヘッダーヘルパーは `PATH` 上の `node` を実行し、見つからない場合はランチャーにフォールバックします。Windows では、どちらも `cmd.exe` 経由で実行され、Codex の起動時の `PATH` 上にある `node` を使用します。

プラグインは独自の MCP エントリ（`.mcp.json`）を持っています。サーバー URL は `?toolset=core,coding,tracker,tasks` 付きで、プラグインのヘッダーヘルパー `bin/loom-mcp-headers.mjs` が認証します。これに加えて `codex mcp add loom` を実行しないでください。同じ名前のエントリはプラグインのエントリを置き換え、別の名前のエントリはすべてのツールを二重に一覧表示します。

<a id="approve-the-hooks"></a>

### フックの承認

Codex は、ユーザーが承認した後にのみプラグインのフックを実行します。コマンドが変わる更新の後にも、再度承認が必要です。インストール後に一度 `codex` を対話的に実行し、Loom のフックを承認してください。Codex は各承認を `~/.codex/config.toml` の `[hooks.state]` 配下に `trusted_hash` として記録します。承認するまでは MCP ツールは動作しますが、`codex exec` の場合も含め、セッションはキャプチャされません。

<a id="the-token"></a>

### トークン

同じマシン上の単一ユーザーのサーバーにはトークンは不要です。それ以外の場合、ヘルパーとフックは `LOOM_API_KEY`、`~/.loom/config.json`、`~/.loom/token` の順に読み取ります。エージェント（ワークロードトークンを持つ `LOOM_CLIENT_ID`）は、`LOOM_API_KEY` が設定されていればそれを使い、設定されていなければ交換したトークンのみを使います。`LOOM_SELF_SIGNUP=open` のサーバーでは、認証情報のない最初のセッションがマシンをサインアップし、`~/.loom/config.json` を自動的に書き込みます（`LOOM_AUTO_SIGNUP=off` で無効になります）。すでに持っているトークンを使うには、次のようにします。

```bash
mkdir -p ~/.loom && cat > ~/.loom/config.json <<'EOF'
{ "url": "https://loom.example.com", "token": "<writer JWT>" }
EOF
chmod 600 ~/.loom/config.json
```

トークンは、ダッシュボードの **Settings → Tokens** タブ（`<server>/settings`）から、または `loom mint-token --subject you --org <org> --namespaces <ns> --roles writer` で取得します。フックには `writer` が必要で、ツールは `viewer` でも読み取りができます。詳しくは[トークンとグラント](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)を参照してください。

<a id="the-server"></a>

### サーバー

Codex のプラグインにはインストールオプションがないため、URL はプラグインの `.mcp.json`（出荷時は `http://localhost:7777`）と、フック用の `plugin-defaults.json` に固定されています。独自のマーケットプレイスを公開するデプロイメントでは、両方を設定し、名前を `loom`（`.agents/plugins/marketplace.json`）のままにします。スキルとヘッダーヘルパーは、そのマーケットプレイスのコピーだけを読み取ります。

1 台のマシンで別のサーバーを使う場合は、`~/.codex/config.toml` のエントリを上書きし（上書きはヘルパーを含めてプラグインのエントリ全体を置き換えます）、フック用に同じ URL を `~/.loom/config.json` に記述します。

```toml
[mcp_servers.loom]
url = "https://loom.example.com/mcp?toolset=core,coding,tracker,tasks"
http_headers_helper = '''node -e "const f=require('fs'),p=require('path');if(process.versions.node.split('.')[0]<18)process.exit(1);const d=p.join(require('os').homedir(),'.codex','plugins','cache','loom','loom');const k=v=>v.split(/[.-]/).map(x=>x.padStart(9,'0')).join('.');const v=f.readdirSync(d).filter(x=>f.existsSync(p.join(d,x,'bin','loom-node'))).sort((a,b)=>k(a)<k(b)?-1:1).pop();import(require('url').pathToFileURL(p.join(d,v,'bin','loom-mcp-headers.mjs')).href)" || { l="$(ls -d "$HOME"/.codex/plugins/cache/loom/loom/*/bin/loom-node 2>/dev/null | sort -V | tail -1)"; sh "$l" "${l%/*}/loom-mcp-headers.mjs"; }'''
```

Codex は、macOS と Linux ではヘルパーを `sh -c` 経由で、Windows では `cmd.exe` 経由で、縮小された環境（`HOME` または `USERPROFILE`、`PATH`、ロケール、その他いくつか）で実行します。また `${PLUGIN_ROOT}` は展開しません。そのためコマンドは、どちらのシェルでも実行できる `node -e` で始まり、ホームディレクトリの `.codex` 配下にインストールされている最新の `loom@loom` のヘッダーヘルパーを読み込みます。`sh` では、この手順が失敗した場合（その `PATH` 上の `node` が存在しない、または 18 より古い、あるいはインストール済みのコピーが見つからない場合）、インストール済みの最新のランチャー（`bin/loom-node`）にフォールバックします。このランチャーは、`HOME` 配下のバージョンマネージャーのデフォルトディレクトリや、一般的なインストール先ディレクトリも探します。`LOOM_NODE` や `NVM_DIR` などの変数は、ランチャーには渡されません。Windows では、`PATH` 上の `node` が実行されます。`CODEX_HOME` をカスタマイズしている場合は、パスを明示的に記述してください。ヘッダーヘルパーからの `Authorization` を拒否する Codex ビルド（"returned a reserved header"）では、さらに `bearer_token_env_var = "LOOM_API_KEY"` が必要で、`LOOM_API_KEY` を export しておきます。

`loom install --client codex --server-url https://loom.example.com --token <JWT>` は、`bearer_token_env_var` と `?toolset=coding` を含むこのような上書きを書き込み、フック用に `url` と `token` を `~/.loom/config.json` に書き込みます（[mcp-clients.md](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#codex-without-the-plugin)）。

<a id="where-things-live"></a>

### 各ファイルの場所

| 項目        | 場所                                                                                                                     |
| ----------- | ------------------------------------------------------------------------------------------------------------------------ |
| MCP エントリ | プラグインの `.mcp.json`。`~/.codex/config.toml` の `[mcp_servers.loom]` はこれを全体として上書きします                  |
| Bearer      | `LOOM_API_KEY` → `~/.loom/config.json` の `token` → `~/.loom/token`                                                      |
| フック設定  | `~/.loom/config.json` の `{url, token, namespace?, org?}`。`LOOM_BASE_URL` で上書きされます。`url` がない場合は `plugin-defaults.json` |
| プラグイン  | `~/.codex/plugins/cache/loom/loom/<version>/`                                                                            |

<a id="what-differs-from-claude-code"></a>

## Claude Code との違い

| Codex                                                            | 影響                                                                                                                                                                                                                                                                                                                                                                       |
| ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Read` / `Grep` ツールがなく、ファイルはシェルツールで読み取る  | コードシャントはシェルツール（`Bash`、`shell`、`exec_command`、`local_shell`）を監視し、`cat`、`rg`、`grep -r` を検出します。コマンドは文字列でも argv 配列でもかまいません                                                                                                                                                                                                |
| 編集は `apply_patch` を経由する                                  | ファイルコンテキストと影響範囲は、パッチのテキスト（`*** Update File:` / `*** Add File:`）から対象パスを読み取ります                                                                                                                                                                                                                                                       |
| `transcript_path` が存在しない、または Codex 独自の形式の場合がある | フックはターンジャーナルを保持します。`loom-run` が各プロンプトを、`loom-conversation` が Stop 時に各応答（`last_assistant_message`）を記録します。作業メモリのノート、run のクローズ、会話プレーン、セッションメモリがこれを読み取ります                                                                                                                                      |
| ペイロードにターンごとのトークンカウンターがない                 | `loom-usage` は組み込まれません                                                                                                                                                                                                                                                                                                                                            |
| `SessionEnd` フックに与えられる時間は約 1 秒                     | ここで実行されるのは投げっぱなしのスクリプト（キャプチャ、run のクローズ、セッションメモリ、セッションブリーフ）のみです。会話のターンは Stop 時に送信されます                                                                                                                                                                                                              |
| `PostToolUseFailure` イベントがない                              | エラーとしてフラグが付いた `PostToolUse` の結果（`is_error`、`isError`、`error`）は、タイプ `error` として観測されます                                                                                                                                                                                                                                                     |
| mentor-hint フックはすべてのツールに一致する                     | Loom の `hint` を持たない結果は無視し、独自のリクエストは行いません                                                                                                                                                                                                                                                                                                        |
| フックは `$SHELL -lc` 経由で、Windows では `cmd.exe` 経由で実行される | 各コマンドは `bin/loom-node` を通してスクリプトを起動します。これは、`PATH`、バージョンマネージャー配下、または通常のインストールディレクトリから Node 18+ を探します。Windows では `commandWindows` がシステムの `PATH` にある `node` を実行します。Codex は 0.131 からこれを読み取ります。それより前のビルドでは Windows 上で `sh` コマンドが `cmd.exe` 経由で実行されて失敗するため、Windows には Codex 0.131 以降が必要です |
| 識別子                                                           | run ID は `cx-<session_id>`、デフォルトのエージェントは `codex`、セッションメモリには `source:codex` が付きます                                                                                                                                                                                                                                                            |

SessionStart は `startup`、`resume`、`clear`、`compact` で発火します。プライマーは stdout に出力され、Codex がこれを開発者コンテキストとして追加します。`hooks/hooks.json` のすべてのコマンドは、スクリプトの第 3 引数でクライアントを指定します（`… PostToolUse codex`）。

<a id="skills"></a>

## スキル

プラグインには `loom-memory`、`loom-code`、`loom-mentor`、`loom-optimize`、`loom-session`、`loom-status`、`improve-loom-for-my-repo` が含まれています。詳細は [claude-code.md](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#skills) を参照してください。Codex はプラグインからこれらを読み込みます。名前を指定して呼び出す（`$loom-status`）か、説明文によって自動的に呼び出されるようにします。

<a id="agentsmd-without-the-plugin"></a>

## プラグインなしの AGENTS.md

プラグインを使う場合は、上記のスキルがコントラクトを担うため、`AGENTS.md` には何も記述する必要がありません。プラグインを使わない Codex は、リポジトリのルートにある `AGENTS.md` から[エージェント向け指示](agent-instructions.md)のブロックを読み込みます。このブロックにはそのリポジトリの名前空間が記載されるため、グローバルな `~/.codex/AGENTS.md` には置きません。

<a id="headless"></a>

## ヘッドレス

プラグインを実行するユーザー向けにインストールしてあれば、フックは `codex exec` の下で実行されます。
プラグインのエントリを使わずに、1 回の実行だけサーバーを指定するには、次のようにします。

```bash
codex exec --json \
  -c 'mcp_servers.loom.url="https://loom.example.com/mcp?toolset=coding"' \
  -c 'mcp_servers.loom.bearer_token_env_var="LOOM_API_KEY"' \
  "summarise what we decided about retries"
```

通常のコーディング作業には `?toolset=coding` だけで十分です。ツール一覧を小さくするには `&schema_profile=compact` を追加します（[MCP リファレンス](../../reference/mcp.md)）。スケジュール実行や CI での実行については、[ヘッドレスボットガイド](../../guides/headless-bots.md)を参照してください。

<a id="checking-it-works"></a>

## 動作の確認

- `codex mcp list` に `loom` が表示されます。最初のツール呼び出しで、ダッシュボードの **Settings → Tokens** タブにあるトークンの行が緑色に変わります。
- 1 回のセッションの後：（名前空間でログ記録が有効な場合）ダッシュボードの Conversations ページにその会話が表示され、`cx-<session_id>` に対する `run.get` でそのランの observation を確認でき、次回のプライマーはワーキングメモリのノートから始まります。
- 何も届かない場合：`loom-status` スキルを実行してください。このスキルは Node ランタイムを確認し、プラグインの `bin/loom-client-status.mjs` を実行します。Loom のチェックアウトや Python は不要です。`loom doctor --client`（オペレーター向け CLI）を使うと、配線のチェックも追加されます。

<a id="troubleshooting"></a>

## トラブルシューティング

| 症状                                           | 原因                                                   | 対処                                                                                                     |
| ---------------------------------------------- | ------------------------------------------------------ | -------------------------------------------------------------------------------------------------------- |
| MCP 接続が 401 で失敗する                      | ヘルパーがトークンを見つけられない                     | `~/.loom/config.json` を書き込むか、`LOOM_API_KEY` を export する                                        |
| MCP 接続が 401 で失敗するが、フックは動作する  | ヘルパーが Node を見つけられず、`LOOM_NODE` が渡されない | Codex の起動時の `PATH` に Node のディレクトリを含める。詳細は `$loom-status` に表示される               |
| 「予約済みヘッダーを返した」というエラーが出る | この Codex ビルドは、ヘルパーからの `Authorization` を拒否する | `bearer_token_env_var = "LOOM_API_KEY"` で上書きする                                                     |
| ツールが別のサーバーに接続する                 | 同梱の URL がお使いのデプロイと異なる                  | 上記の `config.toml` の上書きに加えて、`~/.loom/config.json` の `url` を設定する                         |
| ツールが二重に表示される                       | 別の名前で 2 つ目の MCP エントリがある                 | 削除する（`codex mcp remove <name>`）                                                                    |
| フックで `node: not found` と表示される        | インストール済みのプラグインが Node ランチャーより古い | `codex plugin marketplace upgrade loom`                                                                  |
| フックが Node.js 18+ に関する `loom:` の行を出力する | ランチャーが Node 18+ を見つけられない、または古い Node しか見つからない | Codex の起動場所で `LOOM_NODE=/path/to/node` を設定する（macOS、Linux）、または `$loom-status` を使う   |
| フックが何も書き込まない                       | ビューアートークンを使っている、またはフックが別の `url` を読んでいる | ライタートークンを使う。`~/.loom/config.json` と `LOOM_BASE_URL` を確認する                              |
| 会話ページが空                                 | 名前空間で会話ログが無効になっている                   | ダッシュボードの会話ページにある **会話を記録する** スイッチ、または `conversation.set_logging`          |

<a id="related"></a>

## 関連項目

- [Claude Code プラグイン](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md)：フックのリファレンスとすべての設定項目。
- [MCP クライアント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)と [MCP リファレンス](../../reference/mcp.md)。
- [インテグレーション](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md)。
