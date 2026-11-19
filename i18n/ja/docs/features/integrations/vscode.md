<!-- i18n: source=docs/features/integrations/vscode.md blob=3cffbda831d94714b443b5f9d0a7ffe59bbc0ca5 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/vscode.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/vscode.md), which is authoritative.

<a id="vs-code"></a>

# VS Code

VS Code のエージェントモード（GitHub Copilot）は、streamable HTTP 経由のリモート MCP サーバーとして Loom に接続します。VS Code 拡張機能はありません。`loom_*` ツールがそのまま連携の手段であり、セッションのキャプチャやコードの shunt はありません。これらは [Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) と [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md) のプラグインが備えています。

<a id="local-single-user-server"></a>

## ローカルのシングルユーザーサーバー

同じマシン上のシングルユーザーサーバーは、トークンなしで MCP 呼び出しを受け付けます。[VS Code に Loom を追加](https://vscode.dev/redirect?url=vscode%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D) ・[VS Code Insiders に Loom を追加](https://insiders.vscode.dev/redirect?url=vscode-insiders%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D) は、ユーザープロファイルの `mcp.json` に `servers.loom` エントリを追加します。`type` と `url` は、ダッシュボードおよび `loom install --client vscode` が VS Code に渡すものと同じです。リンクのペイロードは `vscode:mcp/install?<payload>` の形で URL エンコードされており、内容は次のとおりです。

```json
{ "name": "loom", "type": "http", "url": "http://localhost:7777/mcp" }
```

リンクを開くと、VS Code のインストール確認が表示されます。ターミナルからは `code --add-mcp '{"name":"loom","type":"http","url":"http://localhost:7777/mcp"}'` でも同じ操作ができます。コードインデックスを使うコーディングセッションでは、`url` に `?toolset=coding` を追加します（[ツールセットの選択](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#choosing-a-toolset)）。

<a id="any-other-server"></a>

## その他のサーバー

トークンを含めてエントリを書き込みます。

```bash
loom install --client vscode --server-url https://loom.example.com --token "$LOOM_API_KEY" --toolset coding
```

このコマンドは、`servers.loom` エントリ（`type: "http"`、`url`、`Authorization` ヘッダー）をユーザープロファイルの `mcp.json` にマージし、`settings.json` には手を加えません。トークンをファイルに残したくない場合は、VS Code が一度だけ入力を求めて保存する input を使い、エントリを手で書きます。

```json
{
  "servers": {
    "loom": {
      "type": "http",
      "url": "https://loom.example.com/mcp?toolset=coding",
      "headers": { "Authorization": "Bearer ${input:loom-token}" }
    }
  },
  "inputs": [
    { "id": "loom-token", "type": "promptString", "password": true, "description": "Loom token" }
  ]
}
```

エージェントモードのチャットを開き、`loom_*` ツールが一覧に表示されることを確認します。`401` が返る場合は[接続の確認](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#checking-the-connection)を参照してください。

<a id="agent-instructions"></a>

## エージェント向け指示

エージェントがツールを呼び出すタイミングを把握できるように、[エージェント向け指示](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-instructions.md)のブロックを `.github/copilot-instructions.md` に追加します。ただし、リポジトリの `AGENTS.md` にすでに含まれている場合は不要です。このブロックは `coding` ツールセットのツールを指しています。`url` に `?toolset=coding` を付けて接続してください。付けない場合、ブロックが挙げるツールのうち一覧に出ないものは、ブロックに書かれているとおり `loom_op` 経由で実行します。

<a id="related"></a>

## 関連項目

- [MCP クライアントの接続](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)：`loom install`、ツールセット、その他のエディタ。
- [MCP リファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md)：エンドポイント、認証モード、ツール一覧。
- [連携](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md)：Loom が接続するすべてのもの。
