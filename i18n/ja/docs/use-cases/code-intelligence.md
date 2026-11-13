<!-- i18n: source=docs/use-cases/code-intelligence.md blob=4f52e242dcb612150541843af58b15f37c31a265 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# コーディングエージェント向けのコードインデックス

Loom は、git リポジトリの共有インデックスを MCP 経由でコーディングエージェントに提供します。エージェントは構造に関する質問を投げるだけで、ファイル全体を読み込まずに、答えとなる行を受け取れます。リポジトリを追加し、`LOOM_CODE_INGEST_ENABLED=true` を設定しておく必要があります。インデックスの構築方法と最新に保つ方法は [コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) を、各言語で得られる情報は [言語別のコードインデックス](code-intelligence-by-language.md) を参照してください。

<a id="what-you-get"></a>

## 提供される機能

- **定義、呼び出し元、影響範囲。** `code.symbol`、`code.callers`、`code.impact` は、ファイルと行番号を添えて回答します。各回答には、その根拠となるコミットとインデックスのティアも示されます。[Code operations](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **意味による検索と完全一致の文字列検索。** `code.search` はある仕組みがどこにあるかを見つけ、`code.grep` は正確なメッセージや識別子を見つけます。
- **履歴とプルリクエスト。** レビューコメント付きのマージ済み PR の履歴、PR 作成前の重複チェック、リグレッションの bisect 候補を提供します。これらを使うには PR 履歴を有効にする必要があります（`LOOM_CODE_PR_HISTORY_ENABLED`）。レビューコメントとオープンな PR には GitHub トークンも必要です。それぞれの要件は [History, PRs and releases](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) を参照してください。
- **確認できる鮮度。** インデックスはマージ済みコミットの分だけブランチより遅れており、古い回答にはその旨が示されます。[Freshness](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **プラグイン。** Claude Code プラグインは `coding` ツールセットで、Codex プラグインは `core,coding,tracker,tasks` で接続し、どちらもコードツールを一覧に表示します。Cursor などの MCP クライアントは `?toolset=coding` を使います。[Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)

<a id="measured"></a>

## 計測結果

ClickHouse リポジトリで、Claude Code に各回答の末尾へ最大 3 つのファイルを挙げるよう指示しました。Loom を使った場合、36 件の概念的な質問（10 回のペア実行）に対して正しいファイルを挙げた割合は 96.4% で、Loom を使わない場合の 91.4% を上回りました。また、Loom を使った実行のコストは 21% 低くなりました（エージェントのモデル利用料金。Loom 自身のモデル呼び出しは除く）。
SWE-bench Verified の 200 件の issue では、`code.search` は 82% で、変更されたファイルを上位 10 件の結果に含めました（ファイルの特定であり、issue の解決ではありません）。これに対し、インデックス済みのすべてのファイルを対象にした BM25 は 57%、`.py` ファイルのみを対象にした BM25 は 79% でした。`.py` のみの場合との差は誤差の範囲内です。
[Loom あり・なしの比較](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[SWE-bench Verified のファイル特定](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## はじめに

[リポジトリの追加](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)を行うと、そのリポジトリのインデックスが有効になります。階層については[コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)を参照してください。

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で experimental と記載されています。
