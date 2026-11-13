<!-- i18n: source=docs/use-cases/code-intelligence.md blob=42cc2c3e3e4af87c1d93ff61f15c3e5f7c8f6117 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# コーディングエージェント向けのコードインデックス

Loom は、git リポジトリの共有インデックスをコーディングエージェントに MCP 経由で提供します。エージェントは構造に関する質問を投げるだけで、ファイル全体を読み込まずに、その答えとなる行を受け取れます。利用するにはリポジトリを追加し、`LOOM_CODE_INGEST_ENABLED=true` を設定する必要があります。インデックスの構築方法と最新状態の保ち方については、[コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)を参照してください。

<a id="what-you-get"></a>

## 提供される機能

- **定義、呼び出し元、影響範囲。** `code.symbol`、`code.callers`、`code.impact` は、ファイルと行番号を添えて回答します。各回答には、その根拠となるコミットとインデックスの階層も示されます。[Code operations](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **意味による検索と、完全一致の文字列検索。** `code.search` は、ある仕組みがどこにあるかを見つけます。`code.grep` は、特定のメッセージや識別子を完全一致で見つけます。
- **履歴とプルリクエスト。** レビューコメント付きのマージ済み PR の履歴、PR 作成前の重複チェック、リグレッションの bisect 候補を提供します。これらを使うには PR 履歴を有効にする必要があります（`LOOM_CODE_PR_HISTORY_ENABLED`）。レビューコメントとオープン中の PR には、GitHub トークンも必要です。それぞれに必要なものは [History, PRs and releases](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) を参照してください。
- **確認できる鮮度。** インデックスはマージ済みのコミット数だけブランチに遅れます。古い回答にはその旨が表示されます。[Freshness](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **プラグイン。** Claude Code プラグインは `coding` ツールセットで接続し、Codex プラグインは `core,coding,tracker,tasks` で接続します。どちらもコードツールを一覧に表示します。Cursor などの MCP クライアントでは `?toolset=coding` を使います。[Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)

<a id="measured"></a>

## 計測結果

ClickHouse リポジトリで、Claude Code に各回答の末尾へ最大3つのファイルを挙げるよう指示しました。Loom を使った場合、36 件の概念的な質問（10 回のペア実行）のうち 96.4% で正しいファイルを挙げられました。Loom を使わない場合は 91.4% でした。また、Loom を使った実行はコストが 21% 低くなりました（エージェントのモデル利用料金で、Loom 自身のモデル呼び出しは除きます）。
SWE-bench Verified の 200 件の issue では、`code.search` は 82% で、変更されたファイルを上位 10 件の結果に含めました（issue の解決ではなく、ファイルの特定）。これに対し、インデックス済みのすべてのファイルを対象にした BM25 は 57%、`.py` ファイルのみを対象にした BM25 は 79% でした。`.py` のみとの差は誤差の範囲内です。
[Loom あり・なしの比較](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[SWE-bench Verified のファイル特定](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## はじめに

[リポジトリの追加](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)を行うと、そのリポジトリのインデックスが有効になります。ティアについては[コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)で説明しています。

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で experimental と記されています。
