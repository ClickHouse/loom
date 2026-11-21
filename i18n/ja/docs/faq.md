<!-- i18n: source=docs/faq.md blob=d1e8cdcb41e8e749ecd46d194697f615642a3ca8 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/faq.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/faq.md), which is authoritative.

<a id="frequently-asked-questions"></a>

# よくある質問

最初によく寄せられる質問への短い回答です。各回答から、詳しく説明しているページへリンクしています。

<a id="using-it"></a>

## 使い方

<a id="what-is-the-fastest-way-to-try-loom"></a>

### Loom を最も早く試す方法は何ですか？

Loom サーバーがすでにある場合は、Claude Code または Codex のプラグインをインストールし、サーバーを指定します：[コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)。ノートPC でサーバーを動かすには、`loom up` で数分で起動できます：[クイックスタート](getting-started/quickstart.md)。

<a id="how-do-i-give-claude-code-codex-or-cursor-long-term-memory"></a>

### Claude Code、Codex、Cursor に長期メモリを持たせるにはどうすればよいですか？

セッション開始時のコンテキストとキャプチャには Claude Code または Codex のプラグインを使います。それ以外の MCP クライアントは `loom install --client <name>` で接続できます（[MCP クライアント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)を参照）。サーバーは[クイックスタート](getting-started/quickstart.md)で起動します。Cursor には MCP エントリと `loom`、`loom-code` スキルが設定されます（セッション開始時のコンテキスト用のフックはなく、Cursor ではまだテストされていません）。トランスクリプトのキャプチャは、別の `sessionEnd` フックで行われます。詳しくは[コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)と [Cursor](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md) を参照してください。

<a id="why-are-my-search-results-nonsense"></a>

### 検索結果が的外れなのはなぜですか？

