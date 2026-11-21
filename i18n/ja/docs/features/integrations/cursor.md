<!-- i18n: source=docs/features/integrations/cursor.md blob=ba85fce4ce13c17118f863ce5f4a67693523e037 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md), which is authoritative.

<a id="cursor"></a>

# Cursor

Cursor では、Loom は次の 3 つの要素で利用します。

| 要素 | 提供するもの | 方法 |
| ------------------ | -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| MCP ツール | Cursor のエージェントで使える `loom_*` ツール | [プラグイン](#plugin)、ローカルのシングルユーザーサーバー向けの[インストールリンク](#mcp-tools)、または `loom install --client cursor`（[MCP クライアント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)） |
| スキル | `loom` と `loom-code` のスキル | [プラグイン](#plugin)、または `npx skills add ClickHouse/loom --skill loom loom-code` |
| トランスクリプトのキャプチャ | 各会話のターンを `conversation.append` に記録 | `sessionEnd` フック `integrations/cursor/cursor-session-hook.sh` |

Cursor プラグインには MCP エントリと `loom` および `loom-code` スキルが含まれ、フックはありません。コードのシャント、ツール呼び出し・実行・ワーキングメモリのキャプチャは行われません。これらは [Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) と [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md) のプラグインで利用できます。

<a id="plugin"></a>

## プラグイン

リポジトリのルートにある `.cursor-plugin/` により、このリポジトリは Cursor プラグインのマーケットプレイスになります。含まれるプラグインは `clickhouse-loom` の 1 つで、`loom` MCP サーバーとルートの `skills/` を提供します。このプラグインは Cursor のマーケットプレイスには掲載されていません。ローカルのシングルユーザー接続と読み取り専用のメモリ検索は、Cursor でテスト済みです。Teams または Enterprise プランでは、チーム管理者が **Dashboard → Plugins & MCPs → Add Marketplace → Import from Repo** から `https://github.com/ClickHouse/loom` を指定して追加します。

プラグインの変数は **Loom MCP URL** の 1 つです。チーム管理者が Cursor のダッシュボードの **Plugins & MCPs** で設定すると、全メンバーに適用されます。

| サーバー                                   | Loom MCP URL                                             | サインイン             |
| ------------------------------------------ | -------------------------------------------------------- | ---------------------- |
| シングルユーザー、各メンバーのマシン上     | `http://localhost:7777/mcp?toolset=coding`（デフォルト） | なし                   |
| `LOOM_OAUTH_ENABLED=true` のチームサーバー | `https://<your server>/mcp`                              | ブラウザ、ユーザーごと |

OAuth が有効なサーバーには、protected-resource メタデータが示すリソースである、表のチームサーバー行の Loom MCP URL をそのまま指定します。この URL は末尾の /mcp までのパスを含み、ローカル用の ?toolset=coding クエリは付けません。デフォルトのツールセットに含まれないツールは `loom_op` 経由で実行します。このエントリは `Authorization` ヘッダーを送信しないため、OAuth が無効なチームサーバーは 401 を返します。そのようなチームではプラグインを使わず、[後述](#mcp-tools)の `loom install --client cursor` でトークン付きのエントリを書き込み、`npx skills add` でスキルを追加します。OAuth の詳細は[ブラウザでサインインするクライアント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#clients-that-sign-in-by-browser)を参照してください。

<a id="mcp-tools"></a>

## MCP ツール

同じマシン上のシングルユーザーサーバーは、トークンなしで MCP 呼び出しを受け付けます。[Cursor に Loom を追加](https://cursor.com/en/install-mcp?name=loom&config=eyJ1cmwiOiJodHRwOi8vbG9jYWxob3N0Ojc3NzcvbWNwP3Rvb2xzZXQ9Y29kaW5nIn0%3D) は、ダッシュボードが Cursor に割り当てる `coding` ツールセットで、このエントリを `mcpServers.loom` の下に追加します。リンクの `config` はこのエントリを base64 でエンコードしたものです。

```json
{ "url": "http://localhost:7777/mcp?toolset=coding" }
```

このエントリはデフォルトのポート 7777 を指しています。別のポートを使う場合は、`~/.cursor/mcp.json` の `url` を編集してください。それ以外のサーバーでは、トークンを指定してエントリを書き込みます。

```bash
loom install --client cursor --server-url https://loom.example.com --token "$LOOM_API_KEY"
```

このコマンドは、`mcpServers.loom` エントリ（`url` と `Authorization` ヘッダー）を `~/.cursor/mcp.json` にマージします。実行後は Cursor を再起動してください。ツールセット、エージェントのラベル、クライアントごとのファイル一覧表は [MCP クライアント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#wire-a-client-with-loom-install) にあります。

次に、[エージェント向けの指示](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-instructions.md) の `.cursor/rules/loom.mdc` を追加します。これにより、エージェントはいつ想起し、保存し、コードインデックスを使うかを把握できます。ただし、リポジトリの `AGENTS.md` にすでに同じブロックがある場合は不要です。

<a id="transcript-capture"></a>

## トランスクリプトのキャプチャ

自分で登録するフックです。`cursor-session-hook.sh` は、終了した会話のトランスクリプトを `loom ingest-transcript` に渡し、このコマンドが各ターンを `conversation.append` に送信します。ターンが保存されるのは、名前空間で会話ログが有効になっている場合だけです。

<a id="install"></a>

### インストール

1. Loom のチェックアウトから（`pip install -e .`）、または git から（`pip install "git+https://github.com/ClickHouse/loom"`）`loom` CLI をインストールします。`python3` が `PATH` 上にある必要があります。
2. フックを `~/.cursor/hooks/cursor-session-hook.sh` にコピーし、実行可能にします。
3. `~/.cursor/hooks.json` に登録します。ユーザーファイル内のパスは `~/.cursor/` からの相対パスです。プロジェクトの `.cursor/hooks.json` では、プロジェクトのルートから解決されます。

   ```json
   {
     "version": 1,
     "hooks": {
       "sessionEnd": [{ "command": "./hooks/cursor-session-hook.sh", "timeout": 150 }]
     }
   }
   ```

4. フックに環境変数を渡します。Dock から起動したアプリはシェルのプロファイルを読み込まないため、トークンは `~/.loom/token` に置き（`loom mint-token --save` が書き込みます）、残りは `hooks.json` から指すラッパーで設定します。

   ```bash
   #!/usr/bin/env bash
   export LOOM_BASE_URL=https://loom.example.com LOOM_NAMESPACE=my-namespace
   exec "$HOME/.cursor/hooks/cursor-session-hook.sh" "$@"
   ```

5. 名前空間の会話ログを有効にします（ダッシュボードの Conversations ページにある **Record conversations** スイッチ、または `conversation.set_logging`）。有効にするまで、サーバーはターンを受け付けますが、何も保存しません。

| 変数                         | デフォルト              | 意味                                                                                            |
| ---------------------------- | ----------------------- | ----------------------------------------------------------------------------------------------- |
| `LOOM_NAMESPACE`             | 必須                    | スレッドの書き込み先の名前空間                                                                  |
| `LOOM_API_KEY`               | `~/.loom/token`         | トークン。CLI は環境変数から読み取るため、コマンドラインには現れません                          |
| `LOOM_BASE_URL`              | `http://localhost:7777` | Loom サーバー                                                                                   |
| `LOOM_AGENT`                 | `cursor`                | 会話の行の `agent`                                                                              |
| `LOOM_BIN`                   | `loom`                  | 実行する CLI                                                                                    |
| `LOOM_CAPTURE_CONVERSATIONS` | `true`                  | `false` にすると、フックは何もしなくなります                                                    |
| `LOOM_INGEST_MAX_BYTES`      | `16777216`              | これを超えるトランスクリプトは切り詰められず、スキップされます                                  |
| `LOOM_INGEST_TIMEOUT`        | `120`                   | CLI の実行を許可する秒数（`timeout` または `gtimeout` が必要）。`hooks.json` のタイムアウトより小さくしてください |

<a id="what-the-hook-reads"></a>

### フックが読み取るもの

Cursor はフックをプロセスとして起動し、その標準入力に JSON オブジェクトを 1 つ書き込みます。フックは 2 つのフィールドを読み取り、残りは無視します。

| フィールド        | 用途                                                                                         |
| ----------------- | -------------------------------------------------------------------------------------------- |
| `transcript_path` | トランスクリプトファイル。トランスクリプトが無効の場合は `null` で、その場合は何も送信されません |
| `conversation_id` | スレッド ID `cursor-<conversation_id>`（使用できるのは `[A-Za-z0-9._:-]` のみで、最大 128 文字） |

パスのフォールバックとして、環境変数の `CURSOR_TRANSCRIPT_PATH` を使用します。使用可能な `conversation_id` がない場合、スレッド ID は `cursor-` にトランスクリプトのパスのハッシュを付けたものになります。`sessionEnd` を登録してください。`stop` でも動作しますが、エージェントのターンごとにトランスクリプト全体を再送信します。

フックは、Cursor が `~/.cursor/projects/<project>/agent-transcripts/` に書き込む JSONL を読み取ります。1 行に 1 オブジェクトで、`role` と `message.content`（文字列またはブロックのリスト）を持ちます。`{role, content}` の `.json` 配列や、`# user` / `# assistant` セクションを持つ `.md` ファイルは、変更せずにそのまま渡されます。それ以外のファイルは、stderr にメッセージを出力してスキップされます。

<a id="capture-boundary"></a>

### キャプチャの範囲

- **送信されるもの：** `user` と `assistant` の行のテキストを、ファイル内の順序で送信します。対象はテキストブロックのみです。`tool_use` ブロック、ツールの結果、thinking、その他すべてのロールは、マシン上で破棄されます。
- **送信されないもの：** フック入力の `user_email`、`workspace_roots`、モデルに関するフィールドは送信されません。書き込み主体はトークンが指す者であり、行に含まれるのは `agent` だけで、Cursor アカウントの情報は含まれません。
- **マスキングされないもの：** ターンのテキストは書かれたとおりに送信されます。キャプチャするチャットにシークレットを含めないか、名前空間のログを無効のままにしてください。

<a id="behaviour"></a>

### 動作

- **Cursor をブロックしません。** フックモードでは、どの結果でも終了コード 0 で終了し、stdout に `{}` を出力します。CLI の出力は stderr に送られます。
- **Loom に到達できなくても安全です。** フックは 1 行ログを出力して終了コード 0 で終了し、再試行はしません。次回の実行でトランスクリプトを再送信し、`conversation.append` はすでに保存されたターンをスキップします（[ターン ID](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md#transcript-capture-hooks)）。
- **上限があります。** フック入力は最大 1 MiB、トランスクリプトは `LOOM_INGEST_MAX_BYTES` まで、CLI は `LOOM_INGEST_TIMEOUT` 秒まで読み取り・実行されます。
- **出力は最小限です。** 診断メッセージは stderr への 1 行で、先頭に `loom-cursor-hook:` が付き、トランスクリプトのテキストやトークンは含まれません。
- **手動モード。** `cursor-session-hook.sh <transcript>` は 1 つのファイルを取り込み、CLI の終了コードで終了します（`2` は設定エラー、`3` はトランスクリプトがスキップされた場合、それ以外は CLI の終了コード）。

<a id="limits"></a>

## 制限事項

- 保存されたターンは記録であり、抽出元ではありません。そこから何かが抽出されることはありません。会話からメモリを書き込むには、そのターンを `memory.set_from_messages` に送信してください。
- トランスクリプトの形式は Cursor 独自のもので、Cursor によるドキュメントはありません。形式が変更されると、stderr に "no user or assistant text in the transcript" と表示されます。

<a id="related"></a>

## 関連情報

- [インテグレーション](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md#transcript-capture-hooks)：その他のトランスクリプトテンプレート。
- [MCP クライアント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)。
- [会話とセッション](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md)。
- [ソース README](https://github.com/ClickHouse/loom/blob/main/integrations/cursor/README.md)。
