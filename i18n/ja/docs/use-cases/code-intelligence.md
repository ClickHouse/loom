<!-- i18n: source=docs/use-cases/code-intelligence.md blob=9f10f6e9bc93b39e97d3aee93cedeeb7260a7297 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# コーディングエージェント向けのコードインデックス

Loom は、git リポジトリの共有インデックスを MCP 経由でコーディングエージェントに提供します。エージェントは構造に関する質問を投げるだけで、ファイル全体を読まずに、答えとなる行を受け取れます。リポジトリを追加し、`LOOM_CODE_INGEST_ENABLED=true` を設定しておく必要があります。インデックスの構築方法と最新に保つ方法は [コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) を、言語ごとに得られる内容は [言語別コードインデックス](code-intelligence-by-language.md) を参照してください。

<a id="what-you-get"></a>

## 得られるもの

- **定義、呼び出し元、影響範囲。** `code.symbol`、`code.callers`、`code.impact` は、ファイルと行の参照付きでコードの場所を示します。`code.index_status` は各ティアのインデックス済みコミットを報告します。[コードオペレーション](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **意味による検索と完全一致の文字列検索。** `code.search` は仕組みが存在する場所を見つけ、`code.grep` は正確なメッセージや識別子を見つけます。
- **履歴とプルリクエスト。** レビューコメント付きのマージ済み PR の履歴、PR 作成前の重複チェック、リグレッションに対する bisect 候補を提供します。これらには PR 履歴を有効にする設定（`LOOM_CODE_PR_HISTORY_ENABLED`）が必要で、レビューコメントとオープン中の PR には GitHub トークンも必要です。それぞれの要件は [履歴、PR、リリース](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) にあります。
- **コードに紐づく知見。** `code.symbol` は定義にリンクされたメモリを返します。`code.search`、`code.callers`、`code.callees`、`code.impact`、`code.outline`、およびタスクの `code.context_pack` は、返すファイルに関する知見を付与し、そのコードが移動したか、変更されたか、削除されたかも示します。[コードにリンクされたメモリ](memory-linked-to-code.md)
- **確認できる鮮度。** 各ティアはブランチより遅れることがあります。`as_of` が返された場合はそれを確認するか、`code.index_status` で該当ティアのウォーターマークを確認してください。鮮度メタデータがない場合は不明として扱います。[鮮度](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **プラグインと MCP クライアント。** Claude Code プラグインは `coding` ツールセットで、Codex プラグインは `core,coding,tracker,tasks` で接続し、どちらもコードツールを一覧に表示します。Cursor、GitHub Copilot、Gemini CLI、Cline、Kilo Code、OpenCode などの MCP クライアントは、URL に `?toolset=coding` を付けるとコードツールを一覧に表示します（`loom install` は Cursor 向けにこれを追加し、それ以外では書き出す設定に `--toolset coding` で追加します）。[コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) · [MCP クライアント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)

<a id="measured"></a>

## 計測結果

ClickHouse リポジトリで、Claude Code に各回答の最後に最大 3 つのファイルを挙げるよう求めました。Loom を使うと、36 件の概念的な質問（10 回のペア実行）のうち 96.4% で正しいファイルを挙げ、Loom なしの 91.4% を上回りました。また Loom ありの実行はコストが 21% 低くなりました（エージェントのモデル利用料金であり、Loom 自身のモデル呼び出しは含みません）。SWE-bench Verified の 200 件の issue では、`code.search` は 82% で変更されたファイルを上位 10 件に入れました（ファイルの特定であり、issue の解決ではありません）。これに対し、インデックス済みの全ファイルに対する BM25 は 57%、`.py` ファイルのみに対する BM25 は 79% でした。`.py` のみとの差は誤差の範囲内です。[Loom あり・なしの比較](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) · [SWE-bench Verified のファイル特定](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="a-coding-agent-that-reads-the-definition-it-needs"></a>

## 必要な定義を読み取るコーディングエージェント

信頼できるリポジトリから始めます。リリース済みのスタックでは、[オペレーターが設定するインデックス手順](../getting-started/quickstart.md#optional-index-a-repository-on-the-released-stack) に従ってください。ダッシュボードのリポジトリ追加フローにはサンドボックスが必要ですが、このスタックでは有効になっていません。Claude Code または Codex をプラグインで接続し、そのリポジトリのチェックアウト内でセッションを開始します（[リポジトリの照合](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md#when-the-code-index-is-used)）。

1. エージェントに、`op: "code.index_status"` と `arguments: {"namespace": "<indexed namespace>", "brief": true}` を指定して `loom_op` を呼び出させます。高速ティアのインデックス済みコミットである `git_head` を記録します。値が空の場合、そのティアにはウォーターマークがありません。
2. 定義を把握している関数を選びます。エージェントに、その名前空間で関数名と `include_body: true` を指定して `loom_code_symbol` を呼び出させます。返された `path`、`start_line`、`end_line`、`body`、`tier` を確認します。
3. この確認は、`tier: "treesitter"` で、曖昧さのない 1 つの定義かつ省略されていない完全な本体が返された場合にのみ続けます。利用する前に、そのパス、範囲、本体を、記録した `git_head` 時点のソースと比較します。ウォーターマークや本体がない場合、定義が異なる場合、または別のティアが返された場合は、ローカルで読み取ってください。コンパイラティアはそれぞれ独立して進むため、そのウォーターマークは別途リビジョンを確認する必要があります（[鮮度](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md#the-mirrors-own-age)）。
4. 同じチェックアウトで新しいセッションを開始し、2 つの呼び出しを繰り返します。インデックスは再び定義を返し、エージェントは以前のチャットやファイル全体を読み込む必要がありません。

これは高速ティアの定義 1 件を確認するものです。コミットされていない編集には引き続きローカルでの読み取りが必要であり、検索の成功はタスクの精度やコスト削減の計測にはなりません。検索、呼び出し元、影響範囲については [コードオペレーション](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md) を参照してください。

<a id="start"></a>

## 開始する

[リポジトリの追加](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md) では、リポジトリに対してインデックスを有効にします。ティアについては [コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) で説明しています。

このページのオペレーションの一部は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md) で experimental とされています。