インストールでフェイクの埋め込みモデルが動いています。その `.env`（チェックアウト内のもの、または `~/.loom/.env`）に `fake-ngram-*` が指定されており、そのベクトルでは検索が意味を成しません。埋め込みモデルを切り替えると、保存済みのデータが再埋め込みされます（[モデルとキー](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)）。リリース版スタックを新規インストールした場合は、すでにローカルモデルで埋め込みが行われます。ホスト型の埋め込みモデルは、言い換えの多い質問でより良く検索できます（[プロバイダー](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#providers)）。

<a id="why-does-memorysearch-say-abstain-instead-of-returning-something"></a>

### `memory.search` が結果を返さず `abstain` と答えるのはなぜですか？

お使いの埋め込みモデル向けに調整された類似度の下限を超えるヒットがなかったため、Loom は最も近い弱い一致を返さず、分からないと答えます。理由はレスポンスに含まれます。[検索とランキング](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)を参照してください。

<a id="why-was-my-write-flagged-as-a-conflict-or-refused"></a>

### 書き込みが競合としてフラグ付けされたり、拒否されたりしたのはなぜですか？

既存のメモリと意味が近い書き込みは `conflicted` としてマークされ、そのメモリにリンクされます。固定されたメモリに近い書き込みは `409 PINNED_CONFLICT` で拒否され、ブラックリストのパターンに一致する書き込みは `422 BLACKLISTED` になります。競合は、ダッシュボードのレビューキューか `conflicts.resolve` で解決します。[競合と拒否](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)を参照してください。

<a id="can-i-use-loom-just-for-myself"></a>

### Loom を自分だけで使えますか？

はい。ログインなし・名前空間 1 つの[ローカルスタック](getting-started/quickstart.md)を動かし、[プラグインまたは `loom install`](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) でエージェントを接続します。ログインのあるサーバーでは、各ユーザーに `personal-<slug>` 名前空間が用意されます。MCP クライアントはデフォルトでここに書き込み、権限が付与されない限り他の人は読めません。Claude Code から保存したメモリは、Codex、Cursor、ダッシュボードからも利用できます。

<a id="can-two-agents-see-each-others-memories"></a>

### 2 つのエージェントは互いのメモリを見られますか？

名前空間のメンバーは全員、その行を読めます。各エージェントは自分専用のレイヤーに書き込み、管理者は `memory.promote` で名前空間の共有レイヤーにメモリをコピーできます。トークンは複数の名前空間を読みつつ、1 つに書き込むことができます。[ID と名前空間](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md)を参照してください。

<a id="when-should-i-use-knowledge-documents-instead-of-memories"></a>

### メモリではなくナレッジドキュメントを使うのはどんなときですか？

メモリは、改訂可能な短い主張 1 つです。ドキュメント、長いページ、数キロバイトを超えるものは、`knowledge.add` でナレッジプレーンに入れます。これがチャンク分割と埋め込みを行います。[メモリ](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#what-goes-where)と[ナレッジドキュメント](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md)を参照してください。

<a id="how-do-i-make-loom-forget-something"></a>

### Loom に何かを忘れさせるにはどうすればよいですか？

`memory.delete` は 1 つのメモリを墓標（tombstone）にします（猶予期間中は復元できます）。`erasure.request` は、ユーザーまたは名前空間が所有するすべてを全テーブルから削除し、何も残っていないことを検証します。オペレーターが保持期間を設定しない限り、Loom が自動的に削除することはありません。[保持と消去](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)を参照してください。

<a id="how-do-i-find-out-why-a-result-ranked-where-it-did"></a>

### 結果がその順位になった理由を調べるにはどうすればよいですか？

`memory.search` を `explain=true` で呼び出します。各ヒットの `score_components` には、ランキングのシグナルが含まれます。リランカー使用後は、その順位の動き（`rerank_from`、`rerank_pos`、`rerank_delta`）も含まれ、リランカーが順序を変えた場合は、そのリランカー自身の統合スコア（`rank_score_pre_rerank`）も含まれます。`memory.search_trace` はベクトル検索の部分だけをスコアリングします。[検索と ask](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)を参照してください。

<a id="can-loom-answer-questions-about-my-code"></a>

### Loom は自分のコードに関する質問に答えられますか？

はい、サーバーがインデックスしているリポジトリが対象です。`code.search`、`code.symbol`、`code.callers`、`code.impact` などがあり、各回答にはどのコミットとインデックスのティアに基づくかが示されます。[コードインデックス](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)と[リポジトリの追加](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)を参照してください。

<a id="why-doesnt-my-coding-agent-use-the-code-index"></a>

### コーディングエージェントがコードインデックスを使わないのはなぜですか？

プラグインがインデックスを接続するのは、セッションが git チェックアウト内で始まり、そのリモートが、読み取り権限のあるインデックス済みリポジトリである場合に限られます。リポジトリのチェックアウトからセッションを開始し、ダッシュボードの **Repositories**（`<server>/repositories`）にそのリポジトリが表示されていることを確認してください。[コードインデックスが使われる場合](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md#when-the-code-index-is-used)も参照してください。

<a id="running-it"></a>

## 実行について

<a id="do-i-need-clickhouse-cloud"></a>

### ClickHouse Cloud は必要ですか？

いいえ。Loom は ClickHouse OSS でも動作し、ラップトップ用スタックは Docker 上で ClickHouse OSS を起動します。本番環境での利用では Cloud を想定しています。単一ノードの OSS にはレプリケーションもポイントインタイムリカバリもないためです。詳しくは[デプロイ](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)を参照してください。

<a id="can-i-use-anthropic-or-gemini-models"></a>

### Anthropic や Gemini のモデルは使えますか？

はい。LLM プロバイダーには OpenAI または Anthropic を使用でき、埋め込みは OpenAI、Gemini、またはローカルの CPU モデルから取得できます。埋め込みモデルを変更するとベクトル空間が変わるため、変更後に `loom reembed` を実行してください。詳しくは [LLM と埋め込み](https://github.com/ClickHouse/loom/blob/main/docs/architecture/llm-architecture.md)を参照してください。

<a id="how-do-i-give-someone-access"></a>

### 他のユーザーにアクセス権を付与するには？

トークンを発行し（`loom mint-token`、またはダッシュボードの Settings → Tokens）、名前空間に対するロールを付与します。詳しくは[トークンとグラント](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)を参照してください。

<a id="something-is-wrong-where-do-i-start"></a>

### 問題が発生しました。どこから始めればよいですか？

サーバー上で `loom doctor`、Claude Code で `/loom:loom-status`、そして `/v1/health` を確認してください。そのうえで、症状別にまとめられている[トラブルシューティング](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md)を参照してください。

<a id="the-project"></a>

## プロジェクト

<a id="why-is-an-operation-missing-from-my-mcp-tool-list"></a>

### MCP ツール一覧に操作が表示されないのはなぜですか？

MCP の一覧はツールセット（`?toolset=` または `LOOM_MCP_TOOLSET`、デフォルトは `core`）によって決まり、コードツールは `LOOM_MCP_CODE_TOOLS=true` の場合にのみ一覧に表示されます。`all` では、core にも atomic にも載っておらず、`experimental` と記されている op は一覧から除外されます。ただし、すべての op は `loom_op` を通じて名前で呼び出すことができ（`readonly` では読み取りのみ）、HTTP や SDK からも呼び出せます。[MCP ツールセット](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets)を参照してください。

<a id="how-good-is-the-retrieval-and-how-do-you-know"></a>

### 検索の品質はどの程度で、どのように確認できますか？

Loom は、手法、データセット、実行コマンドとともにベンチマークスイートを提供しています。記載しているのは、コミット済みのファイルから再現できる結果のみです。[ベンチマーク](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/overview.md)を参照してください。

<a id="how-is-loom-different-from-putting-a-vector-database-next-to-my-app"></a>

### Loom は、アプリの隣にベクトルデータベースを置く構成とどう違いますか？

デフォルトの ClickHouse authority では、1 つの ClickHouse データベースが行、ベクトル、テキストインデックス、グラフ、監査、消去の状態を保持するため、同期を保つ必要がありません。[Postgres メモリ authority](architecture/storage-authority.md#why-two-stores)では、稼働中のメモリと知識の状態を Postgres に保持し、2 つのストアの整合は Loom 自身が保ちます。各書き込みは、保存されている内容と照合して検証されます。各特性にかかるコストと、Loom が適さない場合については、[概要](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md)にまとめています。
