<!-- i18n: source=docs/getting-started/connect-a-coding-agent.md blob=4a72e3770686ed00295619940601d7f23baa7988 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md), which is authoritative.

<a id="shared-memory-for-claude-code-codex-and-cursor"></a>

# Claude Code、Codex、Cursor の共有メモリ

Loom は、同じセルフホスト型サーバーに接続したプラグインまたは MCP ツールを通じて、Claude Code、Codex、Cursor に共有の長期メモリを提供します。各チームメンバーに、チームの名前空間へのアクセス権を持つトークンを発行すれば、エージェントは他のメンバーが保存した事実を検索できます。以下のプラグインまたは MCP エントリをインストールすると、次のセッションでエージェントが保存した内容を呼び出せます。Claude Code と Codex のプラグインが Loom に送信する内容については、[データの取り扱い](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#data-handling)を参照してください。会話の記録はデフォルトでオフです。名前空間の所有者は、ダッシュボードの Conversations ページにある **Record conversations** スイッチ、または `conversation.set_logging` でオンにできます。サーバーを自分で運用する場合は、まず[クイックスタート](quickstart.md)を参照してください。エージェントにリリースを固定してインストールさせ、動作を確認させるには、最新リリースの `https://github.com/ClickHouse/loom/releases/latest/download/agent-setup.txt` をエージェントに渡してください（[コーディングエージェントで Loom をセットアップする](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/agent-setup.md)）。

<a id="what-you-need"></a>

## 必要なもの

![設定 → トークン：新しいトークンと、それが出力する Claude Code のセットアップ](https://github.com/ClickHouse/loom/raw/main/docs/images/dashboard-connect.png)

- **サーバー URL**：例：`https://loom.example.com`。`loom up` で起動したスタックの場合は `http://localhost:7777` です。
- **名前空間用の writer トークン**：同じマシン上のシングルユーザーサーバーに接続する場合は不要です。その場合、プラグインや [Cursor](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md#mcp-tools) と [VS Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/vscode.md#local-single-user-server) のインストールリンクは、トークンなしで接続できます。トークンは、ダッシュボードの `<server>/settings` → **Tokens** で作成するか、運用担当者に `loom mint-token --subject you --org <org> --namespaces <namespace> --roles writer` の実行を依頼してください。セルフサインアップが有効なサーバーでは、Claude Code プラグインが最初のセッションでマシンを自動的にサインアップできます。詳しくは [認証情報がない場合の動作](#no-token-yet) を参照してください。

<a id="claude-code"></a>

<a id="how-do-i-give-claude-code-shared-memory"></a>

## Claude Code に共有メモリを持たせるには？

サーバー URL と、共有したい名前空間の writer トークンを指定して Loom プラグインをインストールします。プラグインはセッション開始時に関連するメモリを読み込み、エージェントにファクトを保存・検索するツールを提供します。

```bash
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom \
  --config server_url=https://loom.example.com \
  --config token=<writer JWT>
```

2 つの `--config` の値はどちらも省略できます。省略した場合、Claude Code が最初に一度だけ尋ねます。新しいセッションを開始してください。Loom のセッションプライマーが先頭に表示され、以降は次のように動作します。

- フックは、[データの取り扱い](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#data-handling)に記載されている内容を Loom に送信します。
- `loom-memory`、`loom-code`、`loom-session` などのスキルが、ファクトを保存するタイミング、検索するタイミング、コードインデックスの使い方を Claude に伝えます。
- `loom_search`、`loom_memory_set` とコードツールが MCP ツールとして利用できます。

プラグインがインストールするすべての内容と設定方法は、[Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) のページを参照してください。

<a id="codex"></a>

<a id="how-do-i-give-codex-shared-memory"></a>

## Codex に共有メモリを持たせるには？

Loom プラグインをインストールし、そのフックと MCP エントリを同じサーバーに接続して、共有名前空間にアクセスできるトークンを使います。プラグインはセッション開始時にメモリのプライマーを読み込みます。フックには、以下で説明する承認が必要です。

```bash
codex plugin marketplace add ClickHouse/loom
codex plugin add loom@loom
```

その後、`codex` を一度対話モードで実行し、[プラグインのフックを承認](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md#approve-the-hooks)してください。承認するまでは、ツールは動作しますが、何もキャプチャされません。

プラグインの MCP エントリは `http://localhost:7777` を指しています。別のサーバーを使う場合は、そのサーバーの URL とトークンを `~/.loom/config.json` に書き込み、[Codex: サーバー](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md#the-server)の手順に従って `~/.codex/config.toml` の MCP エントリを上書きします。

Codex プラグインには同じフックとスキルが含まれています。違いについては [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md) のページを参照してください。プラグインをインストールした状態で `codex mcp add loom` も実行しないでください。同じ名前のエントリがプラグインのエントリを置き換えてしまいます。

<a id="cursor-and-other-mcp-clients"></a>

<a id="how-do-i-give-cursor-or-another-mcp-client-shared-memory"></a>

## Cursor などの MCP クライアントにメモリを共有させるには？

クライアントを、共有名前空間用のトークンを使って Loom の MCP エンドポイントに接続し、その後、下にリンクしているエージェント向け指示を追加します。ツールで保存・呼び出しできるのはファクトです。セッションのトランスクリプトを取り込むには、クライアントのフック連携も必要です。

Cursor の Teams プランまたは Enterprise プランでは、チーム管理者が [Cursor プラグイン](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md#plugin)（Cursor では未検証）をインポートできます。このプラグインには MCP のエントリと Loom のスキルが含まれています。

同じマシン上のシングルユーザーサーバーに対して Cursor または VS Code を使う場合、トークンは不要です。[Cursor](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md#mcp-tools) または [VS Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/vscode.md#local-single-user-server) のインストールリンクを使ってください。それ以外の場合は、ダッシュボードで **Settings → Tokens** を開き、**New token** フォームに入力します（トークンがすでにある場合は、先に **New token** を押します）。**Create token** を押してから、**Connect** で使用するクライアント（Cursor、VS Code、Cline、Windsurf、Zed、Claude Desktop）を選びます。ダイアログに、そのクライアントの設定ファイル用のエントリがトークン入りで表示されます。ほかの MCP クライアントでは、[MCP クライアント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)からエントリをコピーしてください。`loom` CLI がインストールされている場合（リポジトリのクローンで `pip install -e .` を実行）は、次の 1 つのコマンドでエントリを書き込めます。

```bash
loom install --client cursor --server-url https://loom.example.com --token "$LOOM_API_KEY"
```

その後、クライアントを再起動してください。`--client` には `claude-desktop`、`vscode`、`cline`、`windsurf`、`zed` も指定できます。指定しない場合、`loom install` は見つかったすべてのクライアントを設定します。これらのクライアントで使えるのはツールです。トランスクリプトの取り込みにはフックが必要で、[Cursor](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md) にはフックがあり、そのほかのクライアントは[トランスクリプトテンプレート](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md#transcript-capture-hooks)を利用します。ツールセットとコマンドが書き込むエントリについては、[MCP クライアント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)を参照してください。続いて、コーディング用クライアントのルールファイルに[エージェント向け指示](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-instructions.md)ブロックを追加し、エージェントがいつツールを呼び出すべきかを分かるようにします。Cursor 以外のクライアントでは、ブロックで挙げているツールが一覧に表示されるよう、エントリの URL に `?toolset=coding` を追加してください（`loom install` では `--toolset coding`）。

<a id="check-that-it-works"></a>

## 動作を確認する

1. Claude Code で `/loom:loom-status` を実行します。プラグインが呼び出すサーバー、トークンが受け付けられて名前空間に書き込めるかどうか、サーバーに届かなかったキャプチャ済みバッチがあるかどうかが報告されます。
2. エージェントに、長期的に残したい内容を伝えます。例：「our deploy window is Tuesday and Thursday only」。新しいセッションで「when can we deploy?」と尋ねます。回答は Loom から得られます。
3. ダッシュボード（`<server>/`）を開きます。保存されたファクトが表示されます。会話の記録が有効な間は、そのセッションの会話も表示されます。どちらも修正または削除できます。

CLI がある場合は、`loom doctor --client` でローカルの接続設定も確認できます。

<a id="when-the-code-index-is-used"></a>

## コードインデックスが使われる場合

プラグインがコードインデックスを接続するのは、インデックス済みリポジトリの git チェックアウト内でセッションが始まった場合のみです。

- チェックアウトのリモート（`origin`、次に `upstream`、その後にその他）を読み取り、サーバーがインデックスしているリポジトリの中から探します。独自のインデックスを持たないフォークは、親リポジトリのインデックスを使います。
- そのリポジトリのコード名前空間への読み取りアクセス権が必要です。ダッシュボードの **Repositories** ページには、読み取り可能なリポジトリが表示され、そこから参加できます。
- 一致した場合、セッションでのコードに関する質問はそのリポジトリに限定されます。インデックス済みパスに対する grep とファイル全体の読み取りには、最新のインデックス済みコミット時点のインデックスから応答します。
- 一致しない場合（git 管理外のディレクトリ、誰もインデックスしていないリポジトリ、読み取り権限のないリポジトリ）は、何も変わりません。エージェントはローカルファイルを読み、メモリは通常どおり動作します。

それでもあるリポジトリのインデックスを使いたい場合は、リポジトリを指定します。指定方法は、`LOOM_CODE_REPO=<owner>/<name>`（例：`ClickHouse/loom`。読み取り可能なリポジトリのうち、その名前のものが 1 つだけなら `loom` のみでも可）、Claude Code プラグインの `code_repo` オプション、または `~/.loom/config.json` の `"code_repo"`（Codex セッションが読み取る設定）です。これはフォールバックです。git チェックアウトの外にいる場合や、リポジトリにインデックスがないチェックアウト（URL の異なるミラーなど）の場合は、プライマーがそのリポジトリの名前空間を示し、すべての `loom_code_*` 呼び出しがそれを既定値として使います。インデックス済みのチェックアウト内では、そのチェックアウト自身のインデックスが優先されます。別のリポジトリを検索するには、呼び出しにその名前空間を渡してください。`local_first`（`LOOM_CODE_LOCAL_FIRST=on`）を指定すると、grep とファイル読み取りでインデックスを使いません。たとえば、未コミットの変更を作業中の場合に便利です。詳細：[Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#the-code-shunt)。

<a id="no-token-yet"></a>

## トークンがまだない場合

`LOOM_SELF_SIGNUP=open` で起動したサーバーでは、トークンを見つけられなかった最初の Claude Code セッションが、そのマシンをサインアップします。トークンは `~/.loom/config.json`（モード 0600）に、クレームコードは `~/.loom/claim-<namespace>.txt` に書き込まれ、名前空間をクレームする方法がプライマーに表示されます。コードは一度だけ使え、サーバーの `/oauth/agent-claim` ページで、自分で決めたパスワードとともに使います。`LOOM_AUTO_SIGNUP=off` を設定すると、この動作は無効になります。セルフサインアップのないサーバーでは、セッションはトークンなしのままとなり、トークンの入手方法がプライマーに表示されます。

<a id="next"></a>

## 次のステップ

- [最初のエージェント](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)：SDK を使って、自分のコードから同じメモリを利用します。
- [エージェント連携](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md)：何を保存するか、いつ検索するか、避けるべき間違いについて説明します。
- [コードインデックス](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)：サーバーがインデックスしたリポジトリについて Loom に質問します。
- [トラブルシューティング](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md)：`/loom:loom-status` から始めます。
