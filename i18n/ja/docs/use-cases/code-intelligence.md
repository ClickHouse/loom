<!-- i18n: source=docs/use-cases/code-intelligence.md blob=6fd1469bda94290a1ca6a632c41c8fb026085a22 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# コーディングエージェント向けのコードインデックス

Loom は、git リポジトリの共有インデックスをコーディングエージェントに提供し、MCP 経由で公開します。エージェントは構造に関する質問を投げるだけで、ファイル全体を読み込まなくても、答えとなる行を受け取れます。利用するには、リポジトリを追加し、`LOOM_CODE_INGEST_ENABLED=true` を設定する必要があります。インデックスの構築方法と最新の状態に保つ仕組みについては、[コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)を参照してください。

<a id="what-you-get"></a>

## できること

- **定義、呼び出し元、影響範囲。** `code.symbol`、`code.callers`、`code.impact` は、ファイルと行番号を添えて回答します。各回答には、その根拠となるコミットとインデックスの階層も示されます。[Code operations](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **意味による検索と、完全一致の文字列検索。** `code.search` は、ある仕組みがどこにあるかを探します。`code.grep` は、正確なメッセージや識別子を探します。
- **履歴とプルリクエスト。** レビューコメント付きのマージ済み PR の履歴、PR 作成前の重複チェック、リグレッションの bisect 候補を提供します。これらを使うには PR 履歴を有効にする必要があります（`LOOM_CODE_PR_HISTORY_ENABLED`）。レビューコメントとオープンな PR には、さらに GitHub トークンも必要です。それぞれに必要なものは [History, PRs and releases](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) を参照してください。
- **確認できる鮮度。** インデックスはマージ済みのコミット分だけブランチより遅れます。古い回答にはその旨が示されます。[Freshness](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **プラグイン。** Claude Code プラグインは `coding` ツールセットで接続し、Codex プラグインは `core,coding,tracker,tasks` で接続します。どちらもコードツールを一覧に含みます。Cursor などの MCP クライアントは `?toolset=coding` を使います。[Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)

<a id="measured"></a>

## 計測結果

ClickHouse リポジトリでは、Claude Code は各回答の最後に 3 つのファイルを挙げます。Loom を使った場合、概念的な 36 件の質問（10 回のペア実行）のうち 96.4% で正しいファイルがその中に含まれていました。Loom を使わない場合は 91.4% で、モデルコストは 21% 低くなりました（エージェントのモデル利用料金であり、Loom 自身のモデル呼び出しは除きます）。
200 件の SWE-bench Verified の issue では、`code.search` は 82% で変更されたファイルを上位 10 件以内にランク付けしました（ファイルの特定であり、issue の解決ではありません）。これに対し、インデックス化したすべてのファイルを対象とした BM25 は 57%、`.py` ファイルのみを対象とした BM25 は 79% でした。`.py` のみとの差は誤差の範囲内です。
[Loom あり・なしの比較](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[SWE-bench Verified のファイル特定](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## はじめに

[リポジトリの追加](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)によって、リポジトリのインデックスが有効になります。ティアについては[コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)で説明しています。

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で experimental と記載されています。
